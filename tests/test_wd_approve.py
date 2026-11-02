"""Watchdog fixes go live only when a person taps Apply (default WATCHDOG_APPLY=approve)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("ALLOWANCE_DB", os.path.join(tempfile.mkdtemp(), "wd.db"))
os.environ.setdefault("OPERATOR_TOKENS", "zach:zach,jordan:jordan")
os.environ.setdefault("DEMO", "1")
import server  # noqa: E402


class WatchdogApprove(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(server, "_wd_dir", return_value=self.d)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_default_mode_is_approve(self):
        self.assertEqual(server.WATCHDOG_APPLY, os.environ.get("WATCHDOG_APPLY", "approve"))

    def test_apply_refused_when_nothing_waiting(self):
        server._wd_set_state("green")
        with self.assertRaises(server.Refused):
            server.handle("POST", "/api/watchdog/apply", {}, "zach")

    def test_apply_logs_and_restarts(self):
        server._wd_set_state("awaiting_approval", fixed=[["SCAM_NOT_ESCALATED", "fixed", "abc1234"]])
        with mock.patch.object(server.threading, "Timer") as T:
            out = server.handle("POST", "/api/watchdog/apply", {}, "zach")
        self.assertEqual(out["applied"], ["abc1234"])
        T.assert_called_once()
        self.assertEqual(server.watchdog_status()["state"], "repaired")
        self.assertTrue(server.SVC.s.one("SELECT 1 FROM events WHERE kind='watchdog_fix_applied' AND provenance='operator'"))

    def test_reject_reverts_and_keeps_stop(self):
        server._wd_set_state("awaiting_approval", fixed=[["A", "fixed", "aaa1111"], ["B", "fixed", "bbb2222"]])
        with mock.patch.object(server.subprocess, "run") as run:
            out = server.handle("POST", "/api/watchdog/reject", {}, "jordan")
        self.assertEqual(out["reverted"], ["aaa1111", "bbb2222"])
        self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["bbb2222", "aaa1111"])   # newest first
        self.assertEqual(server.watchdog_status()["state"], "rejected")


if __name__ == "__main__":
    unittest.main()
