"""An in-memory stand-in for the PayPal Payouts sandbox, so the PayPal rail runs and is tested with no keys.

It speaks PayPal's own JSON shapes (batch_header, items[].transaction_status, errors{name,message},
links[]) to the REAL adapter (allowance/paypal_adapter.py), which is plugged in as its transport. So the
adapter's translation - status mapping, duplicate handling, receiver checks - is exercised end to end.
Nothing leaves the machine.

    api = demo_paypal()        # a PayPalPayouts adapter wired to this fake
    api.fake.simulate(batch_id, "UNCLAIMED")
"""
import copy
import random
import string
import threading
import time

from .adapter import Ambiguous, ProviderError

BASE = "https://api-m.sandbox.paypal.com"

# Desk / Airwallex-style names the demo UI and seed use -> what PayPal would show
_ALIAS = {"PAID": "SUCCESS", "SENT": "PENDING", "SCHEDULED": "PENDING", "PROCESSING": "PENDING"}
_FAIL_AS = {  # desk failure type -> (PayPal item status, errors.name)
    "CHANNEL_TIMEOUT": ("FAILED", "INTERNAL_ERROR"), "SYSTEM_ERROR": ("FAILED", "INTERNAL_ERROR"),
    "INSUFFICIENT_FUNDS": ("FAILED", "INSUFFICIENT_FUNDS"),
    "INVALID_BENEFICIARY_DETAILS": ("FAILED", "RECEIVER_UNREGISTERED"), "INVALID_ACCOUNT_NAME_OR_NUMBER": ("FAILED", "INVALID_EMAIL"),
    "ACCOUNT_CLOSED": ("FAILED", "RECEIVER_ACCOUNT_LOCKED"), "ACCOUNT_UNDER_RESTRICTION": ("FAILED", "RECEIVER_ACCOUNT_LOCKED"),
    "BENEFICIARY_BANK_RETURNED": ("RETURNED", None), "RECALL_REQUESTED": ("REVERSED", None),
    "BENEFICIARY_REQUESTED": ("REFUNDED", None), "TM_SUSPENDED": ("BLOCKED", None),
}
_ERR_MSG = {"RECEIVER_UNREGISTERED": "The recipient for this payout does not have an account.",
            "INTERNAL_ERROR": "An error occurred while processing this payout request.",
            "INSUFFICIENT_FUNDS": "You have insufficient funds in your PayPal balance."}


def _id(n=13):
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))


class FakePayPalAPI:
    """Callable transport: (method, path, body, headers) -> PayPal-shaped JSON, or ProviderError/Ambiguous."""

    def __init__(self):
        self.lock = threading.Lock()
        self.batches, self.by_sender, self.by_request_id, self.items = {}, {}, {}, {}
        self.posts = 0                    # POSTs that reached "PayPal"
        self.lose_response_next = False   # payout IS created, caller sees a timeout
        self.fail_5xx_next = False        # 5xx before anything is created
        self.honor_request_id = True      # PayPal-Request-Id replay; off -> only sender_batch_id dedupe

    def __call__(self, method, path, body=None, headers=None):
        headers = headers or {}
        if method == "POST" and path == "/v1/payments/payouts":
            return self._create(body, headers)
        if method == "GET" and path.startswith("/v1/payments/payouts/"):
            bid = path.rsplit("/", 1)[1].split("?")[0]
            with self.lock:
                if bid not in self.batches:
                    raise ProviderError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
                return copy.deepcopy(self.batches[bid])
        if method == "GET" and path.startswith("/v1/payments/payouts-item/"):
            iid = path.rsplit("/", 1)[1]
            with self.lock:
                if iid not in self.items:
                    raise ProviderError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.")
                return copy.deepcopy(self.items[iid])
        raise ProviderError(404, "NOT_FOUND", f"fake PayPal has no {method} {path}")

    def _create(self, body, headers):
        with self.lock:
            if self.fail_5xx_next:
                self.fail_5xx_next = False
                raise Ambiguous("POST /v1/payments/payouts: HTTP 503")
            self.posts += 1
            rid = headers.get("PayPal-Request-Id")
            if rid and self.honor_request_id and rid in self.by_request_id:
                return copy.deepcopy(self.by_request_id[rid])
            sbid = (body.get("sender_batch_header") or {}).get("sender_batch_id")
            items = body.get("items") or []
            if not sbid or not items:
                raise ProviderError(400, "VALIDATION_ERROR", "Invalid request - see details.")
            if sbid in self.by_sender:
                old = self.by_sender[sbid]
                # Exactly the shape the real sandbox returned on Nov 3 2026 (the link sits inside details[]).
                raise ProviderError(400, "USER_BUSINESS_ERROR", "User business error.", {
                    "name": "USER_BUSINESS_ERROR", "message": "User business error.", "links": [],
                    "details": [{"field": "SENDER_BATCH_ID", "location": "body", "issue": "Batch with given sender_batch_id already exists",
                                 "link": [{"href": f"{BASE}/v1/payments/payouts/{old}", "rel": "self", "method": "GET"}]}]})
            bid = _id()
            its = []
            total = 0
            for it in items:
                iid = _id()
                total += round(float(it["amount"]["value"]) * 100)
                item = {"payout_item_id": iid, "transaction_status": "PENDING", "payout_batch_id": bid,
                        "payout_item_fee": {"currency": it["amount"]["currency"], "value": "0.00"},
                        "payout_item": {k: copy.deepcopy(it[k]) for k in ("recipient_type", "amount", "note", "receiver", "sender_item_id") if k in it},
                        "time_processed": None, "links": [{"href": f"{BASE}/v1/payments/payouts-item/{iid}", "rel": "item", "method": "GET"}]}
                self.items[iid] = item
                its.append(item)
            cur = items[0]["amount"]["currency"]
            hdr = {"payout_batch_id": bid, "batch_status": "PENDING", "time_created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "sender_batch_header": copy.deepcopy(body["sender_batch_header"]),
                   "amount": {"currency": cur, "value": f"{total // 100}.{total % 100:02d}"}}
            self.batches[bid] = {"batch_header": hdr, "items": its, "links": []}
            self.by_sender[sbid] = bid
            resp = {"batch_header": copy.deepcopy({k: hdr[k] for k in ("payout_batch_id", "batch_status", "sender_batch_header")}),
                    "links": [{"href": f"{BASE}/v1/payments/payouts/{bid}", "rel": "self", "method": "GET"}]}
            if rid:
                self.by_request_id[rid] = resp
            if self.lose_response_next:
                self.lose_response_next = False
                raise Ambiguous("read timeout after POST /v1/payments/payouts")
            return copy.deepcopy(resp)

    def item_of(self, batch_id):
        return self.batches[batch_id]["items"][0]

    def simulate(self, batch_id, next_status, failure_type=None):
        """Move the (single) item. Accepts PayPal statuses (SUCCESS, UNCLAIMED, ONHOLD, FAILED, RETURNED, BLOCKED,
        REFUNDED, REVERSED) and, for the demo UI, the desk's own names (PAID, SENT, FAILED + a desk failure type)."""
        with self.lock:
            b = self.batches[batch_id]
            it = b["items"][0]
            st, err = _ALIAS.get(next_status, next_status), None
            if st == "FAILED" and failure_type:
                st, err = _FAIL_AS.get(failure_type, ("FAILED", failure_type))
            if st == "UNCLAIMED":
                err = "RECEIVER_UNREGISTERED"
            it["transaction_status"] = st
            if err:
                it["errors"] = {"name": err, "message": _ERR_MSG.get(err, err.replace("_", " ").capitalize())}
            else:
                it.pop("errors", None)
            if st == "SUCCESS":
                it["transaction_id"] = it.get("transaction_id") or _id(17)
            b["batch_header"]["batch_status"] = "PENDING" if st == "PENDING" else "SUCCESS"


def demo_paypal(book=None):
    """A PayPalPayouts adapter wired to the in-memory fake. `.fake` is the fake PayPal."""
    from .paypal_adapter import PayeeBook, PayPalPayouts
    fake = FakePayPalAPI()
    api = PayPalPayouts(client_id="demo", client_secret="demo", transport=fake, book=book or PayeeBook())
    api.fake = fake
    return api
