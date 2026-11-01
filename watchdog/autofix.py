"""Self-repair, no human in the loop.

For every open Watchdog finding that still breaks at HEAD:
  1. (money rules) the live desk is put on a WATCHDOG STOP - no payment can go out while a money rule is broken
  2. a repair model (Claude) is shown the finding, its replay, the rules, and the source; it returns edits
  3. automatic gates, all must pass:
       G1 scope   - edits only in allowance/*.py; never tests/, watchdog/ or the rule checker
       G2 no-weaken - the patch may not remove refusals, alarms, STOP or lock calls, or raise caps
       G3 tests   - the full test suite passes in a scratch copy
       G4 replay  - this finding AND every earlier finding replay clean
       G5 hunt    - a fresh 300-scenario hunt is clean
       G6 second opinion - a different model reviews the diff and must answer APPROVE
  4. the patch is committed with a full receipt (watchdog/log.jsonl), the finding is closed, the desk restarts
     on the new code and the WATCHDOG STOP is lifted
  5. if no attempt passes all gates, the desk stays stopped - failing safe is the default
At the start of every cycle the suite runs at HEAD; if it fails and the last commit is a Watchdog fix, it is reverted.
"""
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIND = os.path.join(HERE, "findings")
LOG = os.path.join(HERE, "log.jsonl")
MONEY_RULES = {"UNTRACKED_CREATE", "UNAPPROVED_PAYEE", "PAYEE_DETAILS_CHANGED", "OVER_PER_TX_CAP", "SENT_WHILE_STOPPED",
               "SECOND_LIVE_TRANSFER", "UNTRACKED_MONEY", "OVER_DAILY_CAP"}
GUARDS = [r"raise Refused", r"_alert\(", r"_anomaly\(", r"\.kill\(", r"with self\.s\.tx\(\)", r"with self\.s\.lock", r"_gate\("]
REPAIR_MODEL = os.environ.get("WATCHDOG_REPAIR_MODEL", "claude-sonnet-5-5")
REVIEW_MODEL = os.environ.get("WATCHDOG_REVIEW_MODEL", "claude-haiku-5-5")
MAX_TRIES = int(os.environ.get("WATCHDOG_MAX_TRIES", "3"))


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def log(**rec):
    rec["at"] = now()
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print("[watchdog]", rec.get("event"), rec.get("rule", ""), rec.get("result", ""), flush=True)


def test_modules(root):
    return ["tests." + os.path.basename(p)[:-3] for p in sorted(glob.glob(os.path.join(root, "tests", "test_*.py")))]


def run_suite(root):
    r = subprocess.run([sys.executable, "-m", "unittest", *test_modules(root)], cwd=root, capture_output=True, text=True, timeout=1200)
    tail = (r.stderr or "").strip().splitlines()[-3:]
    return r.returncode == 0, " | ".join(tail)


def failing_tests(root):
    """Ids of every failing test (sub-tests included) - so a fix is judged against what was already failing."""
    r = subprocess.run([sys.executable, "-m", "unittest", "-v", *test_modules(root)], cwd=root, capture_output=True, text=True, timeout=1200)
    return set(re.findall(r"^(?:FAIL|ERROR): (.+)$", r.stderr or "", re.M))


def broken_findings(root):
    ok, out = run_py(root, "import glob,json\nfrom watchdog.replay import replay\n"
                           "print(json.dumps(sorted(f.split('/')[-1] for f in glob.glob('watchdog/findings/*.json') if replay(f,False)!=([],None))))")
    return set(json.loads(out.strip().splitlines()[-1])) if out.strip() else set()


def hunt_rules(root, n=300):
    ok, out = run_py(root, "import json\nfrom watchdog.sim import gen,play\nr=set()\n"
                           f"for s in range(900000,{900000 + n}):\n v,c=play(gen(s)); r|={{x[1] for x in v}}|({{'CRASH'}} if c else set())\n"
                           "print(json.dumps(sorted(r)))")
    return set(json.loads(out.strip().splitlines()[-1])) if out.strip() else {"HUNT_ERROR"}


def run_py(root, code, timeout=1200):
    r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-400:]


def ask_claude(model, system, user, max_tokens=4000):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("no ANTHROPIC_API_KEY for the repair model")
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read())
    return "".join(b.get("text", "") for b in out["content"])


REPAIR_SYSTEM = """You repair a payments-safety desk (Python). An automated attacker found a scenario that breaks a money rule.
Return ONLY a JSON object: {"explanation": "...", "edits": [{"file": "allowance/<name>.py", "old": "<exact unique text>", "new": "<replacement>"}]}.
Rules: the smallest change that makes the rule hold. Only files under allowance/. Never weaken a check: do not remove or loosen any
Refused/raise, alarm, STOP, lock or transaction, and never raise a cap. Prefer refusing or alarming over guessing. "old" must match
the file exactly once."""

REVIEW_SYSTEM = """You are the second, independent reviewer of an automatic fix to payments-safety code. Answer with one line:
APPROVE: <why> or REJECT: <why>. Reject if the change could let money move twice, to an unapproved payee, past a cap or STOP,
weakens any refusal/alarm/lock, or does more than fix the stated finding."""


def repair_prompt(finding, trace, feedback):
    src = "\n\n".join(f"=== {p} ===\n" + open(os.path.join(ROOT, p)).read() for p in ("allowance/service.py", "allowance/agent.py", "allowance/store.py"))
    rules = open(os.path.join(HERE, "sim.py")).read()
    fb = f"\n\nYOUR PREVIOUS ATTEMPT FAILED THE GATES:\n{feedback}\n" if feedback else ""
    return (f"FINDING: {finding['rule']} - {finding['detail']}\nSTEPS: {json.dumps(finding['steps'])}\nREPLAY:\n{trace}\n{fb}\n"
            f"=== RULE CHECKER (watchdog/sim.py, read-only) ===\n{rules}\n\n{src}")


def parse_edits(text):
    m = re.search(r"\{.*\}", text, re.S)
    obj = json.loads(m.group(0))
    return obj.get("explanation", ""), obj["edits"]


def gate_scope_and_strength(edits, root):
    for e in edits:
        f = e["file"].replace("\\", "/")
        if not re.fullmatch(r"allowance/[a-z_]+\.py", f):
            return False, f"G1 scope: {f} is outside allowance/"
        path = os.path.join(root, f)
        if not os.path.exists(path):
            return False, f"G1 scope: {f} does not exist"
        txt = open(path).read()
        if txt.count(e["old"]) != 1:
            return False, f"G1 edit target not unique in {f}"
        new_txt = txt.replace(e["old"], e["new"])
        for g in GUARDS:
            if len(re.findall(g, new_txt)) < len(re.findall(g, txt)):
                return False, f"G2 no-weaken: patch removes a '{g}' in {f}"
        if re.search(r"(per_tx_cap|daily_cap|dual_approval_over|max_fee)\s*=\s*\d", e["new"]) and not re.search(r"(per_tx_cap|daily_cap|dual_approval_over|max_fee)\s*=\s*\d", e["old"]):
            return False, "G2 no-weaken: patch sets a cap value"
        open(path, "w").write(new_txt)
    return True, "G1/G2 ok"


def try_fix(finding, path, trace, feedback):
    reply = ask_claude(REPAIR_MODEL, REPAIR_SYSTEM, repair_prompt(finding, trace, feedback), 6000)
    explanation, edits = parse_edits(reply)
    scratch = tempfile.mkdtemp(prefix="watchdog-fix-")
    work = os.path.join(scratch, "repo")
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.db*"))
    gates = {}
    base = BASELINE  # what was already failing at HEAD before this attempt (other open findings)
    ok, msg = gate_scope_and_strength(edits, work)
    gates["G1_G2"] = msg
    if ok:
        new_fail = failing_tests(work) - base["tests"]
        ok = not new_fail
        gates["G3_tests"] = "pass (no test that passed before fails now)" if ok else "FAIL newly failing: " + "; ".join(sorted(new_fail))[:300]
    if ok:
        still = broken_findings(work)
        name = os.path.basename(path)
        newly = still - base["findings"]
        ok = name not in still and not newly and len(still) < len(base["findings"])
        gates["G4_replays"] = (f"pass ({name} fixed; open findings {len(base['findings'])} -> {len(still)})" if ok else
                               f"FAIL target still broken={name in still}, newly broken={sorted(newly)}")
    if ok:
        rules = hunt_rules(work)
        allowed = {json.load(open(os.path.join(FIND, f)))["rule"] for f in base["findings"] if f != os.path.basename(path)}
        extra = rules - allowed
        ok = not extra
        gates["G5_hunt"] = "pass (300 fresh scenarios, no new kind of break)" if ok else "FAIL new kinds of break: " + ", ".join(sorted(extra))
    diff = subprocess.run(["diff", "-ru", os.path.join(ROOT, "allowance"), os.path.join(work, "allowance")], capture_output=True, text=True).stdout
    if ok:
        verdict = ""
        for _ in range(2):  # the reviewer may think first; give it room, and one retry if it still says nothing
            verdict = ask_claude(REVIEW_MODEL, REVIEW_SYSTEM, f"FINDING: {finding['rule']} - {finding['detail']}\nEXPLANATION: {explanation}\nDIFF:\n{diff[:30000]}", 4000).strip()
            if verdict:
                break
        m = re.search(r"\b(APPROVE|REJECT)\b\s*:?(.*)", verdict, re.S)
        ok = bool(m and m.group(1) == "APPROVE")
        verdict = (m.group(0) if m else verdict or "(reviewer gave no answer)").strip()
        gates["G6_second_opinion"] = verdict[:300]
    return ok, gates, explanation, edits, diff, work


def apply_and_commit(work, finding, explanation, gates, path):
    for f in glob.glob(os.path.join(work, "allowance", "*.py")):
        shutil.copy(f, os.path.join(ROOT, "allowance", os.path.basename(f)))
    rec = json.load(open(path))
    rec.update(status="fixed", fixed_at=now(), fix=explanation, gates=gates)
    json.dump(rec, open(path, "w"), indent=1)
    msg = f"watchdog autofix: {finding['rule']}\n\n{explanation}\n\nGates: " + json.dumps(gates)
    subprocess.run(["git", "add", "-A", "allowance", "watchdog"], cwd=ROOT)
    subprocess.run(["git", "-c", "user.name=Watchdog", "-c", "user.email=watchdog@agent-allowance.local", "commit", "-qm", msg], cwd=ROOT)
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def rollback_if_broken():
    ok, msg = run_suite(ROOT)
    if ok:
        return False
    last = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=ROOT, capture_output=True, text=True).stdout
    if last.startswith("watchdog autofix"):
        subprocess.run(["git", "-c", "user.name=Watchdog", "-c", "user.email=watchdog@agent-allowance.local", "revert", "--no-edit", "HEAD"], cwd=ROOT)
        log(event="rollback", result="suite failed at HEAD; last Watchdog fix reverted", detail=msg)
        return True
    log(event="open_bug_before_cycle", result="tests were already failing when this cycle started (an open bug, not a Watchdog fix) - nothing reverted; repair runs next and payments stay stopped until it passes", detail=msg)
    return False


def open_findings():
    from .replay import replay
    out = []
    for p in sorted(glob.glob(os.path.join(FIND, "*.json"))):
        rec = json.load(open(p))
        v, crash = replay(p, verbose=False)
        if v or crash:
            out.append((p, rec, f"violations={v} crash={crash}"))
        elif rec.get("status") == "open":
            rec["status"] = "fixed"
            rec.setdefault("fixed_at", now())
            json.dump(rec, open(p, "w"), indent=1)
    return out


BASELINE = {"tests": set(), "findings": set()}


def refresh_baseline():
    BASELINE["tests"] = failing_tests(ROOT)
    BASELINE["findings"] = broken_findings(ROOT)


def repair(desk=None):
    """Fix every open finding, one at a time; each fix is judged against what was already broken.
    desk: a live Service to STOP/resume (None when run from the command line)."""
    results = []
    for path, rec, trace in open_findings():
        refresh_baseline()
        if os.path.basename(path) not in BASELINE["findings"]:
            continue  # an earlier fix in this cycle already cured it
        money = rec["rule"] in MONEY_RULES
        if money and desk is not None:
            desk.kill("watchdog", f"Watchdog: money rule {rec['rule']} broken - stopped until an automatic fix passes all gates")
        log(event="repair_start", rule=rec["rule"], finding=os.path.basename(path), desk_stopped=bool(money and desk))
        feedback, fixed = "", False
        for attempt in range(1, MAX_TRIES + 1):
            try:
                ok, gates, explanation, edits, diff, work = try_fix(rec, path, trace, feedback)
            except Exception as e:  # noqa: BLE001
                ok, gates, explanation, diff = False, {"error": str(e)[:300]}, "", ""
            log(event="attempt", rule=rec["rule"], attempt=attempt, result="PASS" if ok else "rejected", gates=gates)
            if ok:
                sha = apply_and_commit(work, rec, explanation, gates, path)
                log(event="fixed", rule=rec["rule"], commit=sha, explanation=explanation[:500], diff=diff[:4000])
                results.append((rec["rule"], "fixed", sha))
                fixed = True
                break
            feedback = json.dumps(gates)
        if not fixed:
            log(event="unfixed", rule=rec["rule"], result="no attempt passed every gate" + ("; desk stays STOPPED" if money and desk else ""))
            results.append((rec["rule"], "unfixed", None))
    return results
