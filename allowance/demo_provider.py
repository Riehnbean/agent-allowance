"""An in-memory stand-in for the Airwallex sandbox, used only by `python3 demo.py` so a judge can try the
desk in a minute with no keys. Same method names and reply shapes the real adapter returns; nothing leaves
the machine. The desk itself (service.py) runs unchanged on top of it."""
import datetime
import threading
import time
import uuid

from .adapter import Ambiguous, ProviderError, minor_to_str


class DemoAirwallex:
    def __init__(self):
        self.lock = threading.Lock()
        self.bens, self.by_id, self.by_req, self.quotes = {}, {}, {}, {}
        self.balance = 10_000_000
        self.rate, self.mid = 1.125853, 1.11599   # EURUSD as quoted by the real sandbox on Nov 1 2026

    # beneficiaries ------------------------------------------------------------------------------------------
    def create_beneficiary(self, payload):
        with self.lock:
            bid = str(uuid.uuid4())
            self.bens[bid] = {"id": bid, "beneficiary": payload["beneficiary"], "nickname": payload.get("nickname"),
                              "transfer_methods": payload.get("transfer_methods", ["LOCAL"])}
            return dict(self.bens[bid])

    def get_beneficiary(self, bid):
        if bid not in self.bens:
            raise ProviderError(404, "not_found", "beneficiary not found")
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.bens[bid].items()}

    def verify_account(self, entity_type, bank_details):
        return {"code": "VERIFIED", "details": {"account_name_match_result": "FULL_MATCH", "resolved_account_name": bank_details.get("account_name")},
                "message": "demo stand-in"}

    def update_beneficiary(self, bid, payload):
        with self.lock:
            self.bens[bid].update({k: v for k, v in payload.items() if v is not None})
            return dict(self.bens[bid])

    # FX -------------------------------------------------------------------------------------------------------
    def fx_quote(self, sell_currency, buy_currency, buy_amount_minor, validity="MIN_15"):
        qid = str(uuid.uuid4())
        sell = round(buy_amount_minor * self.rate)
        exp = datetime.datetime.fromtimestamp(time.time() + 900, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
        q = {"quote_id": qid, "sell_currency": sell_currency, "buy_currency": buy_currency, "buy_amount": buy_amount_minor / 100,
             "sell_amount": sell / 100, "client_rate": self.rate, "mid_rate": self.mid, "valid_to_at": exp, "validity": validity}
        self.quotes[qid] = q
        return dict(q)

    # transfers ------------------------------------------------------------------------------------------------
    def create_transfer(self, request_id, beneficiary_id, amount_minor, currency, reference, method="LOCAL",
                        source_currency=None, quote_id=None):
        with self.lock:
            if request_id in self.by_req:
                return dict(self.by_req[request_id])
            if beneficiary_id not in self.bens:
                raise ProviderError(400, "invalid_beneficiary", "unknown beneficiary")
            payer = amount_minor
            if quote_id:
                q = self.quotes.get(quote_id)
                if not q or q["buy_currency"] != currency:
                    raise ProviderError(400, "invalid_quote", "quote does not match this transfer")
                payer = round(q["sell_amount"] * 100)
            t = {"id": str(uuid.uuid4()), "request_id": request_id, "beneficiary_id": beneficiary_id, "status": "SCHEDULED",
                 "transfer_amount": minor_to_str(amount_minor), "transfer_currency": currency,
                 "source_currency": source_currency or currency, "amount_payer_pays": payer / 100, "fee_amount": 0,
                 "fee_paid_by": "PAYER", "reference": reference, "created_at": time.time()}
            if quote_id:
                t["conversion"] = {"currency_pair": f"{currency}{source_currency}", "rate": self.quotes[quote_id]["client_rate"]}
            self.by_req[request_id] = t
            self.by_id[t["id"]] = t
            return dict(t)

    def get_transfer(self, tid):
        if tid not in self.by_id:
            raise Ambiguous("transfer not found yet")
        return dict(self.by_id[tid])

    def find_by_request_id(self, rid):
        t = self.by_req.get(rid)
        return dict(t) if t else None

    def list_transfers_since(self, since_unix, pages=5):
        return [dict(t) for t in self.by_id.values() if t["created_at"] >= since_unix]

    def simulate(self, tid, next_status, failure_type=None):
        t = self.by_id[tid]
        if next_status == "FAILED":
            t["status"] = "CANCELLED"
            t["failure"] = {"code": "9", "details": {"type": "INCORRECT_ROUTING"},
                            "message": (failure_type or "OTHER").replace("_", " ").capitalize()}
        else:
            t["status"] = next_status
        return dict(t)

    def usd_balance_minor(self):
        return self.balance
