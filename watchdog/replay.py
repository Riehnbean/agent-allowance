"""python3 -m watchdog.replay watchdog/findings/<file>.json  -- replays one saved finding step by step."""
import json
import sys

from .sim import play


def replay(path, verbose=True):
    rec = json.load(open(path))
    steps = [tuple(_t(s)) for s in rec["steps"]]
    v, crash = play(steps)
    if verbose:
        for i, s in enumerate(steps):
            print(f"{i:>2} {s}")
        print("violations:", v or "none", "| crash:", crash or "none")
    return v, crash


def _t(s):
    return [tuple(_t(x)) if isinstance(x, list) else x for x in s]


if __name__ == "__main__":
    v, c = replay(sys.argv[1])
    sys.exit(1 if (v or c) else 0)
