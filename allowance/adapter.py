"""Trusted Airwallex sandbox adapter.

This is the only module that holds provider credentials or talks to Airwallex.
The agent never calls it directly; the incident service does, after policy checks.

Sandbox only: the base URL is pinned to api.sandbox.airwallex.com and the
constructor refuses anything else, so this code cannot move real money.
"""
import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request

SANDBOX_BASE = "https://api.sandbox.airwallex.com/api/v1"


class ProviderError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.details = status, code, message, details or {}


class Ambiguous(Exception):
    """The request may or may not have reached Airwallex (timeout, reset, 5xx).

    Callers must treat the money as possibly sent and reconcile by request_id.
    It is never proof of non-payment."""


class Airwallex:
    def __init__(self, client_id=None, api_key=None, base=SANDBOX_BASE, timeout=30, transport=None):
        if not base.startswith("https://api.sandbox.airwallex.com/") and transport is None:
            raise ValueError("refusing non-sandbox Airwallex base URL")
        self.client_id = client_id or os.environ.get("AWX_CLIENT_ID")
        self.api_key = api_key or os.environ.get("AWX_API_KEY")
        self.base, self.timeout = base, timeout
        self._token, self._exp = None, 0
        self.transport = transport  # tests inject a fake

    # ---- low level -------------------------------------------------------
    def _raw(self, method, path, body=None, auth=True):
        if self.transport:
            return self.transport(method, path, body)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self._auth()
        else:
            headers["x-client-id"], headers["x-api-key"] = self.client_id, self.api_key
        data = None if body is None else json.dumps(body).encode()
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
            raise ProviderError(e.code, err.get("code", "error"), err.get("message", ""), err.get("details")) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError, http.client.HTTPException) as e:
            raise Ambiguous(f"{method} {path}: {e}") from None

    def _auth(self):
        if self._token and time.time() < self._exp - 60:
            return self._token
        if not (self.client_id and self.api_key):
            raise ProviderError(0, "no_credentials", "AWX_CLIENT_ID / AWX_API_KEY not set")
        r = self._raw("POST", "/authentication/login", {}, auth=False)
        self._token, self._exp = r["token"], time.time() + 25 * 60
        return self._token

    # ---- API surface used by Kit 03 --------------------------------------
    def create_beneficiary(self, payload):
        return self._raw("POST", "/beneficiaries/create", payload)

    def fx_quote(self, sell_currency, buy_currency, buy_amount_minor, validity="MIN_15"):
        """Lock an exchange rate: Airwallex holds this price until valid_to_at (POST /fx/quotes/create)."""
        return self._raw("POST", "/fx/quotes/create", {"sell_currency": sell_currency, "buy_currency": buy_currency,
                                                       "buy_amount": minor_to_str(buy_amount_minor), "validity": validity})

    def create_transfer(self, request_id, beneficiary_id, amount_minor, currency, reference, method="LOCAL",
                        source_currency=None, quote_id=None):
        """Submit one transfer. amount is integer minor units (cents) -> exact decimal string.
        Foreign currency: funded from source_currency at the rate locked by quote_id."""
        body = {
            "request_id": request_id,
            "beneficiary_id": beneficiary_id,
            "transfer_amount": minor_to_str(amount_minor),
            "transfer_currency": currency,
            "source_currency": source_currency or currency,
            "transfer_method": method,
            "reference": reference[:140],
            "reason": "payment_to_supplier",
        }
        if quote_id:
            body["quote_id"] = quote_id
        try:
            return self._raw("POST", "/transfers/create", body)
        except ProviderError as e:
            if e.code == "duplicate_request_id":
                # Airwallex already has this request: adopt the existing transfer, never a second one.
                tid = (e.details or {}).get("id")
                if tid:
                    return self.get_transfer(tid)
                found = self.find_by_request_id(request_id)
                if found:
                    return found
                raise Ambiguous("duplicate_request_id without a transfer id - it exists somewhere; look it up later") from None
            raise

    def get_beneficiary(self, beneficiary_id):
        return self._raw("GET", "/beneficiaries/" + beneficiary_id)

    def verify_account(self, entity_type, bank_details):
        """Ask Airwallex whether the account exists and the name matches (Verification/Confirmation of Payee;
        SEPA, UK, VN). Returns {code: VERIFIED|INVALID|CANNOT_VERIFY|..., details: {account_name_match_result}}."""
        return self._raw("POST", "/beneficiaries/verify_account", {"transfer_method": "LOCAL", "entity_type": entity_type, "bank_details": bank_details})

    def update_beneficiary(self, beneficiary_id, payload):
        return self._raw("POST", f"/beneficiaries/{beneficiary_id}/update", payload)

    def get_transfer(self, transfer_id):
        return self._raw("GET", "/transfers/" + transfer_id)

    def find_by_request_id(self, request_id):
        r = self._raw("GET", f"/transfers?request_id={request_id}&page_size=10")
        items = [t for t in r.get("items", []) if t.get("request_id") == request_id]  # never trust the filter blindly
        return items[0] if items else None

    def list_transfers_since(self, since_unix, pages=5):
        """Every transfer created on this Airwallex account since a time - used to spot transfers
        that did NOT come through this desk (a leaked key, someone clicking in the dashboard)."""
        import datetime
        iso = datetime.datetime.fromtimestamp(since_unix, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%%2B0000")
        out = []
        for n in range(pages):
            r = self._raw("GET", f"/transfers?from_created_at={iso}&page_size=100&page_num={n}")
            items = r.get("items", [])
            out += items
            if len(items) < 100:
                break
        return out

    def simulate(self, transfer_id, next_status, failure_type=None):
        body = {"next_status": next_status}
        if failure_type:
            body["failure_type"] = failure_type
        return self._raw("POST", f"/simulation/transfers/{transfer_id}/transition", body)

    def usd_balance_minor(self):
        for b in self._raw("GET", "/balances/current"):
            if b.get("currency") == "USD":
                return str_to_minor(b.get("available_amount", 0))
        return 0


def minor_to_str(minor):
    if not isinstance(minor, int) or minor <= 0:
        raise ValueError("amount must be a positive integer number of cents")
    return f"{minor // 100}.{minor % 100:02d}"


def str_to_minor(v):
    from decimal import Decimal, ROUND_HALF_UP
    return int((Decimal(str(v)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def details_hash(beneficiary):
    """Stable fingerprint of WHERE money goes. Any change to account, routing, name, country or
    currency changes the hash - so a beneficiary edited after approval is detected before paying."""
    import hashlib
    b = beneficiary.get("beneficiary", beneficiary)
    bd = b.get("bank_details", {})
    parts = [bd.get(k, "") for k in ("account_number", "account_routing_type1", "account_routing_value1", "swift_code",
                                    "iban", "bank_country_code", "account_currency", "account_name", "local_clearing_system")]
    parts.append(b.get("company_name") or " ".join(filter(None, [b.get("first_name"), b.get("last_name")])))
    pp = b.get("paypal")
    if pp:  # PayPal rail: WHERE money goes is the receiver (email / PayPal ID). Only appended when present,
        # so existing bank-account fingerprints are unchanged.
        parts += ["PAYPAL", pp.get("recipient_type", ""), pp.get("receiver", "")]
    norm = "|".join(str(x).strip().upper() for x in parts)
    return hashlib.sha256(norm.encode()).hexdigest()


def account_key(beneficiary):
    """Account + routing only (no name) - used to spot one bank account reused under different names."""
    b = beneficiary.get("beneficiary", beneficiary)
    pp = b.get("paypal")
    if pp:  # one PayPal receiver reused under different supplier names is the same mule pattern
        return "PAYPAL|" + str(pp.get("receiver", "")).strip().upper()
    bd = b.get("bank_details", {})
    return "|".join(str(bd.get(k, "")).strip().upper() for k in ("account_number", "account_routing_value1", "iban", "swift_code"))
