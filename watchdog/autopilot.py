"""Autopilot: find -> fix -> prove -> ship, with no person in the loop, and fail safe when it can't.

    python3 -m watchdog.autopilot [--minutes 3] [--no-ship]

Every run (hourly from the server, or cron):
  1. Hunt: play generated attack scenarios against a throwaway desk (watchdog.sim).
  2. For each finding, ask a model (Claude via the Anthropic API) for a code fix as a unified diff.
  3. Prove it in a scratch copy of the repo. A fix ships only if ALL gates pass:
       - it touches only application code (allowance/, server.py, web/) - never tests/, watchdog/ or receipts/,
         so the fixer cannot weaken the checks that judge it;
       - it is small (<= 150 changed lines);
       - the full test suite passes;
       - every finding the Watchdog has ever saved replays clean;
       - a fresh hunt of new scenarios finds no rule break.
  4. Ship: apply to the live repo, commit with a receipt, and signal the server to reload.
     Re-run the suite on the live tree; if anything is red, revert the commit automatically.
  5. Fail safe: if a MONEY rule break can't be fixed in 3 tries, press STOP on the live desk
     ("autopilot: ..."). Payments stay stopped until a later run ships a verified fix; then autopilot
     clears only the STOP it raised itself.
Everything it does is appended to watchdog/autopilot-log.jsonl and summarized in watchdog/STATUS.json.
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIND = os.path.join(HERE, "findings")
LOG = os.path.join(HERE, "autopilot-log.jsonl")
STATUS = os.path.join(HERE, "STATUS.json")
SUITE = ["tests.test_allowance", "tests.test_break", "tests.test_break_server", "tests.test_codex_review",
         "tests.test_nov1_review", "tests.test_nov1_verify", "tests.test_indep", "tests.test_r3_concurrency",
         "tests.test_r3_normalizer", "tests.test_r3_provider", "tests.test_watchdog"]
ALLOWED = re.compile(r"^(allowance/[\w/]+\.py|server\.py|web/[\w.]+)$")
MONEY_RULES = {"UNTRACKED_CREATE", "UNAPPROVED_PAYEE", "PAYEE_DETAILS_CHANGED", "OVER_PER_TX_CAP", "SENT_WHILE_STOPPED",
               "SECOND_LIVE_TRANSFER", "OVER_DAILY_CAP", "UNTRACKED_MONEY", "SUITE_RED"}
MAX_LINES = 150


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def log(event, **kw):
    with open(LOG, "a") as f:
        f.write(json.dumps({"at": now(), "event": event, **kw}) + "\n")
    print(f"[autopilot] {event} {json.dumps(kw)[:300]}", flush=True)


def run_suite(cwd):
    r = subprocess.run([sys.executable, "-m", "unittest", *SUITE], cwd=cwd, capture_output=True, text=True, timeout=900)
    tail = "\n".join(l for l in (r.stderr or "").splitlines() if "ResourceWarning" not in l)[-3000:]
    return r.returncode == 0, tail


def hunt(cwd, seeds, steps=25):
    """Run a hunt inside `cwd` (its own code). Returns list of (rule, seed, steps, detail)."""
    code = (f"import sys,json; sys.path.insert(0,{cwd!r});\n"
            f"from watchdog.sim import gen, play\n"
            f"out=[]\nseen=set()\n"
            f"for s in range({seeds[0]},{seeds[1]}):\n"
            f"  st=gen(s,{steps}); v,c=play(st)\n"
            f"  h=[(x[1],x[2]) for x in v]+([('CRASH',c[1])] if c else [])\n"
            f"  for r,d in h[:1]:\n"
            f"    if r not in seen: seen.add(r); out.append([r,s,d])\n"
            f"print('@@'+json.dumps(out))")
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True, timeout=1800)
    m = re.search(r"@@(.*)", r.stdout or "")
    if not m:
        return [["CRASH", -1, "hunt itself failed: " + (r.stderr or "")[-300:]]]
    return json.loads(m.group(1))


def replay_all(cwd):
    code = (f"import sys,glob,json; sys.path.insert(0,{cwd!r});\n"
            f"from watchdog.replay import replay\n"
            f"bad=[f for f in sorted(glob.glob({os.path.join(cwd, 'watchdog', 'findings', '*.json')!r})) if replay(f,verbose=False)!=([],None)]\n"
            f"print('@@'+json.dumps(bad))")
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True, timeout=900)
    m = re.search(r"@@(.*)", r.stdout or "")
    return json.loads(m.group(1)) if m else ["replay crashed: " + (r.stderr or "")[-200:]]


def ask_fixer(finding, evidence, attempt, last_error):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("no ANTHROPIC_API_KEY: autopilot can find and fail safe, but cannot write fixes")
    src = ""
    for f in ["allowance/service.py", "allowance/agent.py", "allowance/store.py"]:
        src += f"\n=== {f} ===\n" + open(os.path.join(ROOT, f)).read()
    prompt = (
        "You maintain a payments safety desk (Python). An automated attacker found a rule break. Write the smallest "
        "correct fix as a unified diff (git apply format, paths relative to repo root, a/ and b/ prefixes). Only edit "
        "allowance/*.py, server.py or web/*. Never edit tests or the watchdog. Never weaken a safety check, cap, "
        "approval, allowlist or the STOP switch; when in doubt, make the code refuse or raise an alarm instead of "
        "letting money move. Output ONLY the diff inside one ```diff block.\n\n"
        f"FINDING:\n{json.dumps(finding, indent=1)}\n\nREPLAY OUTPUT:\n{evidence}\n"
        + (f"\nYOUR PREVIOUS ATTEMPT FAILED THE GATES:\n{last_error[-2500:]}\n" if last_error else "")
        + f"\nSOURCE:{src}")
    body = {"model": os.environ.get("AUTOPILOT_MODEL", "claude-sonnet-5-5"), "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read())
    text = "".join(b.get("text", "") for b in out.get("content", []))
    m = re.search(r"```diff\n(.*?)```", text, re.S)
    return m.group(1) if m else text


def touched(diff):
    return sorted(set(re.findall(r"^\+\+\+ b/(\S+)", diff, re.M)))


def changed_lines(diff):
    return sum(1 for l in diff.splitlines() if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---")))


def prove(diff, finding):
    """Apply the diff to a scratch copy and run every gate. Returns (ok, why)."""
    files = touched(diff)
    if not files:
        return False, "no file changes in the diff"
    bad = [f for f in files if not ALLOWED.match(f)]
    if bad:
        return False, f"diff touches files the fixer may not edit: {bad}"
    if changed_lines(diff) > MAX_LINES:
        return False, f"diff too large ({changed_lines(diff)} lines > {MAX_LINES})"
    tmp = tempfile.mkdtemp(prefix="autopilot-")
    scratch = os.path.join(tmp, "repo")
    shutil.copytree(ROOT, scratch, ignore=shutil.ignore_patterns("__pycache__", "*.db", "*.db-*"))
    pf = os.path.join(tmp, "fix.diff")
    open(pf, "w").write(diff)
    r = subprocess.run(["git", "apply", "--whitespace=nowarn", pf], cwd=scratch, capture_output=True, text=True)
    if r.returncode:
        return False, "git apply failed: " + r.stderr[-800:]
    ok, tail = run_suite(scratch)
    if not ok:
        return False, "test suite failed:\n" + tail
    still = replay_all(scratch)
    if still:
        return False, f"saved findings still break: {still}"
    fresh = hunt(scratch, (int(time.time()) % 10**6, int(time.time()) % 10**6 + 300))
    if fresh:
        return False, f"fresh hunt found new breaks: {fresh}"
    return True, "all gates passed"


def ship(diff, finding):
    pf = os.path.join(tempfile.mkdtemp(), "fix.diff")
    open(pf, "w").write(diff)
    subprocess.run(["git", "apply", "--whitespace=nowarn", pf], cwd=ROOT, check=True)
    msg = (f"autopilot: fix {finding['rule']} ({os.path.basename(finding['_path'])})\n\n"
           f"Found by the Watchdog: {finding['detail'][:300]}\nGates: full suite, all saved findings replay clean, fresh hunt clean.\n\n"
           "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    subprocess.run(["git", "add", "-A", "allowance", "server.py", "web"], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=ROOT, check=True)
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    ok, tail = run_suite(ROOT)  # post-deploy check on the live tree
    if not ok:
        subprocess.run(["git", "revert", "--no-edit", "HEAD"], cwd=ROOT, check=True)
        log("rolled_back", commit=sha, why=tail[-500:])
        return None
    open(os.path.join(HERE, "RELOAD"), "w").write(sha)  # the server restarts itself on the next tick
    return sha


def live_desk():
    db = os.environ.get("ALLOWANCE_DB")
    if not db or not os.path.exists(db):
        return None
    sys.path.insert(0, ROOT)
    from allowance.service import Service
    from allowance.store import Store

    class _NoProvider:  # STOP/resume never talks to Airwallex
        pass
    return Service(Store(db), _NoProvider())


def fail_safe(rule, detail):
    svc = live_desk()
    if svc:
        svc.kill("system", f"autopilot: unfixed {rule} - payments stopped until a verified fix ships")
    log("fail_safe_stop", rule=rule, detail=detail[:300], desk=bool(svc))


def clear_own_stop():
    svc = live_desk()
    if not svc:
        return
    pol = svc.s.policy()
    if pol["killed"] and pol["killed_by"] == "system" and (pol["killed_reason"] or "").startswith("autopilot:"):
        if not open_findings(money_only=True):
            svc.unkill("autopilot", confirmed_by="autopilot-verified-fix")
            log("fail_safe_cleared", reason=pol["killed_reason"])


def open_findings(money_only=False):
    out = []
    for f in sorted(glob.glob(os.path.join(FIND, "*.json"))):
        rec = json.load(open(f))
        if rec.get("status") == "open" and (not money_only or rec["rule"] in MONEY_RULES):
            rec["_path"] = f
            out.append(rec)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=3)
    ap.add_argument("--no-ship", action="store_true", help="find and prove fixes, but do not commit them")
    a = ap.parse_args(argv)
    lock = os.path.join(HERE, ".autopilot.lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 3 * 3600:
        print("another autopilot run is active"); return 0
    open(lock, "w").write(str(os.getpid()))
    try:
        return _run(a)
    finally:
        os.remove(lock)


def _run(a):
    from .run import save, shrink
    from .replay import replay
    started, stats = time.time(), {"scenarios": 0, "new_findings": 0, "fixed": 0, "stopped": False}
    ok, tail = run_suite(ROOT)
    if not ok:
        save("SUITE_RED", -1, [], "the test suite is failing on the live code:\n" + tail[-600:])
    # hunt in time-boxed batches
    base = int(time.time()) % 10**7
    while time.time() - started < a.minutes * 60:
        found = hunt(ROOT, (base, base + 200))
        stats["scenarios"] += 200
        base += 200
        sys.path.insert(0, ROOT)
        from .sim import gen
        for rule, seed, detail in found:
            if any(f["rule"] == rule for f in open_findings()):
                continue
            small = shrink(gen(seed), rule) if seed >= 0 else []
            save(rule, seed, small, detail)
            stats["new_findings"] += 1
            log("found", rule=rule, seed=seed, steps=len(small), detail=detail[:200])
    for fnd in open_findings():
        if fnd["rule"] == "SUITE_RED":
            evidence = fnd["detail"]
        else:
            v, c = replay(fnd["_path"], verbose=False)
            if (v, c) == ([], None):
                _close(fnd, "no longer reproduces")
                continue
            evidence = json.dumps({"violations": v, "crash": c})
        last = None
        for attempt in range(1, 4):
            try:
                diff = ask_fixer({k: v for k, v in fnd.items() if not k.startswith("_")}, evidence, attempt, last)
            except Exception as e:  # no key / API down: can't fix this run
                last = f"fixer unavailable: {e}"
                break
            good, why = prove(diff, fnd)
            log("fix_attempt", rule=fnd["rule"], attempt=attempt, passed=good, why=why[:400], lines=changed_lines(diff))
            if good:
                if a.no_ship:
                    open(fnd["_path"] + ".proposed.diff", "w").write(diff)
                    break
                sha = ship(diff, fnd)
                if sha:
                    _close(fnd, f"fixed by autopilot commit {sha}")
                    stats["fixed"] += 1
                    log("shipped", rule=fnd["rule"], commit=sha)
                break
            last = why
        else:
            pass
        if json.load(open(fnd["_path"])).get("status") == "open" and fnd["rule"] in MONEY_RULES:
            fail_safe(fnd["rule"], fnd["detail"])
            stats["stopped"] = True
    clear_own_stop()
    st = {"last_run": now(), "minutes": round((time.time() - started) / 60, 1), **stats,
          "open": [{"rule": f["rule"], "detail": f["detail"][:160]} for f in open_findings()],
          "commit": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()}
    json.dump(st, open(STATUS, "w"), indent=1)
    log("run_done", **{k: v for k, v in st.items() if k != "open"}, open=len(st["open"]))
    return 1 if st["open"] else 0


def _close(fnd, how):
    rec = json.load(open(fnd["_path"]))
    rec["status"], rec["closed"], rec["how"] = "fixed", now(), how
    json.dump(rec, open(fnd["_path"], "w"), indent=1)


if __name__ == "__main__":
    sys.exit(main())
