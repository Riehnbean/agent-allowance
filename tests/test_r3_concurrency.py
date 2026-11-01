"""Offline round-three concurrency checks. FakeAirwallex only; no application edits."""
import json, os, sys, threading, unittest
from pathlib import Path
from unittest.mock import patch
BUNDLE=Path(os.environ.get("ALLOWANCE_REVIEW_BUNDLE", str(Path(__file__).resolve().parents[1]/"airwallex-verify-2a6c67c")))
sys.path.insert(0,str(BUNDLE))
from tests.test_allowance import Base, BEN
from allowance.service import Refused

class Concurrency(Base):
    def paid(self):
        iid=self.incident()
        p=self.pay_original(iid)
        self.fake.simulate(p["provider_id"],"PAID")
        self.svc.reconcile(p["request_id"])
        return iid,p

    def thread(self, fn, name):
        errors=[]
        def run():
            try: fn()
            except BaseException as e: errors.append(e)
        t=threading.Thread(target=run,name=name)
        t.start()
        return t,errors

    def stale_before_append(self, iid, decision, mutate):
        ev=self.svc.evidence(iid)
        reached,proceed=threading.Event(),threading.Event()
        old=self.svc.s.log
        def pause(actor,kind,*args,**kwargs):
            if actor=="agent" and kind=="decision":
                reached.set()
                if not proceed.wait(5): raise RuntimeError("pause timeout")
            return old(actor,kind,*args,**kwargs)
        with patch.object(self.svc.s,"log",side_effect=pause):
            t,errs=self.thread(lambda:self.svc.record_decision(iid,decision,["old"],"decided",evidence=ev),"decision")
            self.assertTrue(reached.wait(5))
            try: mutate()
            finally: proceed.set()
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(errs),1,errs)
        self.assertIsInstance(errs[0],Refused)
        self.assertEqual(len(self.svc.s.q("SELECT id FROM events WHERE actor='agent' AND kind='decision' AND incident_id=?",iid)),0)

    def test_webhook_before_guard_refuses_stale_wait(self):
        iid=self.incident(); p=self.pay_original(iid)
        self.fake.simulate(p["provider_id"],"PAID")
        self.stale_before_append(iid,"WAIT",lambda:self.svc.on_webhook({"id":"concurrent","data":{"id":p["provider_id"],"status":"PAID"}}))

    def test_sweeper_before_guard_refuses_stale_wait(self):
        iid=self.incident(); p=self.pay_original(iid)
        self.fake.simulate(p["provider_id"],"PAID")
        self.t+=601
        self.stale_before_append(iid,"WAIT",lambda:self.svc.sweep(stale_after=0))

    def test_payment_before_guard_refuses_stale_replace(self):
        iid=self.incident(); p=self.pay_original(iid)
        self.fail_with(p["request_id"],"CHANNEL_TIMEOUT")
        ap=self.svc.approve(iid,"replacement","zach")
        self.stale_before_append(iid,"REPLACE",lambda:self.svc.pay(ap,"replacement",BEN,25000,"USD"))

    def test_guard_holds_lock_through_append(self):
        iid=self.incident(); self.pay_original(iid)
        ev=self.svc.evidence(iid)
        reached,proceed,started,done=threading.Event(),threading.Event(),threading.Event(),threading.Event()
        old=self.svc.s._log
        def pause(actor,kind,*args,**kwargs):
            if actor=="agent" and kind=="decision":
                reached.set()
                if not proceed.wait(5): raise RuntimeError("pause timeout")
            return old(actor,kind,*args,**kwargs)
        def update():
            started.set()
            self.svc.supplier_message(iid,"Use our new bank details")
            done.set()
        with patch.object(self.svc.s,"_log",side_effect=pause):
            t,errs=self.thread(lambda:self.svc.record_decision(iid,"WAIT",["old"],"decided",evidence=ev),"decision")
            self.assertTrue(reached.wait(5))
            u,uerrs=self.thread(update,"supplier")
            self.assertTrue(started.wait(5))
            self.assertFalse(done.wait(.1),"supplier mutation interleaved inside guard+append")
            proceed.set(); t.join(5); u.join(5)
        self.assertFalse(t.is_alive() or u.is_alive())
        self.assertEqual(errs+uerrs,[])
        rows=self.svc.s.q("SELECT id,actor,kind FROM events WHERE incident_id=? AND ((actor='agent' AND kind='decision') OR kind='supplier_message') ORDER BY id",iid)
        self.assertEqual([r["actor"] for r in rows],["agent","supplier"])

    def test_reversal_alarm_must_precede_replacement(self):
        iid,p=self.paid()
        # Provider reversal carries a retryable reason, but ANY PAID reversal must be reviewed first.
        self.fake.by_id[p["provider_id"]].update(status="CANCELLED",failure={"message":"Channel timeout","details":{"type":"INCORRECT_ROUTING"}})
        reached,proceed=threading.Event(),threading.Event()
        old=self.svc._anomaly
        def pause(rid,code,detail):
            if rid==p["request_id"] and code=="PAID_THEN_CANCELLED":
                reached.set()
                if not proceed.wait(5): raise RuntimeError("pause timeout")
            return old(rid,code,detail)
        result=None
        with patch.object(self.svc,"_anomaly",side_effect=pause):
            t,errs=self.thread(lambda:self.svc.on_webhook({"id":"reverse","data":{"id":p["provider_id"],"status":"CANCELLED"}}),"webhook")
            self.assertTrue(reached.wait(5))
            before=self.fake.calls
            # Claude's fix publishes status + alarm in one locked transaction, so an operator who acts during
            # the gap simply waits for it. Run the operator in its own thread, let it queue, then release.
            box={}
            def operator():
                try:
                    ap=self.svc.approve(iid,"replacement","zach")
                    box["r"]=self.svc.pay(ap,"replacement",BEN,25000,"USD")
                except Refused as e: box["refused"]=str(e)
            op=threading.Thread(target=operator); op.start(); op.join(0.3)
            proceed.set(); t.join(5); op.join(5)
            snapshot=dict(self.svc._incident(iid)); result=box.get("r")
        self.assertFalse(t.is_alive()); self.assertEqual(errs,[])
        print("REVERSAL_GAP",json.dumps({"intermediate_incident":snapshot,"replacement_status":result and result["status"],"create_delta":self.fake.calls-before,"final_incident":dict(self.svc._incident(iid))}))
        self.assertEqual(self.fake.calls,before,"replacement escaped before reversal alarm was set")

    def test_close_must_recheck_after_provider_reversal(self):
        iid,p=self.paid()
        reached,proceed=threading.Event(),threading.Event()
        old=self.svc.s.tx
        def pause():
            if threading.current_thread().name=="operator-close":
                reached.set()
                if not proceed.wait(5): raise RuntimeError("pause timeout")
            return old()
        with patch.object(self.svc.s,"tx",side_effect=pause):
            t,errs=self.thread(lambda:self.svc.close(iid,"zach"),"operator-close")
            self.assertTrue(reached.wait(5))
            self.fake.by_id[p["provider_id"]].update(status="CANCELLED",failure={"message":"Channel timeout","details":{"type":"INCORRECT_ROUTING"}})
            self.svc.reconcile(p["request_id"])
            alarmed=dict(self.svc._incident(iid))
            proceed.set(); t.join(5)
        self.assertFalse(t.is_alive())
        self.assertTrue(all(isinstance(e,Refused) for e in errs),errs)
        final=dict(self.svc._incident(iid))
        print("CLOSE_RACE",json.dumps({"alarmed":alarmed,"final":final,"errors":[str(e) for e in errs]}))
        self.assertNotEqual(final["state"],"RESOLVED","close overwrote a concurrent reversal escalation")

if __name__=="__main__": unittest.main(verbosity=2)

