"""Round-4 independent review (Nov 2): each finding as a test."""
import os
import tempfile
import unittest

from tests.test_allowance import Base
from tests.test_allowance import bank
from allowance.service import Refused


class R4(Base):
    def test_supplier_message_must_be_text(self):
        iid = self.incident()
        with self.assertRaises(Refused):
            self.svc.supplier_message(iid, ["Our bank changed - send to new account 55667788"])
        self.assertIsNone(self.svc.s.one("SELECT 1 FROM events WHERE kind='supplier_message' AND incident_id=?", iid))

    def test_approval_lasts_at_most_15_minutes(self):
        iid = self.incident()
        for bad in (86400, 901, 0, -5):
            with self.assertRaises(Refused):
                self.svc.approve(iid, "original", "zach", ttl=bad)
        self.svc.approve(iid, "original", "zach")          # default 15 min is fine

    def test_unreadable_fee_still_records_status_and_alarms(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fake.by_req[p["request_id"]]["fee_amount"] = ""
        out = self.svc.reconcile(p["request_id"])
        self.assertEqual(out["status_source"], "provider_get")
        self.assertEqual(self.svc.s.one("SELECT alert FROM incidents WHERE id=?", iid)["alert"], "PROVIDER_MISMATCH")

    def test_anomaly_re_alarms_after_it_was_cleared(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.svc._anomaly(p["request_id"], "CANCELLED_BUT_LIVE", "x")
        self.svc.s.db.execute("UPDATE incidents SET alert=NULL, state='OPEN' WHERE id=?", (iid,))   # someone cleared it
        self.svc._anomaly(p["request_id"], "CANCELLED_BUT_LIVE", "x")
        self.assertEqual(self.svc.s.one("SELECT alert FROM incidents WHERE id=?", iid)["alert"], "CANCELLED_BUT_LIVE")


class R4FX(Base):
    def setUp(self):
        super().setUp()
        self.fake.now = lambda: self.t
        self.svc.s.db.execute("UPDATE policy SET fx_currencies='EUR', allowed_countries='US,DE'")
        d = bank(name="Holz Werk GmbH")
        d["beneficiary"]["bank_details"].update({"account_currency": "EUR", "bank_country_code": "DE", "iban": "DE89370400440532013000"})
        self.fake.bens["ben-eur"] = d
        self.svc.allow_beneficiary("ben-eur", "Holz Werk GmbH", "IBAN ****3000", "zach")

    def eur_incident(self):
        Base.n += 1
        return self.svc.open_incident("Holz Werk", f"EU-R4-{Base.n}", "ben-eur", 25_000, "EUR", deadline=self.t + 3600, allow_similar=True)

    def pay_eur(self, iid):
        return self.svc.pay(self.svc.approve(iid, "original", "zach"), "original", "ben-eur", 25_000, "EUR")

    def test_quote_without_mid_rate_refused(self):
        orig = self.fake.fx_quote
        def no_mid(*a, **k):
            q = orig(*a, **k); q.pop("mid_rate", None); return q
        self.fake.fx_quote = no_mid
        iid = self.eur_incident()
        with self.assertRaisesRegex(Refused, "mid-market"):
            self.pay_eur(iid)
        self.assertEqual(self.fake.calls, 0)

    def test_balance_precheck_uses_dollar_cost(self):
        self.fake.balance = 27_500          # USD 275 wallet; EUR 250 costs USD 281.46
        iid = self.eur_incident()
        with self.assertRaisesRegex(Refused, "would not cover"):
            self.pay_eur(iid)
        self.assertEqual(self.fake.calls, 0)


class R4Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("ALLOWANCE_DB", os.path.join(tempfile.mkdtemp(), "r4.db"))
        os.environ.setdefault("OPERATOR_TOKENS", "zach:zach,jordan:jordan")
        os.environ.setdefault("DEMO", "1")
        import server
        cls.server = server

    def test_body_must_be_object_and_fields_text(self):
        h = self.server.handle
        with self.assertRaises(ValueError):
            h("POST", "/api/kill", ["x"], "zach")
        with self.assertRaises(ValueError):
            h("POST", "/api/kill", {"reason": {"a": 1}}, "zach")

    def test_state_hides_account_numbers(self):
        st = self.server.handle("GET", "/api/state", {}, "zach")
        for b in st["beneficiaries"]:
            self.assertNotIn("account_key", b)
            self.assertNotIn("details_hash", b)


if __name__ == "__main__":
    unittest.main()
