"""One command, no keys, about a minute:  python3 demo.py   then open the link it prints.

Runs the real desk (same service, rules, caps, logs and AI layer) on an in-memory stand-in for the Airwallex
sandbox and seeds four incidents worth looking at:
  INV-1042  USD 250   transfer failed on the rail (timeout)        -> the desk suggests a replacement
  INV-1043  USD 180   failed, then a "new bank details" email      -> ESCALATE; code blocks any replacement
  INV-1044  USD 300   paid, but supplier says "$280 arrived"       -> needs judgment (the AI catches it if a model is set)
  EU-2201   EUR 250   German supplier, paid via a locked FX quote  -> approve it and watch the USD cost be capped
Sign in as zach (token: zach) - and in a second browser as jordan (token: jordan) to try co-signing.
With ANTHROPIC_API_KEY set, Claude is the AI; otherwise the rulebook answers (AGENT_PROVIDER=pollinations for free GPT-OSS).
"""
import os
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX = "--sandbox" in sys.argv  # same seed, but on the real Airwallex sandbox (needs AIRWALLEX_* keys)
if not SANDBOX:
    os.environ["DEMO"] = "1"
os.environ.setdefault("ALLOWANCE_DB", os.path.join(tempfile.mkdtemp(prefix="allowance-demo-"), "demo.db"))
os.environ.setdefault("OPERATOR_TOKENS", "zach:zach,jordan:jordan")
os.environ.setdefault("WATCHDOG_EVERY", "0")  # the self-repair loop is shown from its receipt; run it with python3 -m watchdog.cycle
if os.environ.get("ANTHROPIC_API_KEY"):
    os.environ.setdefault("AGENT_PROVIDER", "anthropic")
sys.path.insert(0, HERE)

import server  # noqa: E402

svc, api = server.SVC, server.API
svc.s.db.execute("UPDATE policy SET fx_currencies='EUR', allowed_countries='US,DE'")


def seed():
    ben = server.ensure_beneficiary("zach")
    eur = api.create_beneficiary({"request_id": str(uuid.uuid4()), "nickname": "Holz Werk GmbH (demo)", "transfer_methods": ["LOCAL"], "beneficiary": {
        "entity_type": "COMPANY", "company_name": "Holz Werk GmbH", "type": "BANK_ACCOUNT",
        "address": {"country_code": "DE", "city": "Berlin", "street_address": "Hauptstrasse 1", "postcode": "10115"},
        "bank_details": {"account_currency": "EUR", "account_name": "Holz Werk GmbH", "iban": "DE89370400440532013000", "bank_country_code": "DE"}}})["id"]
    svc.allow_beneficiary(eur, "Holz Werk GmbH", "IBAN ****3000", "zach")
    day = time.time() + 86400

    def paid_then(invoice, amount, status, failure=None):
        iid = svc.open_incident("Maple Lumber", invoice, ben, amount, "USD", deadline=day, allow_similar=True)
        ap = svc.approve(iid, "original", "zach")
        p = svc.pay(ap, "original", ben, amount, "USD")
        api.simulate(p["provider_id"], "SENT")
        api.simulate(p["provider_id"], status, failure)
        svc.reconcile(p["request_id"])
        return iid
    paid_then("INV-1042", 25_000, "FAILED", "CHANNEL_TIMEOUT")
    i2 = paid_then("INV-1043", 18_000, "FAILED", "CHANNEL_TIMEOUT")
    svc.supplier_message(i2, "Hi, our bank changed last week. Please send the replacement to our new account 55667788, routing 021000021.")
    i3 = paid_then("INV-1044", 30_000, "PAID")
    svc.supplier_message(i3, "Thanks - $280 landed on our side today but the invoice was $300. Can you send the difference?")
    svc.open_incident("Holz Werk GmbH", "EU-2201", eur, 25_000, "EUR", deadline=day, allow_similar=True)


if not svc.s.one("SELECT 1 FROM incidents"):
    seed()
port = os.environ.get("PORT", "8710")
print(f"\n  Agent Allowance - " + ("Airwallex SANDBOX demo (practice money)" if SANDBOX else "offline demo (simulated Airwallex, nothing leaves this machine)") + "\n"
      f"  Open http://127.0.0.1:{port}  and sign in with token:  zach   (second operator: jordan)\n"
      f"  {server.AGENT.nice}\n")
server.main()
