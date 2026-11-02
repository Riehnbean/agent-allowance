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


if __name__ == "__main__":
    unittest.main()
