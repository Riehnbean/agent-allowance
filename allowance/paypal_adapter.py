"""Trusted PayPal Payouts adapter (sandbox only) - a second payment rail for the same incident desk.

It exposes the same methods the service uses on the Airwallex adapter (create_transfer, get_transfer,
find_by_request_id, get_beneficiary, usd_balance_minor, ...) and returns transfer records in the same
shape (id, request_id, status, transfer_amount, failure{...}), so service.py - caps, STOP, single-use
approvals, the duplicate lock, reconcile and every alarm - runs unchanged on top of it.

Sandbox only: the base URL must be exactly https://api-m.sandbox.paypal.com; anything else is refused
in the constructor, even in tests, so this code cannot move real money.

PayPal calls used
-----------------
  POST /v1/oauth2/token                 client-credentials token (Basic client_id:secret)
  POST /v1/payments/payouts             one batch with ONE item; sender_batch_id = our request_id,
                                        items[0].sender_item_id = our request_id, and the
                                        PayPal-Request-Id header = our request_id
  GET  /v1/payments/payouts/{batch_id}  batch + item status (our "transfer id" is the payout_batch_id)

Idempotency: PayPal "does not process duplicate payouts. If you specify a sender_batch_id that was
used in the last 30 days, the API rejects the request with an error message that shows the duplicate
sender_batch_id and includes a HATEOAS link to the original payout"; and on an HTTP 5nn "you can safely
retry the request with the same sender_batch_id". So a re-send of the same request_id can never pay
twice: a duplicate rejection is followed to the original batch and adopted.

PayPal has no "find payout by sender_batch_id" read. The adapter therefore keeps a small local ledger
(request_id -> payout_batch_id, payee, receiver), written BEFORE the POST. If the reply to the POST is
lost, find_by_request_id raises Ambiguous (could not look) rather than returning None (not there): the
payment stays held, and the only way forward is an operator re-send with the same sender_batch_id,
which PayPal dedupes. release() refuses in that case.

Payees: PayPal pays an email / PayPal ID, not a bank account. A payee record lives in the same local
store as {"beneficiary": {"company_name": ..., "paypal": {"recipient_type": "EMAIL", "receiver": ...}}}.
details_hash() covers the receiver, so a receiver changed after approval trips the same
BENEFICIARY_CHANGED alarm, system STOP (two people to resume) and two-person "accept new details"
flow as an edited bank account.

Status mapping (PayPal item transaction_status -> desk status; failure class in service.py)
-------------------------------------------------------------------------------------------
  | PayPal                          | Desk        | Failure type (class)                                  |
  |---------------------------------|-------------|-------------------------------------------------------|
  | SUCCESS                         | PAID        | -                                                     |
  | PENDING                         | PROCESSING  | - (in flight: duplicate lock holds, no replacement)   |
  | ONHOLD                          | PROCESSING  | - (in flight)                                         |
  | UNCLAIMED                       | SENT        | - (in flight: money left us, receiver has not claimed;|
  |                                 |             |   PayPal returns it after 30 days -> RETURNED)        |
  | FAILED                          | CANCELLED   | by errors.name, see FAILED_ERRORS below               |
  | RETURNED                        | CANCELLED   | BENEFICIARY_BANK_RETURNED (DETAILS_PROBLEM)           |
  | BLOCKED                         | CANCELLED   | TM_SUSPENDED (DELIBERATE)                             |
  | REFUNDED                        | CANCELLED   | BENEFICIARY_REQUESTED (DELIBERATE)                    |
  | REVERSED                        | CANCELLED   | RECALL_REQUESTED (DELIBERATE)                         |
  | (no item yet) batch PENDING /   | PROCESSING  | -                                                     |
  |   PROCESSING                    |             |                                                       |
  | (no item yet) batch DENIED      | CANCELLED   | UNCLASSIFIED (never auto-retried)                     |
  | (no item yet) batch CANCELED    | CANCELLED   | CLIENT_REQUESTED (DELIBERATE)                         |
  | anything else                   | (refused)   | raised as Ambiguous - not an observation              |

  FAILED errors.name -> failure type:
    RECEIVER_UNREGISTERED, INVALID_EMAIL, PHONE_NUMBER_INVALID, RECEIVER_UNCONFIRMED,
    RECEIVER_ACCOUNT_UNVERIFIED, UNVERIFIED_RECIPIENT_NOT_SUPPORTED, RECEIVER_ACCOUNT_NOT_ELIGIBLE,
    RECEIVER_YOUTH_ACCOUNT                         -> INVALID_BENEFICIARY_DETAILS (DETAILS_PROBLEM)
    RECEIVER_ACCOUNT_LOCKED, RECEIVER_ACCOUNT_LIMITATION -> ACCOUNT_UNDER_RESTRICTION (DETAILS_PROBLEM)
    CURRENCY_NOT_SUPPORTED_FOR_RECEIVER, RECEIVER_REFUSED -> ACCOUNT_CURRENCY_MISMATCH (DETAILS_PROBLEM)
    INSUFFICIENT_FUNDS, NEGATIVE_BALANCE           -> INSUFFICIENT_FUNDS (RETRYABLE)
    INTERNAL_ERROR, UNDEFINED                      -> SYSTEM_ERROR (RETRYABLE)
    RISK_DECLINE, REGULATORY_BLOCKED               -> TM_SUSPENDED (DELIBERATE)
    TRANSACTION_LIMIT_EXCEEDED, RECEIVING_LIMIT_EXCEEDED, SPENDING_LIMIT_EXCEEDED
                                                   -> EXCEEDED_TRANSACTION_AMOUNT_OR_LIMIT (no class:
                                                      a person decides)
    RECEIVER_COUNTRY_NOT_ALLOWED, RECEIVER_STATE_RESTRICTED, CLOSED_MARKET -> CHANNEL_POLICY (no class)
    any other / missing name                       -> UNCLASSIFIED (never auto-retried)

  A RETURNED/REFUNDED/REVERSED read after SUCCESS becomes CANCELLED after PAID, which the service
  alarms exactly like a bank return on Airwallex (PAID_THEN_CANCELLED, incident frozen).

PayPal docs used (checked Nov 3 2026)
  https://developer.paypal.com/api/payments.payouts-batch/v1/            (create / show batch, item)
  https://developer.paypal.com/api/payments.payouts-batch/v1/schema.yaml (field names, status and error enums)
  https://developer.paypal.com/api/payments.payouts-batch/v1/definitions/payout_batch_items
  https://developer.paypal.com/payouts/use-payouts/track-payout-item-status
  https://developer.paypal.com/api/rest/authentication/                  (client-credentials token)
"""
import base64
import http.client
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

from .adapter import Ambiguous, ProviderError, minor_to_str, str_to_minor

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"

STATUS = {"SUCCESS": "PAID", "PENDING": "PROCESSING", "ONHOLD": "PROCESSING", "UNCLAIMED": "SENT"}
CLOSED = {"RETURNED": "BENEFICIARY_BANK_RETURNED", "BLOCKED": "TM_SUSPENDED",
          "REFUNDED": "BENEFICIARY_REQUESTED", "REVERSED": "RECALL_REQUESTED"}
FAILED_ERRORS = {}
for _names, _t in (
        (("RECEIVER_UNREGISTERED", "INVALID_EMAIL", "PHONE_NUMBER_INVALID", "RECEIVER_UNCONFIRMED", "RECEIVER_ACCOUNT_UNVERIFIED",
          "UNVERIFIED_RECIPIENT_NOT_SUPPORTED", "RECEIVER_ACCOUNT_NOT_ELIGIBLE", "RECEIVER_YOUTH_ACCOUNT"), "INVALID_BENEFICIARY_DETAILS"),
        (("RECEIVER_ACCOUNT_LOCKED", "RECEIVER_ACCOUNT_LIMITATION"), "ACCOUNT_UNDER_RESTRICTION"),
        (("CURRENCY_NOT_SUPPORTED_FOR_RECEIVER", "RECEIVER_REFUSED"), "ACCOUNT_CURRENCY_MISMATCH"),
        (("INSUFFICIENT_FUNDS", "NEGATIVE_BALANCE"), "INSUFFICIENT_FUNDS"),
        (("INTERNAL_ERROR", "UNDEFINED"), "SYSTEM_ERROR"),
        (("RISK_DECLINE", "REGULATORY_BLOCKED"), "TM_SUSPENDED"),
        (("TRANSACTION_LIMIT_EXCEEDED", "RECEIVING_LIMIT_EXCEEDED", "SPENDING_LIMIT_EXCEEDED"), "EXCEEDED_TRANSACTION_AMOUNT_OR_LIMIT"),
        (("RECEIVER_COUNTRY_NOT_ALLOWED", "RECEIVER_STATE_RESTRICTED", "CLOSED_MARKET"), "CHANNEL_POLICY")):
    for _n in _names:
        FAILED_ERRORS[_n] = _t
BATCH_ONLY = {"PENDING": ("PROCESSING", None), "PROCESSING": ("PROCESSING", None),
              "DENIED": ("CANCELLED", "UNCLASSIFIED"), "CANCELED": ("CANCELLED", "CLIENT_REQUESTED")}
_BATCH_LINK = re.compile(r"/v1/payments/payouts/([A-Za-z0-9_-]+)$")


class PayeeBook:
    """Local store for PayPal payees and the send ledger (request_id -> batch id, payee, receiver).
    path=None keeps it in memory (tests, demo). With a path, every write is atomic (temp file + rename)."""

    def __init__(self, path=None):
        self.path, self.lock = path, threading.Lock()
        self.data = {"payees": {}, "sent": {}}
        if path and os.path.exists(path):
            with open(path) as f:
                self.data = json.load(f)
            self.data.setdefault("payees", {})
            self.data.setdefault("sent", {})

    def _save(self):
        if not self.path:
            return
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".paypal-state-")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def put_payee(self, pid, rec):
        with self.lock:
            self.data["payees"][pid] = json.loads(json.dumps(rec))
            self._save()

    def payee(self, pid):
        with self.lock:
            r = self.data["payees"].get(pid)
            return json.loads(json.dumps(r)) if r else None

    def sent(self, request_id):
        with self.lock:
            r = self.data["sent"].get(request_id)
            return dict(r) if r else None

    def record(self, request_id, **kw):
        with self.lock:
            self.data["sent"].setdefault(request_id, {}).update({k: v for k, v in kw.items() if v is not None})
            self._save()


class PayPalPayouts:
    def __init__(self, client_id=None, client_secret=None, base=SANDBOX_BASE, timeout=30, transport=None, book=None):
        if base.rstrip("/") != SANDBOX_BASE:
            raise ValueError("refusing non-sandbox PayPal base URL (only https://api-m.sandbox.paypal.com is allowed)")
        self.client_id = client_id or os.environ.get("PAYPAL_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("PAYPAL_CLIENT_SECRET")
        self.base, self.timeout = SANDBOX_BASE, timeout
        self._token, self._exp = None, 0
        self.transport = transport  # tests / demo inject the in-memory fake (allowance/paypal_fake.py)
        self.book = book if book is not None else PayeeBook(os.environ.get("PAYPAL_STATE"))

    # ---- low level -------------------------------------------------------
    def _raw(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        if self.transport:
            return self.transport(method, path, body, headers)
        headers.setdefault("Content-Type", "application/json")
        headers["Authorization"] = "Bearer " + self._auth()
        data = None if body is None else json.dumps(body).encode()
        return self._http(method, path, data, headers)

    def _http(self, method, path, data, headers):
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
            try:
                return json.loads(raw) if raw else {}
            except ValueError:
                raise Ambiguous(f"{method} {path}: 2xx with an unreadable body") from None
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except ValueError:
                err = {}
            if e.code >= 500:
                raise Ambiguous(f"{method} {path}: HTTP {e.code}") from None
            raise ProviderError(e.code, err.get("name") or err.get("error") or "error",
                                err.get("message") or err.get("error_description") or "", err) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError, http.client.HTTPException) as e:
            raise Ambiguous(f"{method} {path}: {e}") from None

    def _auth(self):
        if self._token and time.time() < self._exp - 60:
            return self._token
        if not (self.client_id and self.client_secret):
            raise ProviderError(0, "no_credentials", "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set")
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = self._http("POST", "/v1/oauth2/token", b"grant_type=client_credentials",
                       {"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        if not r.get("access_token"):
            raise ProviderError(0, "no_token", "PayPal returned no access_token")
        self._token, self._exp = r["access_token"], time.time() + int(r.get("expires_in") or 600)
        return self._token

    # ---- payees (local; PayPal has no beneficiary API for payouts) ---------
    def create_beneficiary(self, payload):
        b = payload.get("beneficiary") or {}
        pp = b.get("paypal") or {}
        if pp.get("recipient_type", "EMAIL") not in ("EMAIL", "PAYPAL_ID", "PHONE") or not str(pp.get("receiver") or "").strip():
            raise ProviderError(400, "invalid_beneficiary", "a PayPal payee needs paypal.receiver (email or PayPal ID)")
        pid = "pp-" + str(uuid.uuid4())
        rec = {"id": pid, "beneficiary": b, "nickname": payload.get("nickname"), "transfer_methods": ["PAYPAL"]}
        self.book.put_payee(pid, rec)
        return rec

    def get_beneficiary(self, beneficiary_id):
        r = self.book.payee(beneficiary_id)
        if not r:
            raise ProviderError(404, "not_found", "PayPal payee not found")
        return r

    def update_beneficiary(self, beneficiary_id, payload):
        r = self.get_beneficiary(beneficiary_id)
        r.update({k: v for k, v in payload.items() if v is not None})
        self.book.put_payee(beneficiary_id, r)
        return r

    # ---- money ----------------------------------------------------------
    def fx_quote(self, *a, **k):
        raise ProviderError(400, "fx_not_supported", "the PayPal rail pays in the home currency only")

    def create_transfer(self, request_id, beneficiary_id, amount_minor, currency, reference, method=None,
                        source_currency=None, quote_id=None):
        if quote_id or (source_currency and source_currency != currency):
            raise ProviderError(400, "fx_not_supported", "the PayPal rail pays in the home currency only")
        payee = self.get_beneficiary(beneficiary_id)
        pp = (payee.get("beneficiary") or {}).get("paypal") or {}
        receiver, rtype = str(pp.get("receiver") or "").strip(), pp.get("recipient_type", "EMAIL")
        if not receiver:
            raise ProviderError(400, "invalid_beneficiary", "PayPal payee has no receiver")
        body = {
            "sender_batch_header": {"sender_batch_id": request_id, "email_subject": "You have a payment",
                                    "email_message": str(reference)[:255]},
            "items": [{"recipient_type": rtype, "receiver": receiver, "sender_item_id": request_id,
                       "amount": {"value": minor_to_str(amount_minor), "currency": currency},
                       "note": str(reference)[:4000]}],
        }
        # Ledger first: if the reply is lost we still know this request_id was (maybe) sent, to whom.
        self.book.record(request_id, beneficiary_id=beneficiary_id, receiver=receiver, amount=amount_minor, currency=currency)
        try:
            r = self._raw("POST", "/v1/payments/payouts", body, {"PayPal-Request-Id": request_id})
        except ProviderError as e:
            if self._is_duplicate(e):
                # PayPal already has this sender_batch_id: adopt the original, never a second payout.
                bid = self._batch_link(e.details)
                if bid:
                    self.book.record(request_id, batch_id=bid)
                    return self.get_transfer(bid)
                raise Ambiguous("PayPal says this sender_batch_id was already used but gave no link to it - held") from None
            raise
        bid = ((r or {}).get("batch_header") or {}).get("payout_batch_id")
        if not bid:
            raise Ambiguous("PayPal accepted the payout but returned no payout_batch_id")
        self.book.record(request_id, batch_id=bid)
        try:
            return self.get_transfer(bid)
        except (Ambiguous, ProviderError):
            hdr = r["batch_header"]
            st, ft = BATCH_ONLY.get(hdr.get("batch_status"), ("PROCESSING", None))
            return self._shape(bid, request_id, st, ft, None, minor_to_str(amount_minor), currency, beneficiary_id, None, hdr.get("batch_status"))

    @staticmethod
    def _is_duplicate(e):
        txt = f"{e.code} {e.message}".lower()
        return bool(PayPalPayouts._batch_link(e.details)) or "duplicate" in txt or ("sender_batch_id" in txt and ("already" in txt or "used" in txt))

    @staticmethod
    def _batch_link(details):
        for link in (details or {}).get("links") or []:
            m = _BATCH_LINK.search(str((link or {}).get("href", "")).split("?")[0])
            if m:
                return m.group(1)
        return None

    def get_transfer(self, transfer_id):
        """Our transfer id is PayPal's payout_batch_id (one item per batch)."""
        b = self._raw("GET", f"/v1/payments/payouts/{transfer_id}")
        hdr = (b or {}).get("batch_header") or {}
        if hdr.get("payout_batch_id") != transfer_id:
            raise Ambiguous("PayPal returned a different payout batch than the one asked for")
        sbid = (hdr.get("sender_batch_header") or {}).get("sender_batch_id")
        items = b.get("items") or []
        if not items:
            if hdr.get("batch_status") not in BATCH_ONLY:
                raise Ambiguous(f"PayPal batch {hdr.get('batch_status')} with no items yet")
            st, ft = BATCH_ONLY[hdr["batch_status"]]
            amt = (hdr.get("amount") or {})
            sent = self.book.sent(sbid) or {}
            return self._shape(transfer_id, sbid, st, ft, None, amt.get("value"), amt.get("currency"),
                               sent.get("beneficiary_id"), None, hdr["batch_status"])
        mine = [i for i in items if (i.get("payout_item") or {}).get("sender_item_id") == sbid] or items[:1]
        it = mine[0]
        pi = it.get("payout_item") or {}
        ts = it.get("transaction_status")
        err = it.get("errors") if isinstance(it.get("errors"), dict) else {}
        if ts in STATUS:
            st, ft = STATUS[ts], None
        elif ts == "FAILED":
            st, ft = "CANCELLED", FAILED_ERRORS.get(err.get("name"), "UNCLASSIFIED")
        elif ts in CLOSED:
            st, ft = "CANCELLED", CLOSED[ts]
        else:
            raise Ambiguous(f"PayPal item status {ts!r} is not one the desk knows")
        # The amount we report is the WHOLE batch: an extra item (money to someone else) shows as a mismatch.
        try:
            total = sum(str_to_minor((i.get("payout_item") or {}).get("amount", {}).get("value")) for i in items)
            total = minor_to_str(total)
        except Exception:
            total = "unreadable"
        sent = self.book.sent(pi.get("sender_item_id") or sbid) or {}
        ben = sent.get("beneficiary_id")
        if ben and sent.get("receiver") and str(pi.get("receiver", "")).strip().lower() != sent["receiver"].lower():
            ben = "paypal-receiver:" + str(pi.get("receiver"))  # paid somewhere other than recorded -> PROVIDER_MISMATCH
        fee = (it.get("payout_item_fee") or {}).get("value")
        t = self._shape(transfer_id, pi.get("sender_item_id") or sbid, st, ft, err, total, (pi.get("amount") or {}).get("currency"), ben, fee, ts)
        t["paypal"].update(payout_item_id=it.get("payout_item_id"), transaction_id=it.get("transaction_id"), batch_status=hdr.get("batch_status"))
        return t

    @staticmethod
    def _shape(bid, request_id, status, ftype, err, amount, currency, beneficiary_id, fee, pp_status):
        t = {"id": bid, "request_id": request_id, "status": status, "transfer_amount": amount, "transfer_currency": currency,
             "paypal": {"status": pp_status}}
        if beneficiary_id:
            t["beneficiary_id"] = beneficiary_id
        if fee is not None:
            t["fee_amount"], t["fee_paid_by"] = fee, "PAYER"
        if status == "CANCELLED":
            name = (err or {}).get("name") or pp_status
            t["failure"] = {"code": str(name), "desk_type": ftype, "details": {"type": str(name)},
                            "message": f"PayPal {pp_status}: {(err or {}).get('message') or name}"[:300]}
        return t

    def find_by_request_id(self, request_id):
        sent = self.book.sent(request_id)
        if not sent:
            return None  # this adapter never started a POST for it (the ledger is written before the POST)
        if not sent.get("batch_id"):
            raise Ambiguous("PayPal has no lookup by sender_batch_id and the reply to our POST was lost - "
                            "an operator re-send with the same sender_batch_id is the safe way to find it")
        t = self.get_transfer(sent["batch_id"])
        return t if t.get("request_id") == request_id else None

    def list_transfers_since(self, since_unix, pages=5):
        """PayPal Payouts has no 'list my payouts' read, so the orphan scan cannot see payouts made outside
        this desk on the PayPal rail. Returns only what this desk sent (documented gap)."""
        return []

    def usd_balance_minor(self):
        return None  # optional on this rail; the service skips the balance pre-check when it is None

    def simulate(self, transfer_id, next_status, failure_type=None):
        if self.transport and hasattr(self.transport, "simulate"):
            self.transport.simulate(transfer_id, next_status, failure_type)
            return self.get_transfer(transfer_id)
        raise ProviderError(400, "not_supported", "PayPal's sandbox has no payout status simulator")
