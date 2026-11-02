"""Airwallex Verify Account runs before a SEPA/UK payee is allowlisted; INVALID accounts are refused."""
import os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from allowance.demo_provider import DemoAirwallex  # noqa: E402
from allowance.service import Refused, Service  # noqa: E402
from allowance.store import Store  # noqa: E402

DE = {"entity_type": "COMPANY", "company_name": "Holz Werk GmbH", "type": "BANK_ACCOUNT",
      "address": {"country_code": "DE", "city": "Berlin", "street_address": "Hauptstrasse 1", "postcode": "10115"},
      "bank_details": {"account_currency": "EUR", "account_name": "Holz Werk GmbH", "iban": "DE89370400440532013000", "bank_country_code": "DE"}}


class VerifyPayee(unittest.TestCase):
    def setUp(self):
        self.api = DemoAirwallex()
        self.svc = Service(Store(os.path.join(tempfile.mkdtemp(), "v.db")), self.api)
        self.svc.s.init_policy("USD")
        self.bid = self.api.create_beneficiary({"beneficiary": DE})["id"]

    def test_verified_payee_is_logged(self):
        self.svc.allow_beneficiary(self.bid, "Holz Werk GmbH", "IBAN ****3000", "zach")
        e = self.svc.s.one("SELECT body FROM events WHERE kind='payee_verified'")
        self.assertIn("FULL_MATCH", e["body"])

    def test_invalid_account_refused(self):
        self.api.verify_account = lambda et, bank: {"code": "INVALID", "details": {}}
        with self.assertRaises(Refused):
            self.svc.allow_beneficiary(self.bid, "Holz Werk GmbH", "IBAN ****3000", "zach")
        self.assertIsNone(self.svc.s.one("SELECT 1 FROM beneficiaries WHERE id=?", self.bid))

    def test_unavailable_does_not_block(self):
        def boom(*a):
            raise RuntimeError("503")
        self.api.verify_account = boom
        self.svc.allow_beneficiary(self.bid, "Holz Werk GmbH", "IBAN ****3000", "zach")
        self.assertTrue(self.svc.s.one("SELECT 1 FROM events WHERE kind='payee_verification_unavailable'"))

    def test_us_payee_not_sent_for_verification(self):
        calls = []
        self.api.verify_account = lambda *a: calls.append(a) or {"code": "VERIFIED"}
        us = dict(DE, bank_details={"account_currency": "USD", "account_name": "Maple", "account_number": "123456789", "bank_country_code": "US",
                                    "account_routing_type1": "aba", "account_routing_value1": "026009593"})
        bid = self.api.create_beneficiary({"beneficiary": us})["id"]
        self.svc.allow_beneficiary(bid, "Maple", "acct ****6789", "zach")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
