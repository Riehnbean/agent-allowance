"""Independent adversarial review tests (Nov 1 2026).

Each test asserts the SAFE / CLAIMED behaviour. A failing test = a finding (see REPORT.md).
Run from the repo root:
    python3 -m unittest tests.test_indep -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ALLOWANCE_DB", os.path.join(tempfile.mkdtemp(), "s.db"))
os.environ.setdefault("OPERATOR_TOKENS", "zach:z-secret,jordan:j-secret")

from test_allowance import BEN, Base, FakeAirwallex, bank  # noqa: E402
from allowance.adapter import Ambiguous, details_hash  # noqa: E402
from allowance.agent import LLMAgent, RulesAgent, run  # noqa: E402
from allowance.service import Refused, Service  # noqa: E402
from allowance.store import Store  # noqa: E402


class F1_ReleaseResendRace(Base):
    """P1: release() and resend() on the same held payment can interleave. resend's submit(first=False)
    never re-checks that the payment is still UNKNOWN/RESERVED before calling create_transfer, so the
    transfer is created at Airwallex while the desk has already marked it CANCELLED (budget freed),
    and _set() then drops the create reply as a 'late update'. No alarm; the sweeper never looks at it."""

    def test_release_during_resend_leaves_live_transfer_marked_cancelled(self):
        fake = self.fake
        real_create = fake.create_transfer
        state = {"drop": True, "release_hook": False}

        def create(request_id, *a, **k):
            if state["drop"]:
                state["drop"] = False
                raise Ambiguous("connect timeout - request never reached Airwallex")
            return real_create(request_id, *a, **k)
        fake.create_transfer = create

        iid = self.incident()
        p = self.pay_original(iid)
        self.assertEqual(p["status"], "UNKNOWN")
        self.assertEqual(len(fake.by_req), 0)
        held_budget = self.svc.s.committed_today(self.t)
        self.t += 16 * 60  # past release()'s 15-minute wait; approval (1h) still valid for resend

        real_ben = fake.get_beneficiary

        def ben(bid):
            # Operator B clicks "Release hold" while operator A's "Re-send" is between its checks and the send.
            if state["release_hook"]:
                state["release_hook"] = False
                self.svc.release(p["request_id"], "jordan", "Airwallex has no record of it, releasing the hold")
            return real_ben(bid)
        fake.get_beneficiary = ben
        state["release_hook"] = True

        try:
            self.svc.resend(p["request_id"], "zach")
        except Refused:
            pass  # refusing the re-send (nothing sent) is a safe outcome
        row = self.svc.payment(p["request_id"])
        sent = p["request_id"] in fake.by_req
        # Safe outcome: either nothing was sent, or the desk knows it was sent (not CANCELLED, budget held).
        self.assertFalse(sent and row["status"] == "CANCELLED",
                         f"Airwallex has a live transfer but desk says {row['status']}/{row['failure_type']}; "
                         f"budget held before={held_budget}, now={self.svc.s.committed_today(self.t)}")


class F2_TwinPayeeSplitsUnderCosign(Base):
    """P2: co-sign aggregation (_check day_to_payee, service.py:593) sums by beneficiary_id, but the same
    bank account may be allow-listed twice under the same name (allow_beneficiary only refuses a twin
    with a DIFFERENT name). Two invoices split across the two records reach one account with no co-signer."""

    def test_split_across_duplicate_payee_records_needs_cosign(self):
        self.fake.bens["ben-twin"] = bank()  # identical account, identical name
        self.svc.allow_beneficiary("ben-twin", "Maple Lumber Supply LLC", "****6789 / ABA 026009593", "zach")
        a = self.svc.open_incident("Maple", "INV-A1", BEN, 40_000, "USD", allow_similar=True)
        b = self.svc.open_incident("Maple", "INV-B2", "ben-twin", 40_000, "USD", allow_similar=True)
        self.svc.pay(self.svc.approve(a, "original", "zach"), "original", BEN, 40_000, "USD")
        ap = self.svc.approve(b, "original", "zach")
        with self.assertRaisesRegex(Refused, "co-sign"):
            self.svc.pay(ap, "original", "ben-twin", 40_000, "USD")  # $800 to one account in 24h, limit $500


class F3_ModelCloseOnAlarmNotClamped(Base):
    """P2: LLMAgent.decide() clamps REPLACE (alarm/fraud) and CLOSE (not-received) but not CLOSE/WAIT under
    a safety alarm. allowed_actions() still offers CLOSE when DOUBLE_PAID is up, so a weak or steered model's
    CLOSE is recorded as the agent's call although the prompt's rule 1 and the rulebook say ESCALATE."""

    def test_double_paid_model_close_is_downgraded(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
        self.t += 1
        ap = self.svc.approve(iid, "replacement", "zach")
        r = self.svc.pay(ap, "replacement", BEN, 25_000, "USD")
        self.fake.simulate(r["provider_id"], "PAID")
        self.svc.reconcile(r["request_id"])
        self.fake.by_id[p["provider_id"]]["status"] = "PAID"   # original paid out after all
        self.svc.reconcile(p["request_id"])
        self.assertEqual(self.svc._incident(iid)["alert"], "DOUBLE_PAID")
        self.assertIn("CLOSE", self.svc.allowed_actions(iid))

        agent = LLMAgent(provider="local")
        agent.brains[0].ask = lambda user: '{"decision":"CLOSE","reason":"Supplier confirmed both arrived; all settled."}'
        self.t += 10
        out = run(self.svc, iid, agent)
        self.assertEqual(out["decision"], "ESCALATE", f"model CLOSE accepted with DOUBLE_PAID alarm: {out}")


class F4_SupplierTextSteering(Base):
    """P2: 'bank-change wording forces escalation' is a regex. Ordinary redirection phrasing is missed, so a
    retryable failure plus a bank-change email gets REPLACE from the rulebook (money still goes to the old
    verified account, but the human-must-look signal is lost)."""

    PHRASES = [
        "We switched banks last month, please use the details on our updated invoice.",
        "Our bank account has changed. Call us and we will give you the new info.",
        "Our banking information has been updated - see attached letter.",
        "Please route the replacement to our Chase account ending 4421 instead.",
        "Funds should go to our other account from now on.",
    ]

    def test_bank_change_phrasings_escalate(self):
        missed = []
        for text in self.PHRASES:
            iid = self.incident()
            p = self.pay_original(iid)
            self.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
            self.svc.supplier_message(iid, text)
            self.t += 5
            d = run(self.svc, iid, RulesAgent())["decision"]
            if d != "ESCALATE":
                missed.append((d, text))
        self.assertEqual(missed, [], "bank-change requests not escalated")

    def test_supplier_says_unpaid_blocks_close(self):
        # room for four $250 payments to one payee without tripping the (correct) co-sign / payee caps
        self.svc.s.db.execute("UPDATE policy SET dual_approval_over=100000, per_supplier_daily_cap=200000")
        missed = []
        for text in ["We are still waiting on the money.", "Payment is still missing on our end.",
                     "Nothing has shown up in our account yet.", "No funds arrived."]:
            iid = self.incident()
            p = self.pay_original(iid)
            self.fake.simulate(p["provider_id"], "PAID")
            self.svc.reconcile(p["request_id"])
            self.svc.supplier_message(iid, text)
            self.t += 5
            d = run(self.svc, iid, RulesAgent())["decision"]
            if d != "ESCALATE":
                missed.append((d, text))
        self.assertEqual(missed, [], "supplier says unpaid but agent recommends CLOSE")


class F5_MismatchAfterReopenCountsAsFreshRead(Base):
    """P2: _anomaly() de-duplicates by code, and a PROVIDER_MISMATCH read never updates checked_at but also
    never raises in strict mode when the bogus record's status happens to equal ours. After one reopen, the
    next mismatching read is silent and the replacement goes out on a read that did not describe our transfer."""

    def test_replacement_refused_when_fresh_read_is_someone_elses_transfer(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
        tid = p["provider_id"]
        bogus = dict(self.fake.by_id[tid], beneficiary_id="ben-someone-else")
        self.fake.get_transfer = lambda t: dict(bogus)          # provider now returns a record that is not ours
        self.t += 2
        ap = self.svc.approve(iid, "replacement", "zach")
        with self.assertRaises(Refused):
            self.svc.pay(ap, "replacement", BEN, 25_000, "USD")  # first time: alarm, refused (good)
        self.t += 2
        self.svc.reopen(iid, "zach", "Looked at the dashboard, seems fine to me")
        ap2 = self.svc.approve(iid, "replacement", "zach")
        self.t += 2
        with self.assertRaises(Refused):
            self.svc.pay(ap2, "replacement", BEN, 25_000, "USD")  # second mismatch must still block


class F6_TransientBlockBricksInvoice(Base):
    """P3: a pre-submit block on a first send (low balance, payee re-read failure, STOP race) marks the original
    CANCELLED with status_source='none'. No original can be made again ('an original payment already exists'),
    no replacement can be approved (needs provider_get), and the invoice key blocks a new incident."""

    def test_low_balance_once_does_not_make_invoice_unpayable(self):
        iid = self.incident(invoice="INV-7777")
        self.fake.balance = 0
        with self.assertRaises(Refused):
            self.pay_original(iid)
        self.fake.balance = 10_000_000
        self.t += 60
        paths = []
        for fn in (lambda: self.pay_original(iid),
                   lambda: self.svc.approve(iid, "replacement", "zach"),
                   lambda: self.svc.open_incident("Maple", "INV-7777", BEN, 25_000, "USD", allow_similar=True)):
            try:
                fn()
                paths.append("ok")
            except Refused as e:
                paths.append(str(e)[:60])
        self.assertIn("ok", paths, f"invoice can no longer be paid by any path: {paths}")


class F7_StaleOnOtherIncidentsEscalates(Base):
    """P3: evidence() includes budget_left_today, so activity on ANY other incident makes this incident's advice
    'stale'. run() re-asks once, then records ESCALATE, which flips the incident to ESCALATED (payments frozen,
    reopen needed) although nothing about this incident changed."""

    def test_unrelated_payment_does_not_escalate_this_incident(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fake.simulate(p["provider_id"], "SENT")
        test = self

        class Noisy(RulesAgent):
            def decide(self, ev, allowed):
                other = test.incident(amount=1_000 + Base.n)
                test.pay_original(other, amount=test.svc._incident(other)["amount"])
                return super().decide(ev, allowed)
        out = run(self.svc, iid, Noisy())
        self.assertEqual(out["decision"], "WAIT", out)
        self.assertEqual(self.svc._incident(iid)["state"], "OPEN")


class F8_TwoPersonOnlyInServer(Base):
    """P3 (claim): 'a system STOP needs two people to clear' is enforced only in server.py and only when
    OPERATOR_TOKENS has 2+ names. The README's run recipe uses a single OPERATOR_TOKEN, and Service.unkill
    has no check at all."""

    def test_system_stop_needs_two_people(self):
        self.svc.kill("system", "payee bank details changed after approval")
        with self.assertRaises(Refused):
            self.svc.unkill("zach")


class F9_ReverifyTOCTOU(unittest.TestCase):
    """P2: /api/pending/<id>/confirm checks the payee hash with one GET (server.py:169), then reverify() does a
    SECOND GET (server.py:124) and allow-lists whatever that returns. An edit between the two reads is accepted
    under the two-person approval for different details."""

    def test_confirm_accepts_only_the_details_both_people_saw(self):
        import server
        fake = FakeAirwallex()
        server.API = fake
        server.PROV = server.DropOnce(fake)
        server.SVC = Service(Store(os.path.join(tempfile.mkdtemp(), "x.db")), server.PROV)
        server.SVC.s.init_policy("USD", per_tx_cap=100_000, daily_cap=300_000)
        server.OPERATORS.clear()
        server.OPERATORS.update({"z": "zach", "j": "jordan"})
        fake.bens["ben-1"] = bank()
        server.SVC.allow_beneficiary("ben-1", "Maple Lumber Supply LLC", "acct ****6789", "zach")
        seen = bank(acct="555500001111")       # details the two people checked by phone
        fake.bens["ben-1"] = seen
        pid = server.handle("POST", "/api/payee/reverify", {"note": "Called Maple on the number in our contract"}, "zach")["pending"]
        calls = {"n": 0}
        attacker = bank(acct="999900002222")

        def get_ben(bid):
            calls["n"] += 1
            return seen if calls["n"] == 1 else attacker   # edit lands right after the confirm-time check
        fake.get_beneficiary = get_ben
        server.handle("POST", f"/api/pending/{pid}/confirm", {}, "jordan")
        stored = server.SVC.s.one("SELECT details_hash FROM beneficiaries WHERE id='ben-1'")["details_hash"]
        self.assertEqual(stored, details_hash(seen), "allow-listed details differ from what was confirmed")


if __name__ == "__main__":
    unittest.main()
