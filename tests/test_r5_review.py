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


if __name__ == "__main__":
    unittest.main()
