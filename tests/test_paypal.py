"""PayPal Payouts rail: the same desk (service.py) on the PayPal adapter, against the in-memory PayPal fake.
No network, no keys."""
import os
import sys
import tempfile
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from allowance.adapter import Ambiguous, ProviderError, details_hash  # noqa: E402
from allowance.agent import RulesAgent, run  # noqa: E402
from allowance.paypal_adapter import PayeeBook, PayPalPayouts  # noqa: E402
from allowance.paypal_fake import FakePayPalAPI, demo_paypal  # noqa: E402
from allowance.service import DELIBERATE, DETAILS_PROBLEM, RETRYABLE, Refused, Service  # noqa: E402
from allowance.store import Store  # noqa: E402


def payee(name="Maple Lumber Supply LLC", receiver="ap@maple-lumber.example"):
    return {"beneficiary": {"entity_type": "COMPANY", "company_name": name, "type": "PAYPAL",
                            "paypal": {"recipient_type": "EMAIL", "receiver": receiver}}}


class PBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.t = 1_800_000_000.0
        self.api = demo_paypal()
        self.fake = self.api.fake
        self.svc = Service(Store(os.path.join(self.dir, "a.db")), self.api, clock=lambda: self.t)
        self.svc.s.init_policy("USD", per_tx_cap=100_000, daily_cap=200_000)
        self.ben = self.api.create_beneficiary(payee())["id"]
        self.svc.allow_beneficiary(self.ben, "Maple Lumber Supply LLC", "PayPal ap@m***", "zach")
        self.n = 0

    def incident(self, amount=15_000):
        self.n += 1
        return self.svc.open_incident("Maple Lumber", f"PP-{self.n}", self.ben, amount, "USD", allow_similar=True)

    def pay(self, iid, amount=15_000, role="original"):
        ap = self.svc.approve(iid, role, "zach")
        return self.svc.pay(ap, role, self.ben, amount, "USD")

    def move(self, p, status, ftype=None):
        self.fake.simulate(p["provider_id"], status, ftype)
        return self.svc.reconcile(p["request_id"])


class Idempotency(PBase):
    def test_payout_body_uses_request_id_for_batch_and_item(self):
        p = self.pay(self.incident())
        b = self.fake.batches[p["provider_id"]]
        self.assertEqual(b["batch_header"]["sender_batch_header"]["sender_batch_id"], p["request_id"])
        self.assertEqual(len(b["items"]), 1)
        self.assertEqual(b["items"][0]["payout_item"]["sender_item_id"], p["request_id"])
        self.assertEqual(b["items"][0]["payout_item"]["receiver"], "ap@maple-lumber.example")
        self.assertEqual(b["items"][0]["payout_item"]["amount"], {"value": "150.00", "currency": "USD"})
        self.assertEqual(p["status"], "PROCESSING")

    def _lost_then_resend(self):
        iid = self.incident()
        self.fake.lose_response_next = True
        p = self.pay(iid)
        self.assertEqual(p["status"], "UNKNOWN")
        self.assertEqual(len(self.fake.batches), 1)          # PayPal DID create it
        with self.assertRaisesRegex(Refused, "confirms the previous payment failed"):
            self.svc.approve(iid, "replacement", "zach")       # duplicate lock while unknown
        self.t += 120
        r = self.svc.resend(p["request_id"], "zach")
        self.assertEqual(len(self.fake.batches), 1)          # same sender_batch_id -> still ONE payout
        self.assertEqual(r["status"], "PROCESSING")
        self.assertEqual(r["provider_id"], next(iter(self.fake.batches)))
        return iid, r

    def test_lost_reply_resend_replays_paypal_request_id(self):
        self._lost_then_resend()

    def test_lost_reply_resend_adopts_original_via_duplicate_sender_batch_id(self):
        self.fake.honor_request_id = False                   # PayPal-Request-Id replay off: only sender_batch_id dedupes
        iid, r = self._lost_then_resend()
        self.assertEqual(self.fake.posts, 2)                 # two POSTs, one payout
        self.move(r, "SUCCESS")
        self.svc.close(iid, "zach")

    def test_lost_reply_cannot_be_released_because_paypal_cannot_be_searched(self):
        self.fake.lose_response_next = True
        p = self.pay(self.incident())
        self.t += 16 * 60
        with self.assertRaisesRegex(Refused, "could not confirm"):
            self.svc.release(p["request_id"], "zach", "PayPal shows nothing in the dashboard for this one")
        self.assertEqual(self.svc.payment(p["request_id"])["status"], "UNKNOWN")

    def test_5xx_before_create_then_resend_pays_once(self):
        iid = self.incident()
        self.fake.fail_5xx_next = True
        p = self.pay(iid)
        self.assertEqual(p["status"], "UNKNOWN")
        self.assertEqual(len(self.fake.batches), 0)
        self.t += 120
        self.svc.resend(p["request_id"], "zach")
        self.assertEqual(len(self.fake.batches), 1)

    def test_approval_single_use(self):
        iid = self.incident()
        ap = self.svc.approve(iid, "original", "zach")
        self.svc.pay(ap, "original", self.ben, 15_000, "USD")
        with self.assertRaises(Refused):
            self.svc.pay(ap, "original", self.ben, 15_000, "USD")
        self.assertEqual(len(self.fake.batches), 1)


class StatusMapping(PBase):
    CASES = [  # PayPal item status, errors.name (fake), desk status, failure type, class
        ("SUCCESS", None, "PAID", None, None),
        ("PENDING", None, "PROCESSING", None, None),
        ("ONHOLD", None, "PROCESSING", None, None),
        ("UNCLAIMED", None, "SENT", None, None),
        ("FAILED", "INTERNAL_ERROR", "CANCELLED", "SYSTEM_ERROR", RETRYABLE),
        ("FAILED", "INSUFFICIENT_FUNDS", "CANCELLED", "INSUFFICIENT_FUNDS", RETRYABLE),
        ("FAILED", "RECEIVER_UNREGISTERED", "CANCELLED", "INVALID_BENEFICIARY_DETAILS", DETAILS_PROBLEM),
        ("FAILED", "INVALID_EMAIL", "CANCELLED", "INVALID_BENEFICIARY_DETAILS", DETAILS_PROBLEM),
        ("FAILED", "RECEIVER_ACCOUNT_LOCKED", "CANCELLED", "ACCOUNT_UNDER_RESTRICTION", DETAILS_PROBLEM),
        ("FAILED", "RISK_DECLINE", "CANCELLED", "TM_SUSPENDED", DELIBERATE),
        ("FAILED", "SOMETHING_NEW", "CANCELLED", "UNCLASSIFIED", None),
        ("RETURNED", None, "CANCELLED", "BENEFICIARY_BANK_RETURNED", DETAILS_PROBLEM),
        ("BLOCKED", None, "CANCELLED", "TM_SUSPENDED", DELIBERATE),
        ("REFUNDED", None, "CANCELLED", "BENEFICIARY_REQUESTED", DELIBERATE),
        ("REVERSED", None, "CANCELLED", "RECALL_REQUESTED", DELIBERATE),
    ]

    def test_each_paypal_status(self):
        for pp, err, desk, ftype, cls in self.CASES:
            with self.subTest(pp=pp, err=err):
                self.t += 2 * 86400                            # fresh day: caps and co-sign totals don't interfere
                p = self.pay(self.incident())
                it = self.fake.item_of(p["provider_id"])
                it["transaction_status"] = pp
                if err:
                    it["errors"] = {"name": err, "message": "x"}
                r = self.svc.reconcile(p["request_id"])
                self.assertEqual(r["status"], desk)
                self.assertEqual(r["status_source"], "provider_get")
                self.assertEqual(r["failure_type"], ftype)
                if cls is not None:
                    self.assertIn(ftype, cls)
                self.assertIsNone(r["anomaly"])

    def test_retryable_failure_allows_one_replacement(self):
        iid = self.incident()
        p = self.pay(iid)
        self.move(p, "FAILED", "CHANNEL_TIMEOUT")             # fake -> PayPal FAILED / INTERNAL_ERROR
        self.assertIn("REPLACE", self.svc.allowed_actions(iid))
        r = self.pay(iid, role="replacement")
        self.assertEqual(len(self.fake.batches), 2)
        self.assertNotEqual(r["request_id"], p["request_id"])
        with self.assertRaisesRegex(Refused, "confirms the previous payment failed"):
            self.svc.approve(iid, "replacement", "zach")       # replacement in flight: no third payout

    def test_details_problem_escalates_no_replace(self):
        iid = self.incident()
        p = self.pay(iid)
        it = self.fake.item_of(p["provider_id"])
        it["transaction_status"], it["errors"] = "FAILED", {"name": "RECEIVER_UNREGISTERED", "message": "no account"}
        self.svc.reconcile(p["request_id"])
        self.assertNotIn("REPLACE", self.svc.allowed_actions(iid))
        self.assertEqual(run(self.svc, iid, RulesAgent())["decision"], "ESCALATE")

    def test_unknown_item_status_is_not_an_observation(self):
        p = self.pay(self.incident())
        self.fake.item_of(p["provider_id"])["transaction_status"] = "WEIRD"
        r = self.svc.reconcile(p["request_id"])
        self.assertEqual(r["status"], "PROCESSING")            # unchanged
        with self.assertRaises(Ambiguous):
            self.api.get_transfer(p["provider_id"])


class Unclaimed(PBase):
    def test_unclaimed_stays_in_flight_and_blocks_replacement(self):
        iid = self.incident()
        p = self.pay(iid)
        r = self.move(p, "UNCLAIMED")
        self.assertEqual(r["status"], "SENT")
        acts = self.svc.allowed_actions(iid)
        self.assertIn("WAIT", acts)
        self.assertNotIn("REPLACE", acts)
        self.assertNotIn("CLOSE", acts)
        with self.assertRaisesRegex(Refused, "confirms the previous payment failed"):
            self.svc.approve(iid, "replacement", "zach")
        self.assertEqual(run(self.svc, iid, RulesAgent())["decision"], "WAIT")
        # 30 days later PayPal returns it: a details problem, so a person must check the receiver
        r = self.move(p, "RETURNED")
        self.assertEqual((r["status"], r["failure_type"]), ("CANCELLED", "BENEFICIARY_BANK_RETURNED"))
        self.assertNotIn("REPLACE", self.svc.allowed_actions(iid))

    def test_unclaimed_counts_against_daily_cap(self):
        self.svc.s.db.execute("UPDATE policy SET daily_cap=30000, dual_approval_over=10000000")
        p = self.pay(self.incident())
        self.move(p, "UNCLAIMED")
        with self.assertRaisesRegex(Refused, "today's cap"):
            self.pay(self.incident())


class ReturnAfterSuccess(PBase):
    def test_returned_after_success_alarms_like_a_bank_return(self):
        iid = self.incident()
        p = self.pay(iid)
        self.move(p, "SUCCESS")
        self.svc.close(iid, "zach")
        r = self.move(p, "RETURNED")
        self.assertEqual(r["status"], "CANCELLED")
        self.assertEqual(r["anomaly"], "PAID_THEN_CANCELLED")  # same code as an Airwallex bank return after PAID
        inc = self.svc._incident(iid)
        self.assertEqual((inc["alert"], inc["state"]), ("PAID_THEN_CANCELLED", "ESCALATED"))
        self.assertNotIn("REPLACE", self.svc.allowed_actions(iid))

    def test_reversed_after_success_also_alarms(self):
        iid = self.incident()
        p = self.pay(iid)
        self.move(p, "SUCCESS")
        self.assertEqual(self.move(p, "REVERSED")["anomaly"], "PAID_THEN_CANCELLED")


class ReceiverChange(PBase):
    def test_changed_receiver_email_trips_two_person_protection(self):
        iid = self.incident()
        ap = self.svc.approve(iid, "original", "zach")
        before = details_hash(self.api.get_beneficiary(self.ben))
        cur = self.api.get_beneficiary(self.ben)
        cur["beneficiary"]["paypal"]["receiver"] = "maple-payments@attacker.example"   # insider / BEC edit
        self.api.update_beneficiary(self.ben, cur)
        self.assertNotEqual(details_hash(self.api.get_beneficiary(self.ben)), before)
        with self.assertRaisesRegex(Refused, "changed after they were approved"):
            self.svc.pay(ap, "original", self.ben, 15_000, "USD")
        self.assertEqual(self.fake.posts, 0)                   # nothing reached PayPal
        pol = self.svc.s.policy()
        self.assertTrue(pol["killed"])
        self.assertEqual(pol["killed_by"], "system")
        self.assertEqual(self.svc._incident(iid)["alert"], "BENEFICIARY_CHANGED")
        with self.assertRaisesRegex(Refused, "second person"):
            self.svc.unkill("zach")                            # system STOP: one person cannot resume
        self.svc.unkill("zach", confirmed_by="jordan")

    def test_same_receiver_under_another_name_is_refused(self):
        other = self.api.create_beneficiary(payee("Totally Different Corp", "AP@Maple-Lumber.example"))["id"]
        with self.assertRaisesRegex(Refused, "money-mule"):
            self.svc.allow_beneficiary(other, "Totally Different Corp", "PayPal", "zach")

    def test_payout_to_other_receiver_than_recorded_is_mismatch(self):
        iid = self.incident()
        p = self.pay(iid)
        self.fake.item_of(p["provider_id"])["payout_item"]["receiver"] = "someone@else.example"
        self.svc.reconcile(p["request_id"])
        self.assertEqual(self.svc._incident(iid)["alert"], "PROVIDER_MISMATCH")

    def test_extra_item_in_batch_is_mismatch(self):
        iid = self.incident()
        p = self.pay(iid)
        b = self.fake.batches[p["provider_id"]]
        extra = dict(b["items"][0], payout_item=dict(b["items"][0]["payout_item"], receiver="x@y.example", sender_item_id="other"))
        b["items"].append(extra)
        self.svc.reconcile(p["request_id"])
        self.assertEqual(self.svc._incident(iid)["alert"], "PROVIDER_MISMATCH")


class Guards(PBase):
    def test_base_url_refused(self):
        for bad in ("https://api-m.paypal.com", "https://api.paypal.com", "http://api-m.sandbox.paypal.com",
                    "https://api-m.sandbox.paypal.com.evil.example", "https://evil.example/api-m.sandbox.paypal.com"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    PayPalPayouts("id", "secret", base=bad)
                with self.assertRaises(ValueError):
                    PayPalPayouts("id", "secret", base=bad, transport=FakePayPalAPI())
        PayPalPayouts("id", "secret", base="https://api-m.sandbox.paypal.com/", book=PayeeBook())

    def test_stop_respected(self):
        iid = self.incident()
        ap = self.svc.approve(iid, "original", "zach")
        self.svc.kill("zach")
        with self.assertRaisesRegex(Refused, "STOP"):
            self.svc.pay(ap, "original", self.ben, 15_000, "USD")
        self.assertEqual(self.fake.posts, 0)

    def test_per_payment_cap(self):
        iid = self.svc.open_incident("Maple Lumber", "BIG-1", self.ben, 150_000, "USD")
        with self.assertRaisesRegex(Refused, "cap"):
            self.svc.approve(iid, "original", "zach")

    def test_foreign_currency_refused(self):
        self.svc.s.db.execute("UPDATE policy SET fx_currencies='EUR'")
        iid = self.svc.open_incident("Maple Lumber", "EU-1", self.ben, 10_000, "EUR")
        ap = self.svc.approve(iid, "original", "zach")
        with self.assertRaisesRegex(Refused, "exchange rate"):
            self.svc.pay(ap, "original", self.ben, 10_000, "EUR")
        self.assertEqual(self.fake.posts, 0)

    def test_no_credentials_is_a_clean_error(self):
        a = PayPalPayouts(client_id="", client_secret="", book=PayeeBook())
        a.client_id = a.client_secret = None
        with self.assertRaises(ProviderError):
            a._auth()

    def test_token_request_shape(self):
        seen = []

        class A(PayPalPayouts):
            def _http(self, method, path, data, headers):
                seen.append((method, path, data, headers))
                return {"access_token": "tok", "expires_in": 32400}
        a = A("cid", "csecret", book=PayeeBook())
        self.assertEqual(a._auth(), "tok")
        m, path, data, h = seen[0]
        self.assertEqual((m, path, data), ("POST", "/v1/oauth2/token", b"grant_type=client_credentials"))
        self.assertTrue(h["Authorization"].startswith("Basic "))
        self.assertEqual(a._auth(), "tok")
        self.assertEqual(len(seen), 1)                          # cached

    def test_payee_book_persists(self):
        path = os.path.join(self.dir, "pp.json")
        a = demo_paypal(book=PayeeBook(path))
        pid = a.create_beneficiary(payee())["id"]
        a.book.record("rid-1", batch_id="B1")
        b = PayeeBook(path)
        self.assertEqual(b.payee(pid)["beneficiary"]["paypal"]["receiver"], "ap@maple-lumber.example")
        self.assertEqual(b.sent("rid-1")["batch_id"], "B1")


if __name__ == "__main__":
    unittest.main()
