"""Red-team tests. Each test asserts the SAFE behaviour, so a FAILURE/ERROR == bug confirmed."""
import os
import sqlite3
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_allowance import BEN, Base, FakeAirwallex, bank  # noqa: E402
from allowance.adapter import Airwallex, Ambiguous, ProviderError  # noqa: E402
from allowance.agent import RulesAgent, run  # noqa: E402
from allowance.service import Refused  # noqa: E402


class Fake2(FakeAirwallex):
    """Adds: fail BEFORE reaching Airwallex, lagging list endpoint, dup error without id, hooks."""
    fail_before_next = False
    find_lag = 0
    dup_error_without_id = False
    on_get_beneficiary = None
    get_transfer_error = None

    def create_transfer(self, request_id, *a, **k):
        if self.fail_before_next:
            self.fail_before_next = False
            raise Ambiguous("connection reset before Airwallex saw it")
        if self.dup_error_without_id and request_id in self.by_req:
            raise ProviderError(400, "duplicate_request_id", "request_id already used", {})
        return super().create_transfer(request_id, *a, **k)

    def find_by_request_id(self, rid):
        if self.find_lag:
            self.find_lag -= 1
            return None
        return super().find_by_request_id(rid)

    def get_beneficiary(self, bid):
        if self.on_get_beneficiary:
            h, self.on_get_beneficiary = self.on_get_beneficiary, None
            h()
        return super().get_beneficiary(bid)

    def get_transfer(self, tid):
        if self.get_transfer_error and tid == self.get_transfer_error:
            raise ProviderError(404, "not_found", "transfer not found")
        return super().get_transfer(tid)


class B(Base):
    def setUp(self):
        super().setUp()
        self.fake.__class__ = Fake2
        self.svc.s.db.execute("UPDATE policy SET dual_approval_over=10000000, per_supplier_daily_cap=10000000")


class Break(B):
    # 1 ------------------------------------------------------------------
    def test_duplicate_invoice_bypass_by_supplier_spelling(self):
        a = self.svc.open_incident("Maple Lumber", "INV-7001", BEN, 25_000, "USD")
        p = self.pay_original(a)
        self.fake.simulate(p["provider_id"], "PAID"); self.svc.reconcile(p["request_id"]); self.svc.close(a, "zach")
        # same payee, same invoice, same amount - supplier typed slightly differently
        with self.assertRaisesRegex(Refused, "duplicate invoice"):
            self.svc.open_incident("Maple Lumber Supply", "INV 07001", BEN, 25_000, "USD", allow_similar=True)
        paid = [t for t in self.fake.by_id.values() if t["status"] == "PAID"]
        self.assertEqual(len(paid), 1, "INV-7001 paid twice to the same payee")

    # 2 ------------------------------------------------------------------
    def _unknown_never_sent(self):
        iid = self.incident()
        self.fake.fail_before_next = True
        p = self.svc.pay(self.svc.approve(iid, "original", "zach", ttl=60), "original", BEN, 25_000, "USD")
        self.assertEqual(p["status"], "UNKNOWN")
        self.assertEqual(len(self.fake.by_req), 0)
        return iid, p

    def test_sweeper_resubmits_unknown_after_payee_changed_and_incident_escalated(self):
        iid, p = self._unknown_never_sent()
        self.svc.supplier_message(iid, "Our bank changed - send to new account 55667788")
        run(self.svc, iid, RulesAgent())                       # -> ESCALATE
        self.fake.bens[BEN] = bank(acct="999988887777")        # payee edited at Airwallex
        self.t += 3 * 86400                                    # approval long expired
        self.svc.sweep(stale_after=300)                        # background thread, no human
        self.assertEqual(len(self.fake.by_req), 0, "sweeper sent money to edited payee on escalated incident with expired approval")

    def test_forged_webhook_triggers_resubmit(self):
        iid, p = self._unknown_never_sent()
        self.fake.bens[BEN] = bank(acct="999988887777")
        self.svc.on_webhook({"data": {"object": {"request_id": p["request_id"], "status": "PAID"}}})  # unauthenticated
        self.assertEqual(len(self.fake.by_req), 0, "anonymous webhook caused a transfer to an edited payee")

    # 3 ------------------------------------------------------------------
    def test_approval_not_bound_to_bank_details(self):
        iid = self.incident()
        ap = self.svc.approve(iid, "original", "zach")       # approved while account = ...6789
        self.fake.bens[BEN] = bank(acct="000111222333")      # attacker edits payee
        # what /api/payee/reverify does: one operator, one click, canned note, mule-check override
        self.svc.allow_beneficiary(BEN, "Maple Lumber Supply LLC", "****2333", "operator",
                                   details=self.fake.bens[BEN], override_reason="re-verified by phone")
        with self.assertRaises(Refused):
            self.svc.pay(ap, "original", BEN, 25_000, "USD")  # old approval pays the NEW account

    # 4 ------------------------------------------------------------------
    def test_unknown_provider_status_drops_out_of_budget_and_sweep(self):
        iid = self.incident(amount=90_000)
        p = self.pay_original(iid, amount=90_000)
        self.fake.by_id[p["provider_id"]]["status"] = "SUSPENDED"  # any status the code doesn't list
        self.svc.reconcile(p["request_id"])
        self.assertEqual(self.svc.s.committed_today(self.t), 90_000 + 2_500, "in-flight SUSPENDED payment not counted (amount + possible fee)")

    # 5 ------------------------------------------------------------------
    def test_unknown_becomes_cancelled_on_provider_error_while_money_moves(self):
        iid = self.incident()
        self.fake.lose_response_next = True
        p = self.pay_original(iid)                        # transfer IS created, we see UNKNOWN
        self.fake.find_lag = 1                            # list endpoint lags once
        self.fake.dup_error_without_id = True             # adapter re-raises dup error w/o details.id
        r = self.svc.reconcile(p["request_id"])           # resubmit -> ProviderError
        self.assertNotEqual(r["status"], "CANCELLED", "UNKNOWN turned CANCELLED; budget released; transfer still live at Airwallex")

    def test_cancelled_by_create_error_hides_later_progress(self):
        iid = self.incident()
        self.fake.lose_response_next = True
        p = self.pay_original(iid)
        self.fake.find_lag = 1; self.fake.dup_error_without_id = True
        self.svc.reconcile(p["request_id"])
        tid = next(iter(self.fake.by_id))
        self.fake.simulate(tid, "SENT")
        self.svc.reconcile(p["request_id"])               # SENT is a "late update" -> ignored
        self.assertEqual(self.svc.payment(p["request_id"])["status"], "SENT")

    # 6 ------------------------------------------------------------------
    def test_stop_pressed_during_presubmit_checks_is_ignored(self):
        iid = self.incident()
        ap = self.svc.approve(iid, "original", "zach")
        self.fake.on_get_beneficiary = lambda: self.svc.kill("jordan", "STOP!")
        try:
            self.svc.pay(ap, "original", BEN, 25_000, "USD")
        except Refused:
            pass
        self.assertEqual(self.fake.calls, 0, "transfer created after STOP was on")

    # 7 ------------------------------------------------------------------
    def test_shared_connection_stop_fails_while_another_request_in_tx(self):
        iid = self.incident()
        ap = self.svc.approve(iid, "original", "zach")
        orig = self.svc.s.committed_today

        def slow(now=None):
            time.sleep(0.3)
            return orig(now)
        self.svc.s.committed_today = slow
        outcome = {}

        def worker():
            try:
                outcome["result"] = self.svc.pay(ap, "original", BEN, 25_000, "USD")
            except Refused as e:  # expected: STOP landed first - captured, not printed
                outcome["refused"] = str(e)
            except Exception as e:  # anything else is a real failure
                outcome["error"] = repr(e)
        th = threading.Thread(target=worker)
        th.start(); time.sleep(0.1)
        try:
            self.svc.kill("jordan")               # the server's STOP button, same Service/connection
            err = None
        except sqlite3.OperationalError as e:
            err = e
        th.join()
        self.assertIsNone(err, f"STOP failed: {err}")
        self.assertNotIn("error", outcome, outcome.get("error"))
        self.assertTrue("refused" in outcome or self.fake.calls == 0 or outcome.get("result"), outcome)

    # 8 ------------------------------------------------------------------
    def test_fee_alert_overwrites_double_paid_and_agent_says_close(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
        r = self.svc.pay(self.svc.approve(iid, "replacement", "zach"), "replacement", BEN, 25_000, "USD")
        self.fake.simulate(r["provider_id"], "PAID"); self.svc.reconcile(r["request_id"])
        self.fake.by_id[p["provider_id"]]["status"] = "PAID"
        self.svc.reconcile(p["request_id"])
        self.assertEqual(self.svc._incident(iid)["alert"], "DOUBLE_PAID")
        t = self.fake.by_id[r["provider_id"]]; t["fee_amount"] = 30.0; t["amount_payer_pays"] = 280.0
        self.svc.reconcile(r["request_id"])               # "Check Airwallex" on the replacement
        d = run(self.svc, iid, RulesAgent())
        self.assertEqual(self.svc._incident(iid)["alert"], "DOUBLE_PAID", "DOUBLE_PAID alarm overwritten")
        self.assertNotEqual(d["decision"], "CLOSE")

    # 9 ------------------------------------------------------------------
    def test_return_after_close_is_a_dead_end(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fake.simulate(p["provider_id"], "SENT"); self.fake.simulate(p["provider_id"], "PAID")
        self.svc.reconcile(p["request_id"]); self.svc.close(iid, "zach")
        self.fake.simulate(p["provider_id"], "FAILED", "BENEFICIARY_BANK_RETURNED")
        self.svc.reconcile(p["request_id"])
        inc = self.svc._incident(iid)
        self.assertEqual(inc["alert"], "PAID_THEN_CANCELLED")
        self.assertNotEqual(inc["state"], "RESOLVED", "supplier unpaid but incident still shows RESOLVED")

    def test_sweeper_never_rechecks_paid(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fake.simulate(p["provider_id"], "PAID"); self.svc.reconcile(p["request_id"])
        self.fake.simulate(p["provider_id"], "FAILED", "BENEFICIARY_BANK_RETURNED")
        self.t += 86400
        self.svc.sweep()
        self.assertEqual(self.svc.payment(p["request_id"])["status"], "CANCELLED", "return never noticed by sweeper")

    # 10 -----------------------------------------------------------------
    def test_one_provider_error_aborts_whole_sweep(self):
        a, b = self.incident(), self.incident()
        pa, pb = self.pay_original(a), self.pay_original(b)
        self.fake.simulate(pb["provider_id"], "SENT")
        self.fake.get_transfer_error = pa["provider_id"]
        self.t += 80 * 3600
        try:
            self.svc.sweep()
        except ProviderError:
            pass
        self.assertEqual(self.svc.payment(pb["request_id"])["status"], "SENT", "payment B never re-read")
        self.assertEqual(self.svc._incident(b)["alert"], "OVERDUE_SLA", "SLA alarm never raised for B")

    # 11 -----------------------------------------------------------------
    def test_try_to_break_button_leaves_live_replacement_approval(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fake.simulate(p["provider_id"], "SENT"); self.svc.reconcile(p["request_id"])
        with self.assertRaises(Refused):                    # "Pay the supplier again right now"
            self.svc.approve(iid, "replacement", "zach")    # can't even be approved while in flight
        self.fake.simulate(p["provider_id"], "FAILED", "CHANNEL_TIMEOUT")
        self.svc.reconcile(p["request_id"])
        self.assertIsNone(self.svc.s.one("SELECT id FROM approvals WHERE role='replacement'"))

    # 12 -----------------------------------------------------------------
    def test_adapter_find_by_request_id_trusts_first_item(self):
        api = Airwallex(transport=lambda m, path, body: {"items": [{"id": "t-other", "request_id": "someone-else", "status": "PAID"}]})
        self.assertIsNone(api.find_by_request_id("mine"), "returned an unrelated transfer")

    # 13 -----------------------------------------------------------------
    def test_fraud_message_pushed_out_of_window(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.svc.supplier_message(iid, "We have new bank details, pay into our new account 55667788")
        for i in range(5):
            self.svc.supplier_message(iid, f"Any update? ({i})")
        self.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
        self.assertEqual(run(self.svc, iid, RulesAgent())["decision"], "ESCALATE")

    # 14 -----------------------------------------------------------------
    def test_agent_escalate_unresolves_closed_incident(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fake.simulate(p["provider_id"], "PAID"); self.svc.reconcile(p["request_id"]); self.svc.close(iid, "zach")
        with self.assertRaises(Refused):
            self.svc.record_decision(iid, "ESCALATE", ["llm said so"], "model")
        self.assertEqual(self.svc._incident(iid)["state"], "RESOLVED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
