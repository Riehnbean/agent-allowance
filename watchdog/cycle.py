"""One full Watchdog cycle: roll back a bad fix if the suite is red -> hunt -> repair -> report.

python3 -m watchdog.cycle [--minutes 3]      (cron: hourly)
Inside the desk server it runs on its own thread every WATCHDOG_EVERY seconds (default 3600) and, after a fix
is committed, restarts the desk on the new code once no payment is mid-send, then lifts its own STOP."""
import argparse
import json
import os
import sys

from . import autofix
from .run import main as hunt


def cycle(minutes=3, desk=None):
    autofix.rollback_if_broken()
    hunt(["--minutes", str(minutes)])
    results = autofix.repair(desk)
    fixed = [r for r in results if r[1] == "fixed"]
    unfixed = [r for r in results if r[1] == "unfixed"]
    status = {"at": autofix.now(), "fixed": fixed, "unfixed": unfixed,
              "state": "red" if unfixed else ("repaired" if fixed else "green")}
    json.dump(status, open(os.path.join(autofix.HERE, "status.json"), "w"), indent=1)
    if desk is not None and not unfixed:
        pol = desk.s.policy()
        if pol["killed"] and pol["killed_by"] == "watchdog" and not fixed:
            desk.unkill("watchdog")  # nothing broken any more and no restart needed
    return status


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=3)
    a = ap.parse_args()
    st = cycle(a.minutes)
    print(json.dumps(st))
    sys.exit(1 if st["state"] == "red" else 0)
