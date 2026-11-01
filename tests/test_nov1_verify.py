"""Offline independent re-verification. No application edits, network, or real payments."""
import base64
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, os.environ.get("ALLOWANCE_REVIEW_BUNDLE", str(Path(__file__).parent / "airwallex-verify-3bbba0f")))
from tests.test_allowance import Base, BEN
from allowance import agent as A
from allowance.adapter import Ambiguous, ProviderError
from allowance.service import Refused

class Verify(Base):
    def cancelled(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
        self.t += 2
        return iid, p

    def test_failed_empty_and_error_get_cannot_use_cached_cancelled(self):
        for reply in [None, {}, Ambiguous("timeout"), ProviderError(503, "down", "unavailable")]:
            with self.subTest(reply=repr(reply)):
                iid, p = self.cancelled()
                ap = self.svc.approve(iid, "replacement", "zach")
                self.fake.by_req[p["request_id"]]["status"] = "PAID"
                count = self.fake.calls
                kw = {"side_effect": reply} if isinstance(reply, Exception) else {"return_value": reply}
                with patch.object(self.fake, "get_transfer", **kw):
                    try:
                        result = self.svc.pay(ap, "replacement", BEN, 25000, "USD")
                    except (Refused, KeyError, TypeError):
                        result = None
                    print("STRICT GET", repr(reply), "result", result and result["status"], "create delta", self.fake.calls-count)
                    self.assertIsNone(result, "invalid GET must refuse replacement")
                self.assertEqual(self.fake.calls, count)

    def test_new_destination_disguises_must_escalate(self):
        messages = [
            "Use the ne\u200bw ba\u200bnk details for this invoice.",
            "Use the n&#101;w b&#97;nk details for this invoice.",
            "Use the n**ew** b**ank** details for this invoice.",
            "Use the n<b>ew</b> b<b>ank</b> details for this invoice.",
            "Please remit to a\u200bc\u200bc\u200bo\u200bu\u200bn\u200bt 5.5.6.6.7.7.8.8 instead.",
            "Destination: gb82 west 1234 5698 7654 32",
        ]
        for msg in messages:
            with self.subTest(message=msg):
                iid, _ = self.cancelled()
                self.svc.supplier_message(iid, msg)
                decision, _ = A.RulesAgent().decide(self.svc.evidence(iid), self.svc.allowed_actions(iid))
                print(json.dumps({"message":msg, "normalized":A.normalize(msg), "hits":A.fraud_signals([msg]), "decision":decision}))
                self.assertEqual(decision, "ESCALATE")

    def test_encoded_original_is_not_left_in_model_digest(self):
        secret = base64.b64encode(b"account 55667788").decode()
        iid = self.incident()
        self.svc.supplier_message(iid, "Payment details: " + secret)
        payload = A.digest(self.svc.evidence(iid), self.svc.allowed_actions(iid))
        print("DIGEST", json.dumps(payload))
        self.assertNotIn(secret.rstrip("="), json.dumps(payload))

    def test_lowercase_and_entity_account_redaction(self):
        for text, surviving in [
            ("gb82 west 1234 5698 7654 32", "gb82 west"),
            ("account &#53;&#53;&#54;&#54;&#55;&#55;&#56;&#56;", "&#53;"),
            ("account 5.5.6.6.7.7.8.8", "5.5.6.6.7.7.8.8"),
        ]:
            with self.subTest(text=text):
                print("REDACT", repr(text), "=>", repr(A.redact(text)))
                self.assertNotIn(surviving, A.redact(text))

    def test_evidence_check_and_decision_append_are_atomic(self):
        iid = self.incident()
        self.pay_original(iid)
        evidence = self.svc.evidence(iid)
        reached, proceed = threading.Event(), threading.Event()
        old_log = self.svc.s.log
        errors = []
        def paused_log(actor, kind, *args, **kwargs):
            if actor == "agent" and kind == "decision":
                reached.set()
                if not proceed.wait(5):
                    raise RuntimeError("test synchronizer timed out")
            return old_log(actor, kind, *args, **kwargs)
        def record():
            try:
                self.svc.record_decision(iid, "WAIT", ["no alarm in old snapshot"], "decided", evidence=evidence)
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.svc.s, "log", side_effect=paused_log):
            worker = threading.Thread(target=record)
            worker.start()
            self.assertTrue(reached.wait(5))
            self.svc.supplier_message(iid, "Use our new bank account instead")
            proceed.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(all(isinstance(e, Refused) for e in errors), errors)
        decisions = self.svc.s.q("SELECT body FROM events WHERE incident_id=? AND actor='agent' AND kind='decision'", iid)
        print("POST-CHECK RACE", len(decisions), "decisions", [str(e) for e in errors])
        self.assertEqual(len(decisions), 0, "stale WAIT appended after new FRAUD_SIGNAL")

    def test_reported_latency_includes_refresh(self):
        iid = self.incident()
        self.pay_original(iid)
        elapsed = [1000.0]
        old_reconcile = self.svc.reconcile
        def delayed(rid):
            elapsed[0] += 2.0
            return old_reconcile(rid)
        with patch.object(A.time, "time", side_effect=lambda:elapsed[0]), patch.object(self.svc, "reconcile", side_effect=delayed):
            result = A.run(self.svc, iid, A.RulesAgent())
        self.assertEqual(result["ms"], 2000)

    def test_resend_rejected_at_exact_expiry(self):
        iid = self.incident()
        ap = self.svc.approve(iid, "original", "zach", ttl=1)
        self.fake.lose_response_next = True
        p = self.svc.pay(ap, "original", BEN, 25000, "USD")
        self.t += 1
        before = self.fake.calls
        with self.assertRaises(Refused):
            self.svc.resend(p["request_id"], "zach")
        self.assertEqual(self.fake.calls, before)

    def test_repeated_changes_escalate(self):
        iid = self.incident()
        self.pay_original(iid)
        svc = self.svc
        class Changing:
            calls = 0
            def decide(self, ev, allowed):
                self.calls += 1
                svc.supplier_message(iid, "Any update " + str(self.calls))
                return "WAIT", ["old snapshot"]
        brain = Changing()
        result = A.run(self.svc, iid, brain)
        self.assertEqual(brain.calls, 2)
        self.assertEqual(result["decision"], "ESCALATE")

if __name__ == "__main__":
    unittest.main(verbosity=2)

