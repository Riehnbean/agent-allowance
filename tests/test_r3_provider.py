"""Independent provider boundary review; fixture-only, no network."""
import os, sys, unittest, json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, os.environ.get("ALLOWANCE_REVIEW_BUNDLE", str(Path(__file__).resolve().parents[1] / "airwallex-verify-2a6c67c")))
from tests import test_allowance as T
from allowance.service import Refused

def cancelled():
    h=T.Base("setUp"); h.setUp()
    iid=h.incident(); p=h.pay_original(iid)
    h.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
    h.t+=2
    ap=h.svc.approve(iid,"replacement","zach")
    return h,iid,p,ap

class ProviderReview(unittest.TestCase):
    def test_malformed_nonempty_replies_do_not_send(self):
        values=[
            ("list", [1]), ("string","oops"), ("bool",True),
            ("missing status",{"id":"placeholder"}),
            ("missing identity",{"status":"CANCELLED","failure":{"message":"Channel timeout"}}),
            ("empty id",{"id":"","status":"CANCELLED","failure":{"message":"Channel timeout"}}),
            ("unknown status",{"id":"same","status":"UNKNOWN"}),
            ("reserved status",{"id":"same","status":"RESERVED"}),
            ("wrong case status",{"id":"same","status":"cancelled"}),
            ("null status",{"id":"same","status":None}),
            ("object status",{"id":"same","status":{"value":"CANCELLED"}}),
            ("list status",{"id":"same","status":["CANCELLED"]}),
            ("missing cancellation reason",{"id":"same","status":"CANCELLED"}),
            ("malformed failure",{"id":"same","status":"CANCELLED","failure":"Channel timeout"}),
            ("malformed details",{"id":"same","status":"CANCELLED","failure":{"message":"Channel timeout","details":"x"}}),
            ("unknown failure",{"id":"same","status":"CANCELLED","failure":{"message":"new unknown bank reason"}}),
        ]
        for label,response in values:
            with self.subTest(case=label):
                h,iid,p,ap=cancelled()
                if isinstance(response,dict) and response.get("id")=="same":
                    response={**response,"id":p["provider_id"]}
                count=h.fake.calls
                h.fake.by_req[p["request_id"]]["status"]="PAID"
                error=None; result=None
                with patch.object(h.fake,"get_transfer",return_value=response):
                    try: result=h.svc.pay(ap,"replacement",T.BEN,25000,"USD")
                    except Exception as e: error=type(e).__name__
                print(json.dumps({"case":label,"error":error,"new_creates":h.fake.calls-count,"result":result and result["status"]}))
                self.assertEqual(h.fake.calls,count)
                self.assertIsNone(result)
                h.svc.s.db.close()

    def test_contradictory_and_other_identity_replies_do_not_send(self):
        variants=[
            ("wrong id",lambda p:{"id":"other-transfer"}),
            ("wrong request",lambda p:{"id":p["provider_id"],"request_id":"other-request"}),
            ("wrong id correct request",lambda p:{"id":"other-transfer","request_id":p["request_id"]}),
            ("wrong request no id",lambda p:{"request_id":"other-request"}),
            ("wrong payee",lambda p:{"id":p["provider_id"],"beneficiary_id":"other-beneficiary"}),
            ("wrong currency",lambda p:{"id":p["provider_id"],"transfer_currency":"EUR"}),
            ("wrong amount",lambda p:{"id":p["provider_id"],"transfer_amount":"249.99"}),
            ("structured id",lambda p:{"id":{"id":p["provider_id"]}}),
            ("structured request",lambda p:{"id":p["provider_id"],"request_id":[p["request_id"]]}),
        ]
        for label,build in variants:
            with self.subTest(case=label):
                h,iid,p,ap=cancelled()
                response={"status":"CANCELLED","failure":{"message":"Channel timeout"},**build(p)}
                count=h.fake.calls
                error=None
                with patch.object(h.fake,"get_transfer",return_value=response):
                    try:h.svc.pay(ap,"replacement",T.BEN,25000,"USD")
                    except Exception as e:error=type(e).__name__
                alert=h.svc.evidence(iid)["incident"]["alert"]
                print(json.dumps({"case":label,"error":error,"new_creates":h.fake.calls-count,"alert":alert}))
                self.assertEqual(h.fake.calls,count)
                self.assertEqual(alert,"PROVIDER_MISMATCH")
                h.svc.s.db.close()

    def test_fresh_real_paid_reply_blocks_replacement(self):
        h,iid,p,ap=cancelled()
        h.fake.by_req[p["request_id"]]["status"]="PAID"
        before=h.fake.calls
        with self.assertRaises(Refused):
            h.svc.pay(ap,"replacement",T.BEN,25000,"USD")
        self.assertEqual(h.fake.calls,before)
        self.assertEqual(h.svc.payment(p["request_id"])["status"],"PAID")
        h.svc.s.db.close()

    def test_valid_cancellation_still_permits_single_replacement(self):
        h,iid,p,ap=cancelled()
        before=h.fake.calls
        result=h.svc.pay(ap,"replacement",T.BEN,25000,"USD")
        self.assertEqual(result["status"],"SCHEDULED")
        self.assertEqual(h.fake.calls,before+1)
        h.svc.s.db.close()

    def test_known_paid_return_states_are_not_discarded(self):
        for status in ("RETURNED","REVERSED","SUSPENDED"):
            with self.subTest(status=status):
                h=T.Base("setUp"); h.setUp()
                iid=h.incident(); p=h.pay_original(iid)
                h.fake.by_req[p["request_id"]]["status"]="PAID"
                h.svc.reconcile(p["request_id"])
                h.svc.close(iid,"zach")
                h.fake.by_req[p["request_id"]]["status"]=status
                observed=h.svc.reconcile(p["request_id"])
                inc=h.svc.evidence(iid)["incident"]
                print(json.dumps({"provider_status":status,"stored_status":observed["status"],"state":inc["state"],"alert":inc["alert"]}))
                self.assertEqual(observed["status"],status)
                self.assertEqual(inc["alert"],"PAID_THEN_FAILED")
                self.assertEqual(inc["state"],"ESCALATED")
                h.svc.s.db.close()

if __name__=="__main__":
    unittest.main(verbosity=2)

