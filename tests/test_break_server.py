import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ALLOWANCE_DB"] = os.path.join(tempfile.mkdtemp(), "s.db")
os.environ["OPERATOR_TOKEN"] = "shared"
os.environ["OPERATOR_TOKENS"] = "zach:z-secret,jordan:j-secret"
import server  # noqa
from test_allowance import FakeAirwallex, bank
from allowance.service import Service, Refused
from allowance.store import Store

class ServerBreak(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAirwallex()
        server.API = self.fake; server.PROV = server.DropOnce(self.fake)
        server.SVC = Service(Store(os.path.join(tempfile.mkdtemp(), "x.db")), server.PROV)
        server.SVC.s.init_policy("USD", per_tx_cap=100_000, daily_cap=300_000)
        self.fake.bens["ben-1"] = bank()
        server.SVC.allow_beneficiary("ben-1", "Maple Lumber Supply LLC", "acct ****6789", "operator")

    def test_one_click_reverify_then_old_approval_pays_new_account(self):
        h = server.handle
        iid = h("POST", "/api/incidents", {"amount": "250"}, "zach")["id"]
        h("POST", f"/api/incidents/{iid}/approve", {"role": "original"}, "zach")
        self.fake.bens["ben-1"] = bank(acct="000111222333")            # attacker edits payee
        pid = h("POST", "/api/payee/reverify", {"note": "Operator re-verified by phone on file"}, "zach")["pending"]
        with self.assertRaises(Refused):
            h("POST", f"/api/pending/{pid}/confirm", {}, "zach")          # can't confirm your own request
        h("POST", f"/api/pending/{pid}/confirm", {}, "jordan")
        with self.assertRaises(Refused):
            h("POST", f"/api/incidents/{iid}/pay", {"role": "original"}, "zach")  # old approval saw the old account

    def test_shared_token_is_a_second_identity_for_cosign(self):
        # one human holding OPERATOR_TOKEN + their named token can satisfy four-eyes
        self.assertNotEqual(server.who("shared"), server.who("z-secret"))
        server.SVC.s.db.execute("UPDATE policy SET dual_approval_over=10000")
        h = server.handle
        iid = h("POST", "/api/incidents", {"amount": "600"}, "zach")["id"]
        h("POST", f"/api/incidents/{iid}/approve", {"role": "original"}, "zach")
        with self.assertRaises(Refused):
            h("POST", f"/api/incidents/{iid}/cosign", {"role": "original"}, "zach")   # can't co-sign yourself
        with self.assertRaises(Refused):
            h("POST", f"/api/incidents/{iid}/pay", {"role": "original"}, "zach")

    def test_reverify_and_setup_log_hardcoded_operator(self):
        with self.assertRaises(Refused):
            server.handle("POST", "/api/payee/reverify", {}, "jordan")   # a note is required
        pid = server.handle("POST", "/api/payee/reverify", {"note": "Called Maple on the number in our 2024 contract"}, "jordan")["pending"]
        server.handle("POST", f"/api/pending/{pid}/confirm", {}, "zach")
        b = server.SVC.s.one("SELECT approved_by FROM beneficiaries")
        self.assertEqual(b["approved_by"], "jordan")

if __name__ == "__main__":
    unittest.main()
