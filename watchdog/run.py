"""python3 -m watchdog.run [--minutes 5] [--seeds 0:2000] [--propose]

Plays generated attack scenarios against a throwaway desk. Each broken money rule is shrunk to the
shortest step list that still breaks it and saved to watchdog/findings/<rule>-<seed>.json, plus a
line in watchdog/findings/LATEST.md. Exit code 1 if anything was found (so a scheduler can alert)."""
import argparse
import datetime
import hashlib
import json
import os
import sys
import time

from .sim import gen, play

HERE = os.path.dirname(os.path.abspath(__file__))
FIND = os.path.join(HERE, "findings")


def fails(steps, rule):
    for _ in range(2 if any(s[0] == "race" for s in steps) else 1):  # races get a second chance to show
        v, crash = play(steps)
        if rule.startswith("CRASH") and crash:
            return True
        if any(x[1] == rule for x in v):
            return True
    return False


def shrink(steps, rule):
    """Remove steps while the same rule still breaks (simple delta debugging)."""
    cur = list(steps)
    chunk = max(1, len(cur) // 2)
    while chunk >= 1:
        i, changed = 1, False  # keep step 0 (the first incident)
        while i < len(cur):
            trial = cur[:i] + cur[i + chunk:]
            if trial and fails(trial, rule):
                cur, changed = trial, True
            else:
                i += chunk
        if not changed:
            chunk //= 2
    return cur


def save(rule, seed, steps, detail):
    os.makedirs(FIND, exist_ok=True)
    key = hashlib.sha256(json.dumps([rule, steps]).encode()).hexdigest()[:8]
    path = os.path.join(FIND, f"{rule}-{key}.json")
    rec = {"rule": rule, "seed": seed, "detail": detail, "steps": steps,
           "found_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "status": "open", "replay": f"python3 -m watchdog.replay {os.path.relpath(path, os.path.dirname(HERE))}"}
    if not os.path.exists(path):
        json.dump(rec, open(path, "w"), indent=1)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=2)
    ap.add_argument("--seeds", default=None, help="a:b range instead of time-boxed random seeds")
    ap.add_argument("--steps", type=int, default=25)
    ap.add_argument("--propose", action="store_true", help="ask a model to draft fixes (never applied)")
    a = ap.parse_args(argv)
    if a.seeds:
        lo, hi = map(int, a.seeds.split(":"))
        seeds = iter(range(lo, hi))
    else:
        base = int(time.time())
        seeds = (base + i for i in range(10**9))
    end = time.time() + a.minutes * 60
    seen, ran, found = set(), 0, []
    for seed in seeds:
        if not a.seeds and time.time() > end:
            break
        steps = gen(seed, a.steps)
        v, crash = play(steps)
        ran += 1
        hits = [(x[1], x[2]) for x in v] + ([("CRASH", crash[1])] if crash else [])
        for rule, detail in hits[:1]:
            if rule in seen:
                continue
            seen.add(rule)
            small = shrink(steps, rule)
            path = save(rule, seed, small, detail)
            found.append((rule, len(small), path, detail))
            print(f"FOUND {rule} (seed {seed}, {len(small)} steps): {detail}\n  -> {path}", flush=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    os.makedirs(FIND, exist_ok=True)
    with open(os.path.join(FIND, "LATEST.md"), "w") as f:
        f.write(f"# Watchdog run {stamp}\n\n{ran} attack scenarios, {len(found)} new rule break(s).\n\n")
        for rule, n, path, detail in found:
            f.write(f"- **{rule}** ({n} steps): {detail} -- `{os.path.basename(path)}`\n")
    print(f"{ran} scenarios, {len(found)} finding(s)")
    if found and a.propose:
        from .propose import propose_all
        propose_all()
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
