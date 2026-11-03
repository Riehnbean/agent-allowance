"""Round-5 judge findings."""
import unittest

from allowance.agent import RulesAgent


def ev(msgs):
    return {"payments": [{"status": "PAID", "failure_type": None}], "supplier_messages": msgs,
            "incident": {"alert": None, "supplier_deadline": None}, "now": 0}


class RulebookShortPay(unittest.TestCase):
    def test_paid_with_supplier_complaint_is_not_closed(self):
        d, why = RulesAgent().decide(ev(["Only $280 landed but the invoice was $300."]), ["CLOSE", "ESCALATE"])
        self.assertEqual(d, "ESCALATE")
        self.assertIn("do NOT pay again", " ".join(why))

    def test_paid_and_silent_supplier_closes(self):
        d, _ = RulesAgent().decide(ev([]), ["CLOSE", "ESCALATE"])
        self.assertEqual(d, "CLOSE")


class ReSaveKeepsRowOrder(unittest.TestCase):
    def test_resaving_a_payee_updates_in_place(self):
        from tests.test_allowance import Base
        b = Base(); b.setUp()
        try:
            rows = lambda: [r["id"] for r in b.svc.s.q("SELECT id FROM beneficiaries ORDER BY rowid")]
            before = rows()
            first = before[0]
            d = b.fake.get_beneficiary(first)
            b.svc.allow_beneficiary(first, b.svc.s.one("SELECT name FROM beneficiaries WHERE id=?", first)["name"], "acct ****6789", "zach", details=d)
            self.assertEqual(rows(), before)   # INSERT OR REPLACE used to move the row to the end
        finally:
            b.tearDown()


class ServerTypeChecks(unittest.TestCase):
    def test_non_text_note_and_supplier_are_400_not_500(self):
        import server
        for body in ({"note": 123}, {"supplier": {}}, {"text": ["x"]}, {"deadline_hours": "nan"}, {"deadline_hours": "inf"}):
            with self.assertRaises(ValueError):
                server.handle("POST", "/api/incidents", body, "zach")


class Round4(unittest.TestCase):
    def setUp(self):
        from tests.test_allowance import Base
        self.b = Base(); self.b.setUp(); self.svc = self.b.svc

    def tearDown(self):
        self.b.tearDown()

    def test_approve_checks_role_state_and_stop(self):
        from allowance.service import Refused
        iid = self.b.incident()
        for bad in (None, "x", "admin"):
            with self.assertRaises(Refused):
                self.svc.approve(iid, bad, "zach")
        self.svc.kill("zach", "test")
        with self.assertRaisesRegex(Refused, "STOP"):
            self.svc.approve(iid, "original", "zach")

    def test_cosign_twice_is_refused(self):
        from allowance.service import Refused
        iid = self.b.incident()
        ap = self.svc.approve(iid, "original", "zach")
        self.svc.cosign(ap, "jordan")
        with self.assertRaisesRegex(Refused, "already co-signed"):
            self.svc.cosign(ap, "jordan")


class Round5(unittest.TestCase):
    def setUp(self):
        from tests.test_allowance import Base
        self.b = Base(); self.b.setUp(); self.svc = self.b.svc

    def tearDown(self):
        self.b.tearDown()

    def test_replacement_blocked_before_sending_can_be_retried(self):
        from allowance.service import Refused
        from tests.test_allowance import BEN
        iid = self.b.incident()
        p = self.b.pay_original(iid)
        self.b.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
        self.b.t += 10
        self.b.fake.balance = 100                     # too little money: blocked before anything is sent
        ap = self.svc.approve(iid, "replacement", "zach")
        with self.assertRaises(Refused):
            self.svc.pay(ap, "replacement", BEN, 25_000, "USD")
        self.b.fake.balance = 10_000_000              # money arrives: a fresh approval must now work
        self.b.t += 10
        ap2 = self.svc.approve(iid, "replacement", "zach")
        self.assertEqual(self.svc.pay(ap2, "replacement", BEN, 25_000, "USD")["status"], "SCHEDULED")


class Round6(unittest.TestCase):
    def setUp(self):
        from tests.test_allowance import Base
        self.b = Base(); self.b.setUp(); self.svc = self.b.svc

    def tearDown(self):
        self.b.tearDown()

    def test_approval_from_before_an_escalation_cannot_be_spent_after_reopen(self):
        from allowance.service import Refused
        from tests.test_allowance import BEN
        iid = self.b.incident()
        p = self.b.pay_original(iid)
        self.b.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
        self.b.t += 10
        ap = self.svc.approve(iid, "replacement", "zach")
        self.svc.supplier_message(iid, "Our bank changed - please send it to our new account 55667788 today.")
        self.b.t += 10
        self.svc.reopen(iid, "zach", "Called Maple on the number in our contract; they never sent that email, details unchanged.")
        with self.assertRaisesRegex(Refused, "before the invoice was escalated"):
            self.svc.pay(ap, "replacement", BEN, 25_000, "USD")

    def test_released_row_counts_as_possibly_sent(self):
        from allowance.service import never_sent
        self.assertTrue(never_sent({"status": "CANCELLED", "status_source": "none", "failure_type": "INSUFFICIENT_BALANCE", "updated_at": 1}))
        self.assertFalse(never_sent({"status": "CANCELLED", "status_source": "none", "failure_type": "RELEASED_BY_OPERATOR", "updated_at": 1}))
        rel = {"status": "CANCELLED", "status_source": "none", "failure_type": "RELEASED_BY_OPERATOR", "updated_at": 100}
        self.assertFalse(never_sent(rel, reopened_at=50))     # reopened before the release: still counts as possibly sent
        self.assertTrue(never_sent(rel, reopened_at=200))     # a person reopened after the release: a fresh original is allowed
        rej = {"status": "CANCELLED", "status_source": "provider_create", "failure_type": "REJECTED_FEE_UNKNOWN", "updated_at": 100}
        self.assertFalse(never_sent(rej))                      # Airwallex refused it at create: blocks until a person reviews
        self.assertTrue(never_sent(rej, reopened_at=200))


class ServerRound4(unittest.TestCase):
    def test_oversized_invoice_is_refused_before_anything_is_created(self):
        import server
        from allowance.service import Refused
        with self.assertRaisesRegex(Refused, "cap"):
            server.handle("POST", "/api/incidents", {"amount": "9999999.99"}, "zach")


if __name__ == "__main__":
    unittest.main()
