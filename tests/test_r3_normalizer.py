"""Round-three independent normalizer/privacy specifications. Offline; no application edits."""
import base64
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
bundle = os.environ.get("ALLOWANCE_REVIEW_BUNDLE", str(Path(__file__).resolve().parents[1] / "airwallex-verify-2a6c67c"))
sys.path.insert(0, bundle)
from tests.test_allowance import Base
from allowance import agent as A

class NormalizerReview(Base):
    def cancelled(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
        self.t += 2
        return iid

    def test_prior_forms_are_detected_and_redacted(self):
        for benign in ["Thanks for checking; please send an update tomorrow.", "Invoice maple invoice review completed."]:
            self.assertEqual(A.fraud_signals([benign]), [])
            self.assertEqual(A.redact(benign), benign)
        for message in [
            "Use the ne\u200bw ba\u200bnk details for this invoice.",
            "Use the n&#101;w b&#97;nk details for this invoice.",
            "Use the n**ew** b**ank** details for this invoice.",
            "Use the n<b>ew</b> b<b>ank</b> details for this invoice.",
            "Destination: gb82 west 1234 5698 7654 32",
        ]:
            with self.subTest(message=message):
                self.assertTrue(A.fraud_signals([message]))
        self.assertEqual(A.redact("YWNjb3VudCA1NTY2Nzc4OA=="), "[encoded text removed]")
        for message in ["account 5.5.6.6.7.7.8.8", "account &#53;&#53;&#54;&#54;&#55;&#55;&#56;&#56;"]:
            self.assertNotIn("55667788", A.redact(message))
            self.assertIn("[number removed]", A.redact(message))

    def test_encoded_html_separators_and_long_inline_tags_escalate(self):
        messages = [
            "Use the new&lt;br&gt;bank details for this invoice.",
            "Use the new&amp;lt;br&amp;gt;bank details for this invoice.",
            'Use the n<span class="this-is-a-long-but-ordinary-email-style-class">ew</span> bank details for this invoice.',
        ]
        for message in messages:
            with self.subTest(message=message):
                iid = self.cancelled()
                self.svc.supplier_message(iid, message)
                ev = self.svc.evidence(iid)
                allowed = self.svc.allowed_actions(iid)
                decision, _ = A.RulesAgent().decide(ev, allowed)
                print(json.dumps({"message":message, "normalized":A.normalize(message),
                                  "hits":A.fraud_signals([message]), "alert":ev["incident"].get("alert"),
                                  "allowed":allowed, "decision":decision}))
                self.assertEqual(decision, "ESCALATE")

    def test_decoded_right_parenthesis_does_not_leak_account_words(self):
        raw = "note) account five five six six seven seven eight eight"
        encoded = base64.b64encode(raw.encode()).decode()
        iid = self.cancelled()
        self.svc.supplier_message(iid, encoded)
        digest = A.digest(self.svc.evidence(iid), self.svc.allowed_actions(iid))
        print(json.dumps({"case":"base64 right parenthesis", "encoded":encoded, "digest":digest}))
        self.assertNotIn("five five six six seven seven eight eight", json.dumps(digest))

    def test_multiline_encoded_account_not_in_model_payload(self):
        encoded = base64.b64encode(b"account 55667788\nrouting 021000021").decode()
        iid = self.cancelled()
        self.svc.supplier_message(iid, encoded)
        digest = A.digest(self.svc.evidence(iid), self.svc.allowed_actions(iid))
        print(json.dumps({"case":"base64 multiline", "encoded":encoded, "digest":digest}))
        self.assertNotIn(encoded, json.dumps(digest))

if __name__ == "__main__":
    unittest.main(verbosity=2)

