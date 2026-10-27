"""Independent review regressions: expected safety behavior, offline fakes only.

These intentionally fail against bundle 85c74e1; no application files changed.
"""
import unittest
from unittest.mock import patch
from tests.test_allowance import Base, BEN, bank
from allowance.adapter import Ambiguous
from allowance.service import Refused


class PaymentReview(Base):
    def test_unknown_payment_reserves_its_possible_fee(self):
        self.svc.s.db.execute("UPDATE policy SET daily_cap=54000, dual_approval_over=100000")
        self.fake.fee = 2500
        self.fake.lose_response_next = True
        first = self.pay_original(self.incident())
        self.assertEqual(first['status'], 'UNKNOWN')
        # 2 x ($250 + $25 possible fee) exceeds the $540 daily limit.
        with self.assertRaises(Refused):
            self.pay_original(self.incident())

    def test_cancelled_payment_keeps_its_nonrefundable_fee_in_budget(self):
        self.fake.fee = 2500
        p = self.pay_original(self.incident())
        self.fail_with(p['request_id'], 'CHANNEL_TIMEOUT')
        self.assertEqual(self.svc.s.committed_today(self.t), 2500)

    def test_operator_resend_requires_unexpired_approval(self):
        iid = self.incident()
        ap = self.svc.approve(iid, 'original', 'zach', ttl=1)
        # Crash after durable reservation, before the first network send.
        with patch.object(self.svc, 'submit', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.svc.pay(ap, 'original', BEN, 25000, 'USD')
        rid = self.svc.s.one('SELECT request_id FROM payments')['request_id']
        self.t += 120
        with self.assertRaises(Refused):
            self.svc.resend(rid, 'zach')
        self.assertEqual(self.fake.calls, 0)

    def test_paid_then_failed_is_not_still_reported_paid(self):
        p = self.pay_original(self.incident())
        self.fake.by_req[p['request_id']]['status'] = 'PAID'
        self.svc.reconcile(p['request_id'])
        # Airwallex's non-final FAILED is not synonymous with CANCELLED.
        self.fake.by_req[p['request_id']]['status'] = 'FAILED'
        self.svc.reconcile(p['request_id'])
        self.assertNotEqual(self.svc.payment(p['request_id'])['status'], 'PAID')

    def test_unknown_webhooks_do_not_allocate_durable_dedupe_rows(self):
        for i in range(50):
            self.svc.on_webhook({'id': f'unknown-{i}', 'data': {'id': 'not-ours'}})
        self.assertEqual(self.svc.s.one('SELECT COUNT(*) n FROM webhook_events')['n'], 0)

    def test_reconcile_rejects_a_different_provider_transfer(self):
        p = self.pay_original(self.incident())
        wrong = {'id': 'somebody-elses-transfer', 'request_id': 'other-request',
                 'status': 'PAID', 'beneficiary_id': 'other-payee',
                 'transfer_amount': '999.00', 'transfer_currency': 'EUR'}
        with patch.object(self.fake, 'get_transfer', return_value=wrong):
            self.svc.reconcile(p['request_id'])
        self.assertNotEqual(self.svc.payment(p['request_id'])['status'], 'PAID')

    def test_replacement_requires_a_current_provider_read(self):
        iid = self.incident()
        p = self.pay_original(iid)
        self.fail_with(p['request_id'], 'CHANNEL_TIMEOUT')
        self.t += 2
        ap = self.svc.approve(iid, 'replacement', 'zach')
        # Provider reverses the cancellation after the last cached GET.
        self.fake.by_req[p['request_id']]['status'] = 'PAID'
        with self.assertRaises(Refused):
            self.svc.pay(ap, 'replacement', BEN, 25000, 'USD')


class AuthorityReview(Base):
    def setUp(self):
        super().setUp()
        # The supplied server test module sets only fake local operator identities.
        from tests import test_break_server
        self.server = test_break_server.server
        self.old = (self.server.SVC, self.server.API, self.server.OPERATORS)
        self.server.SVC, self.server.API = self.svc, self.fake
        self.server.OPERATORS = {'test-a': 'zach', 'test-b': 'jordan'}

    def tearDown(self):
        self.server.SVC, self.server.API, self.server.OPERATORS = self.old

    def test_operator_stop_cannot_erase_system_two_person_resume_requirement(self):
        self.svc.kill('system', 'orphan transfer detected')
        self.server.handle('POST', '/api/kill', {}, 'zach')
        try:
            self.server.handle('POST', '/api/unkill', {'note': 'I pressed stop too, resuming now please'}, 'zach')
        except Refused:
            pass
        self.assertTrue(self.svc.s.policy()['killed'])

    def test_reverify_confirmation_is_bound_to_the_bank_details_requested(self):
        # Both humans reviewed account A; provider changes to account B before confirm.
        request = self.server.handle('POST', '/api/payee/reverify',
            {'note': 'Checked the original account using our signed supplier contract'}, 'zach')
        self.fake.bens[BEN] = bank(acct='999999999')
        with self.assertRaises(Refused):
            self.server.handle('POST', f"/api/pending/{request['pending']}/confirm", {}, 'jordan')


if __name__ == '__main__':
    unittest.main()
