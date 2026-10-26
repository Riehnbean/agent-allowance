"""Operator desk: a small local web app over the incident service.

    AWX_CLIENT_ID=... AWX_API_KEY=... OPERATOR_TOKEN=pick-a-secret python3 server.py
    open http://127.0.0.1:8710

Sandbox only. The operator token gates every action; the agent has no token and no route
that moves money - it can only be asked for a proposal.
"""
import hmac
import json
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from allowance.adapter import Airwallex, Ambiguous, ProviderError  # noqa: E402
from allowance.agent import ClaudeAgent, run  # noqa: E402
from allowance.service import ALL_FAILURE_TYPES, Refused, Service  # noqa: E402
from allowance.store import Store  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("OPERATOR_TOKEN", "")
DB = os.environ.get("ALLOWANCE_DB", os.path.join(HERE, "allowance.db"))


class DropOnce:
    """Demo control: the next transfer is really created, but its reply is 'lost'."""
    def __init__(self, api):
        self.api, self.armed = api, False

    def __getattr__(self, k):
        return getattr(self.api, k)

    def create_transfer(self, *a, **k):
        t = self.api.create_transfer(*a, **k)
        if self.armed:
            self.armed = False
            raise Ambiguous("demo: connection dropped after Airwallex accepted the transfer")
        return t


API = Airwallex()
PROV = DropOnce(API)
SVC = Service(Store(DB), PROV)
SVC.s.init_policy("USD", per_tx_cap=int(os.environ.get("PER_TX_CAP", 100_000)), daily_cap=int(os.environ.get("DAILY_CAP", 300_000)))
AGENT = ClaudeAgent()


def cents(v):
    m = re.fullmatch(r"\s*(\d{1,7})(?:\.(\d{1,2}))?\s*", str(v))
    if not m:
        raise Refused("amount must look like 250 or 250.00")
    return int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))


def state():
    s = SVC.s
    incs = []
    for i in s.q("SELECT * FROM incidents ORDER BY id DESC LIMIT 20"):
        pays = [dict(p) for p in s.q("SELECT request_id,role,status,failure_type,failure_message,status_source,provider_id,amount,created_at,updated_at FROM payments WHERE incident_id=? ORDER BY created_at", i["id"])]
        aps = [dict(a) for a in s.q("SELECT id,role,amount,expires_at,approved_by,used_by_request FROM approvals WHERE incident_id=? ORDER BY id", i["id"])]
        last = s.one("SELECT body,at,provenance FROM events WHERE incident_id=? AND kind='decision' ORDER BY id DESC LIMIT 1", i["id"])
        ev = [dict(e, body=json.loads(e["body"])) for e in s.q("SELECT at,actor,kind,provenance,body FROM events WHERE incident_id=? ORDER BY id DESC LIMIT 30", i["id"])]
        incs.append({**dict(i), "payments": pays, "approvals": aps, "allowed": SVC.allowed_actions(i["id"]),
                     "decision": json.loads(last["body"]) | {"at": last["at"], "provenance": last["provenance"]} if last else None, "events": ev})
    pol = dict(s.policy())
    pol["committed_today"] = s.committed_today()
    return {"policy": pol, "beneficiaries": [dict(b) for b in s.q("SELECT * FROM beneficiaries")], "incidents": incs,
            "agent": "claude" if AGENT.key else "rules (no LLM key set)", "drop_armed": PROV.armed,
            "failure_types": ALL_FAILURE_TYPES, "env": "Airwallex SANDBOX - fake money only"}


def ensure_beneficiary():
    b = SVC.s.one("SELECT * FROM beneficiaries LIMIT 1")
    if b:
        return b["id"]
    r = API.create_beneficiary({
        "request_id": str(uuid.uuid4()), "nickname": "Maple Lumber Supply (demo)", "transfer_methods": ["LOCAL"],
        "beneficiary": {"entity_type": "COMPANY", "company_name": "Maple Lumber Supply LLC", "type": "BANK_ACCOUNT",
                        "address": {"country_code": "US", "city": "St. Louis", "street_address": "100 Market St", "postcode": "63101", "state": "MO"},
                        "bank_details": {"account_currency": "USD", "account_name": "Maple Lumber Supply LLC", "account_number": "123456789",
                                         "account_routing_type1": "aba", "account_routing_value1": "026009593", "bank_country_code": "US",
                                         "local_clearing_system": "ACH", "bank_account_category": "Checking"}}})
    SVC.allow_beneficiary(r["id"], "Maple Lumber Supply LLC", "acct ****6789 · ABA 026009593", "operator")
    return r["id"]


def unused_approval(iid, role):
    a = SVC.s.one("SELECT id FROM approvals WHERE incident_id=? AND role=? AND used_by_request IS NULL ORDER BY id DESC LIMIT 1", iid, role)
    if not a:
        raise Refused(f"no unused operator approval for the {role} payment - click Approve first")
    return a["id"]


def handle(method, path, body):
    p = path.strip("/").split("/")
    if method == "GET" and path == "/api/state":
        return state()
    if method == "GET" and len(p) == 4 and p[1] == "incidents" and p[3] == "receipt":
        return SVC.receipt(int(p[2]))
    if method != "POST":
        raise Refused("not found")
    if path == "/api/setup":
        return {"beneficiary": ensure_beneficiary()}
    if path == "/api/kill":
        SVC.kill("operator", body.get("reason") or "operator pressed STOP")
        return {}
    if path == "/api/unkill":
        SVC.unkill("operator")
        return {}
    if path == "/api/drop-next":
        PROV.armed = True
        return {}
    if path == "/api/incidents":
        ben = ensure_beneficiary()
        iid = SVC.open_incident(body.get("supplier") or "Maple Lumber", body.get("invoice") or f"INV-{int(time.time()) % 10000}",
                                ben, cents(body.get("amount", "250")), "USD", deadline=time.time() + float(body.get("deadline_hours", 24)) * 3600)
        return {"id": iid}
    if len(p) >= 4 and p[1] == "incidents":
        iid, act = int(p[2]), p[3]
        inc = SVC.s.one("SELECT * FROM incidents WHERE id=?", iid)
        if act == "approve":
            return {"approval": SVC.approve(iid, body["role"], "operator", ttl=int(body.get("ttl", 900)))}
        if act == "pay":
            role = body["role"]
            amt = cents(body["amount"]) if body.get("amount") else inc["amount"]
            ben = body.get("beneficiary_id") or inc["beneficiary_id"]
            return SVC.pay(unused_approval(iid, role), role, ben, amt, "USD")
        if act == "agent":
            return run(SVC, iid, AGENT)
        if act == "supplier":
            SVC.supplier_message(iid, body.get("text", ""))
            return {}
        if act == "close":
            SVC.close(iid, "operator")
            return {}
    if len(p) >= 4 and p[1] == "payments":
        rid, act = p[2], p[3]
        pay = SVC.payment(rid)
        if act == "reconcile":
            return SVC.reconcile(rid)
        if act == "simulate":
            if not pay["provider_id"]:
                raise Refused("reconcile first - we do not know the Airwallex id yet")
            API.simulate(pay["provider_id"], body["status"], body.get("failure_type") or None)
            SVC.s.log("system", "sandbox_simulator", "operator", pay["incident_id"], request_id=rid, status=body["status"], failure_type=body.get("failure_type"))
            return SVC.reconcile(rid)
    raise Refused("unknown action")


class H(BaseHTTPRequestHandler):
    server_version = "AgentAllowance/0.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method):
        path = urlparse(self.path).path
        if method == "GET" and path in ("/", "/index.html", "/app.js", "/app.css"):
            f = {"/": "index.html", "/index.html": "index.html"}.get(path, path[1:])
            ctype = {"js": "application/javascript", "css": "text/css"}.get(f.rsplit(".", 1)[-1], "text/html; charset=utf-8")
            return self._send(200, open(os.path.join(HERE, "web", f), "rb").read(), ctype)
        if method == "POST" and path == "/webhook/airwallex":
            # Unauthenticated by design: a webhook only makes us re-read Airwallex; it never sets a status.
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
            try:
                SVC.on_webhook(json.loads(self.rfile.read(n) or b"{}"))
            except Exception:
                pass
            return self._send(200, {"ok": True})
        if not path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not TOKEN or not hmac.compare_digest(self.headers.get("X-Operator-Token", ""), TOKEN):
            return self._send(401, {"error": "operator token required"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            self._send(200, handle(method, path, body))
        except Refused as e:
            self._send(409, {"refused": str(e)})
        except ProviderError as e:
            self._send(502, {"provider": e.code, "message": e.message})
        except Ambiguous as e:
            self._send(504, {"ambiguous": str(e)})
        except (KeyError, ValueError, TypeError) as e:
            self._send(400, {"error": f"bad request: {e}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("Set OPERATOR_TOKEN to a secret of your choice first.")
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", 8710))
    print(f"Operator desk on http://{host}:{port}  (Airwallex SANDBOX, agent={'claude' if AGENT.key else 'rules'})")
    ThreadingHTTPServer((host, port), H).serve_forever()
