"""Paying a supplier in another currency through a locked Airwallex FX quote. Caps are in the home
currency (USD) and are checked on the locked dollar cost, never on the face value of the invoice."""
import unittest

from tests.test_allowance import Base, bank
from allowance.service import Refused

EUR_BEN = "ben-eur"


class FX(Base):
    def setUp(self):
        super().setUp()
        self.fake.now = lambda: self.t
        self.svc.s.db.execute("UPDATE policy SET fx_currencies='EUR', allowed_countries='US,DE'")
        d = bank(name="Holz Werk GmbH")
        d["beneficiary"]["bank_details"].update({"account_currency": "EUR", "bank_country_code": "DE", "iban": "DE89370400440532013000"})
        self.fake.bens[EUR_BEN] = d
        self.svc.allow_beneficiary(EUR_BEN, "Holz Werk GmbH", "IBAN ****3000", "zach")

    def eur_incident(self, amount=25_000, invoice=None):
        Base.n += 1
        return self.svc.open_incident("Holz Werk", invoice or f"EU-{Base.n}", EUR_BEN, amount, "EUR", deadline=self.t + 3600, allow_similar=True)

    def pay_eur(self, iid, amount=25_000):
        ap = self.svc.approve(iid, "original", "zach")
        return self.svc.pay(ap, "original", EUR_BEN, amount, "EUR")

    def test_eur_payment_uses_locked_quote_and_counts_dollar_cost(self):
        iid = self.eur_incident(25_000)                     # EUR 250.00
        p = self.pay_eur(iid)
        self.assertEqual(p["status"], "SCHEDULED")
        self.assertTrue(p["fx_quote_id"])
        self.assertEqual(p["cost_amount"], round(25_000 * self.fake.fx_rate))   # USD 281.46 locked
        t = self.fake.by_req[p["request_id"]]
        self.assertEqual((t["source_currency"], t["transfer_currency"], t["quote_id"]), ("USD", "EUR", p["fx_quote_id"]))
        self.assertGreaterEqual(self.svc.s.committed_today(self.t), p["cost_amount"])  # budget holds dollars, not euros

    def test_caps_apply_to_dollar_cost_not_face_value(self):
        self.svc.s.db.execute("UPDATE policy SET per_tx_cap=27000")    # USD 270 cap
        iid = self.eur_incident(25_000)                                 # EUR 250 = USD 281.46 > cap
        with self.assertRaisesRegex(Refused, "per-payment cap"):
            self.pay_eur(iid)
        self.assertEqual(self.fake.calls, 0)

    def test_cosign_threshold_on_dollar_cost(self):
        iid = self.eur_incident(45_000)          # EUR 450 = USD 506.63 > USD 500 co-sign line
        with self.assertRaisesRegex(Refused, "co-sign"):
            self.pay_eur(iid, 45_000)

    def test_currency_not_on_list_refused(self):
        self.svc.s.db.execute("UPDATE policy SET fx_currencies=''")
        iid = self.eur_incident()
        with self.assertRaisesRegex(Refused, "not an allowed payment currency"):
            self.pay_eur(iid)
        self.assertEqual(self.fake.calls, 0)

    def test_excessive_markup_refused(self):
        self.fake.fx_rate = self.fake.fx_mid * 1.03      # 3% over mid-market, policy allows 1.5%
        iid = self.eur_incident()
        with self.assertRaisesRegex(Refused, "markup"):
            self.pay_eur(iid)
        self.assertEqual(self.fake.calls, 0)

    def test_quote_for_wrong_amount_refused(self):
        real = self.fake.fx_quote
        self.fake.fx_quote = lambda s, b, amt, validity="MIN_15": real(s, b, amt + 100, validity)
        iid = self.eur_incident()
        with self.assertRaisesRegex(Refused, "does not match"):
            self.pay_eur(iid)

    def test_incomplete_quote_refused(self):
        self.fake.fx_quote = lambda *a, **k: {"quote_id": "q1", "sell_currency": "USD"}
        iid = self.eur_incident()
        with self.assertRaisesRegex(Refused, "incomplete"):
            self.pay_eur(iid)
        self.assertEqual(self.fake.calls, 0)

    def test_quote_about_to_expire_refused(self):
        self.fake.fx_valid = 30
        iid = self.eur_incident()
        with self.assertRaisesRegex(Refused, "under a minute"):
            self.pay_eur(iid)

    def test_quote_expiring_before_send_sends_nothing(self):
        iid = self.eur_incident()
        real = self.fake.get_beneficiary

        def slow(bid):                       # pre-send checks take longer than the quote lasts
            self.t += 1000
            return real(bid)
        self.fake.get_beneficiary = slow
        with self.assertRaisesRegex(Refused, "exchange rate expired"):
            self.pay_eur(iid)
        self.assertEqual(self.fake.calls, 0)
        self.assertEqual(self.svc.s.committed_today(self.t), 0)   # reservation released

    def test_overcharge_vs_locked_cost_alarms(self):
        iid = self.eur_incident()
        real = self.fake.create_transfer

        def overcharge(*a, **k):
            t = real(*a, **k)
            t["amount_payer_pays"] = t["amount_payer_pays"] + 50   # USD 50 more than the locked quote
            return t
        self.fake.create_transfer = overcharge
        p = self.pay_eur(iid)
        self.assertEqual(self.svc._incident(iid)["alert"], "FEE_OVER_LIMIT")
        self.assertTrue(p["request_id"])

    def test_home_currency_path_unchanged(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.assertIsNone(p["fx_quote_id"])
        self.assertEqual(p["cost_amount"], p["amount"])
        self.assertNotIn("quote_id", {k for k, v in self.fake.by_req[p["request_id"]].items() if v})


if __name__ == "__main__":
    unittest.main()
