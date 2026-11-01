"""Every finding the Watchdog ever saved is replayed here and must stay fixed, plus a short fresh hunt."""
import glob
import json
import os
import unittest

from watchdog.replay import replay
from watchdog.sim import gen, play

FIND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "watchdog", "findings")


class WatchdogRegressions(unittest.TestCase):
    def test_every_saved_finding_stays_fixed(self):
        files = sorted(glob.glob(os.path.join(FIND, "*.json")))
        for f in files:
            with self.subTest(finding=os.path.basename(f)):
                v, crash = replay(f, verbose=False)
                self.assertEqual((v, crash), ([], None), json.load(open(f))["detail"])

    def test_short_fresh_hunt_is_clean(self):
        for seed in range(150):
            v, crash = play(gen(10_000 + seed))
            self.assertEqual((v, crash), ([], None), f"seed {10_000 + seed}")


if __name__ == "__main__":
    unittest.main()
