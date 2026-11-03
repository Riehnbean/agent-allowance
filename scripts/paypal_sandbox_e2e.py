"""One live payout through the PayPal Payouts SANDBOX (fake money), driven by the real desk.

    python3 scripts/paypal_sandbox_e2e.py          # skips cleanly if no keys
    python3 scripts/paypal_sandbox_e2e.py --save   # also writes receipts/paypal_sandbox_e2e.json

Keys: PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET from the environment, or from an .env file
(PAYPAL_ENV_FILE, else ../.env next to the repo). Values are never printed.
Receiver: PAYPAL_SANDBOX_RECEIVER (a sandbox personal account email from the PayPal developer dashboard).
Without it the payout goes to an address with no PayPal account, which PayPal leaves UNCLAIMED - also a
useful check, because the desk must hold it as in flight.

What it checks, live:
  1. OAuth2 token, POST /v1/payments/payouts through service.pay (caps, approval, payee re-read all apply)
  2. the same request_id sent again returns the SAME payout (PayPal-Request-Id replay)
  3. the same sender_batch_id under a NEW PayPal-Request-Id is rejected as a duplicate and the adapter
     follows the link to the original (prints PayPal's error name/message - the part we could not verify offline)
  4. GET /v1/payments/payouts/{id} polled until the item leaves PENDING; prints PayPal status -> desk status
"""
import json
import os
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from allowance.adapter import Ambiguous, ProviderError  # noqa: E402
from allowance.paypal_adapter import PayeeBook, PayPalPayouts  # noqa: E402
from allowance.service import Service  # noqa: E402
from allowance.store import Store  # noqa: E402


def load_env():
    path = os.environ.get("PAYPAL_ENV_FILE") or os.path.join(os.path.dirname(HERE), ".env")
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return
    for line in lines:
        k, sep, v = line.strip().partition("=")
        k = k.replace("export ", "").strip()
        if sep and k.startswith("PAYPAL_") and not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")


def main():
    load_env()
    if not (os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET")):
        print("skipped: PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set (sandbox keys) - nothing was sent")
        return 0
    tmp = tempfile.mkdtemp(prefix="paypal-e2e-")
    api = PayPalPayouts(book=PayeeBook(os.path.join(tmp, "paypal.json")))
    svc = Service(Store(os.path.join(tmp, "e2e.db")), api)
    svc.s.init_policy("USD", per_tx_cap=1_000, daily_cap=5_000)    # $10 / $50: this script sends $1.00
    receiver = os.environ.get("PAYPAL_SANDBOX_RECEIVER") or f"agent-allowance-unclaimed-{uuid.uuid4().hex[:8]}@example.com"
    out = {"environment": "PayPal Payouts SANDBOX - no real money", "receiver_is_sandbox_account": bool(os.environ.get("PAYPAL_SANDBOX_RECEIVER"))}

    ben = api.create_beneficiary({"nickname": "e2e payee", "beneficiary": {"entity_type": "COMPANY", "company_name": "Maple Lumber Supply LLC",
                                  "type": "PAYPAL", "paypal": {"recipient_type": "EMAIL", "receiver": receiver}}})["id"]
    svc.allow_beneficiary(ben, "Maple Lumber Supply LLC", "PayPal e2e receiver", "zach")
    iid = svc.open_incident("Maple Lumber", f"PP-E2E-{int(time.time())}", ben, 100, "USD")
    ap = svc.approve(iid, "original", "zach")
    p = svc.pay(ap, "original", ben, 100, "USD")
    print(f"1. payout sent through the desk: desk status {p['status']}, payout_batch_id {p['provider_id']}")
    out["payout_batch_id"], out["first_status"] = p["provider_id"], p["status"]
    if not p["provider_id"]:
        print("   no batch id yet (reply lost?) - the desk holds it; see the receipt")
    else:
        again = api.create_transfer(p["request_id"], ben, 100, "USD", "replay check")
        same = again["id"] == p["provider_id"]
        print(f"2. same request_id again -> same payout: {same}")
        out["replay_same_payout"] = same
        body = {"sender_batch_header": {"sender_batch_id": p["request_id"]},
                "items": [{"recipient_type": "EMAIL", "receiver": receiver, "sender_item_id": p["request_id"],
                           "amount": {"value": "1.00", "currency": "USD"}}]}
        try:
            r = api._raw("POST", "/v1/payments/payouts", body, {"PayPal-Request-Id": str(uuid.uuid4())})
            dup = {"rejected": False, "payout_batch_id": (r.get("batch_header") or {}).get("payout_batch_id")}
            print("3. WARNING: PayPal accepted a reused sender_batch_id under a new PayPal-Request-Id:", dup)
        except ProviderError as e:
            link = api._batch_link(e.details)
            dup = {"rejected": True, "http": e.status, "name": e.code, "message": e.message[:200],
                   "recognised_as_duplicate": api._is_duplicate(e), "links_to_original": link == p["provider_id"]}
            print(f"3. reused sender_batch_id rejected: HTTP {e.status} {e.code} - {e.message[:120]!r}; "
                  f"adapter recognises it: {dup['recognised_as_duplicate']}; link to original: {dup['links_to_original']}")
        except Ambiguous as e:
            dup = {"rejected": None, "ambiguous": str(e)[:200]}
            print("3. duplicate probe was ambiguous:", e)
        out["duplicate_sender_batch_id"] = dup
        for _ in range(20):
            r = svc.reconcile(p["request_id"])
            raw = api.get_transfer(p["provider_id"])
            if raw["paypal"].get("status") != "PENDING":
                break
            time.sleep(3)
        print(f"4. PayPal item {raw['paypal'].get('status')} -> desk {r['status']}"
              + (f" ({r['failure_type']})" if r["failure_type"] else "") + f"; fee {raw.get('fee_amount')}")
        out["paypal_status"], out["desk_status"], out["failure_type"] = raw["paypal"].get("status"), r["status"], r["failure_type"]
    rc = svc.receipt(iid)
    out["log_intact"] = rc["log_integrity"]["intact"]
    out["events"] = [{"kind": e["kind"], "provenance": e["provenance"]} for e in rc["events"]]
    print(f"   evidence log intact: {out['log_intact']} ({len(out['events'])} entries)")
    if "--save" in sys.argv:
        path = os.path.join(HERE, "receipts", "paypal_sandbox_e2e.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(out, open(path, "w"), indent=1)
        print("   saved", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
