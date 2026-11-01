"""Scenario D (Airwallex SANDBOX, fake money): pay a German supplier EUR 250.00 from a USD balance.

The desk locks an Airwallex FX quote for exactly EUR 250.00, checks every USD cap on the locked dollar cost and the
quote's markup over mid-market, sends the transfer with that quote_id, then reads back what Airwallex booked.
Needs AWX_CLIENT_ID and AWX_API_KEY (sandbox)."""
import json
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from allowance.adapter import Airwallex  # noqa: E402
from allowance.service import Service  # noqa: E402
from allowance.store import Store  # noqa: E402


def eur_beneficiary(api):
    b = api.create_beneficiary({
        "request_id": str(uuid.uuid4()), "nickname": "Holz Werk GmbH (demo)", "transfer_methods": ["LOCAL"],
        "beneficiary": {"entity_type": "COMPANY", "company_name": "Holz Werk GmbH", "type": "BANK_ACCOUNT",
                        "address": {"country_code": "DE", "city": "Berlin", "street_address": "Hauptstrasse 1", "postcode": "10115"},
                        "bank_details": {"account_currency": "EUR", "account_name": "Holz Werk GmbH", "iban": "DE89370400440532013000",
                                         "bank_country_code": "DE"}}})
    return b["id"]


def main():
    api = Airwallex()
    svc = Service(Store(os.path.join(tempfile.mkdtemp(), "fx.db")), api)
    svc.s.init_policy("USD", per_tx_cap=100_000, daily_cap=300_000, fx_currencies="EUR", allowed_countries="US,DE")
    ben = eur_beneficiary(api)
    svc.allow_beneficiary(ben, "Holz Werk GmbH", "IBAN ****3000", "zach")
    iid = svc.open_incident("Holz Werk GmbH", "EU-" + uuid.uuid4().hex[:6].upper(), ben, 25_000, "EUR")
    ap = svc.approve(iid, "original", "zach")
    p = svc.pay(ap, "original", ben, 25_000, "EUR")
    t = api.get_transfer(p["provider_id"]) if p["provider_id"] else None
    out = {
        "invoice": "EUR 250.00 to a German supplier (SEPA), funded from USD",
        "locked_quote": {"quote_id": p["fx_quote_id"], "client_rate": p["fx_rate"], "usd_cost_locked": p["cost_amount"] / 100},
        "desk_status": p["status"], "status_source": p["status_source"],
        "airwallex_transfer": {k: t.get(k) for k in ("id", "status", "source_currency", "transfer_currency", "transfer_amount",
                                                   "amount_payer_pays", "amount_beneficiary_receives", "conversion", "fee_amount")} if t else None,
        "usd_committed_against_daily_cap": svc.s.committed_today() / 100,
        "fx_log": [json.loads(e["body"]) for e in svc.s.q("SELECT body FROM events WHERE kind='fx_quote_locked'")],
    }
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
