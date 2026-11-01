"""Operator desk: a small local web app over the incident service.

    AWX_CLIENT_ID=... AWX_API_KEY=... OPERATOR_TOKEN=pick-a-secret python3 server.py
    open http://127.0.0.1:8710

Sandbox only. The operator token gates every action; the agent has no token and no route
that moves money - it can only be asked for a proposal.
"""
import hmac
import json
import threading
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from allowance.adapter import Airwallex, Ambiguous, ProviderError, details_hash  # noqa: E402
from allowance.agent import LLMAgent, run, think_ahead  # noqa: E402
from allowance.service import ALL_FAILURE_TYPES, Refused, Service  # noqa: E402
from allowance.store import Store  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("OPERATOR_TOKEN", "")
# Named operators for four-eyes approval: OPERATOR_TOKENS="zach:secret1,jordan:secret2"
OPERATORS = {t: n for n, t in (x.split(":", 1) for x in os.environ.get("OPERATOR_TOKENS", "").split(",") if ":" in x)}
if TOKEN and not OPERATORS:
    OPERATORS[TOKEN] = "operator"  # single-operator demo mode; ignored once named operators exist


def who(token):
    for t, n in OPERATORS.items():
        if t and hmac.compare_digest(token or "", t):
            return n
    return None
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
for _k, _env in (("per_tx_cap", "PER_TX_CAP"), ("daily_cap", "DAILY_CAP"), ("dual_approval_over", "COSIGN_OVER")):
    if os.environ.get(_env):  # env changes apply on restart (INSERT OR IGNORE alone would silently keep old caps)
        SVC.s.db.execute(f"UPDATE policy SET {_k}=? WHERE id=1", (int(os.environ[_env]),))
AGENT = LLMAgent()
WAKE = threading.Event()


def cents(v):
    m = re.fullmatch(r"\s*(\d{1,7})(?:\.(\d{1,2}))?\s*", str(v))
    if not m:
        raise Refused("amount must look like 250 or 250.00")
    return int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))


def state():
    s = SVC.s
    incs = []
    for i in s.q("SELECT * FROM incidents ORDER BY id DESC LIMIT 20"):
        pays = [dict(p) for p in s.q("SELECT request_id,role,status,failure_type,failure_message,status_source,provider_id,amount,fee,anomaly,created_at,updated_at FROM payments WHERE incident_id=? ORDER BY created_at", i["id"])]
        aps = [dict(a) for a in s.q("SELECT id,role,amount,expires_at,approved_by,cosigned_by,used_by_request FROM approvals WHERE incident_id=? ORDER BY id", i["id"])]
        # Same rule the service enforces: co-sign is needed over the threshold for ONE payment, or for this payee's 24h total.
        pol0 = s.policy()
        day_to_payee = s.one(
            "SELECT COALESCE(SUM(p.amount),0) s FROM payments p WHERE p.status<>'CANCELLED' AND p.created_at>? AND p.beneficiary_id IN "
            "(SELECT id FROM beneficiaries WHERE account_key=(SELECT account_key FROM beneficiaries WHERE id=?) UNION SELECT ?)",
            time.time() - 86400, i["beneficiary_id"], i["beneficiary_id"])["s"]
        for a in aps:
            a["needs_cosign"] = a["amount"] > pol0["dual_approval_over"] or day_to_payee + a["amount"] > pol0["dual_approval_over"]
        last = s.one("SELECT body,at,provenance FROM events WHERE incident_id=? AND kind='decision' ORDER BY id DESC LIMIT 1", i["id"])
        ev = [dict(e, body=json.loads(e["body"])) for e in s.q("SELECT at,actor,kind,provenance,body FROM events WHERE incident_id=? ORDER BY id DESC LIMIT 30", i["id"])]
        allowed = SVC.allowed_actions(i["id"])
        ai = AGENT.status(SVC.evidence(i["id"]), allowed) if i["state"] != "RESOLVED" else "off"
        incs.append({**dict(i), "payments": pays, "approvals": aps, "allowed": allowed, "ai": ai,
                     "decision": json.loads(last["body"]) | {"at": last["at"], "provenance": last["provenance"]} if last else None, "events": ev})
    pol = dict(s.policy())
    pol["committed_today"] = s.committed_today()
    ok, n, bad = s.verify_chain()
    pol["log"] = {"intact": ok, "entries": n, "first_bad": bad}
    return {"me": None, "operators": sorted(set(OPERATORS.values())),
            "pending": [dict(a) for a in s.q("SELECT * FROM pending_actions WHERE done_by IS NULL AND created_at>? ORDER BY id DESC", time.time() - 3600)],
            "policy": pol, "beneficiaries": [dict(b) for b in s.q("SELECT * FROM beneficiaries")], "incidents": incs,
            "agent": AGENT.nice, "drop_armed": PROV.armed,
            "failure_types": ALL_FAILURE_TYPES, "env": "Airwallex SANDBOX - fake money only"}


def ensure_beneficiary(op="operator"):
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
    SVC.allow_beneficiary(r["id"], "Maple Lumber Supply LLC", "acct ****6789 · ABA 026009593", op)
    return r["id"]


def reverify(requested_by, note, details=None):
    """details: the exact record both people checked (two-person path) - never re-read here, so an edit
    landing between the confirm-time check and this save cannot slip in."""
    if not note or len(note.strip()) < 15:
        raise Refused("write what you checked (at least a sentence)")
    ben = ensure_beneficiary()
    cur = details if details is not None else API.get_beneficiary(ben)
    bd = cur["beneficiary"]["bank_details"]
    SVC.allow_beneficiary(ben, "Maple Lumber Supply LLC", f"acct ****{bd['account_number'][-4:]} · ABA {bd.get('account_routing_value1')}",
                          requested_by, details=cur, override_reason=note)


def _short(x):
    import hashlib
    return hashlib.sha256(str(x).encode()).hexdigest()[:12]


def unused_approval(iid, role):
    a = SVC.s.one("SELECT id FROM approvals WHERE incident_id=? AND role=? AND used_by_request IS NULL ORDER BY id DESC LIMIT 1", iid, role)
    if not a:
        raise Refused(f"no unused operator approval for the {role} payment - click Approve first")
    return a["id"]


def handle(method, path, body, op="operator"):
    p = path.strip("/").split("/")
    if method == "GET" and path == "/api/state":
        return state()
    if method == "GET" and len(p) == 4 and p[1] == "incidents" and p[3] == "receipt":
        return SVC.receipt(int(p[2]))
    if method != "POST":
        raise Refused("not found")
    if path == "/api/setup":
        return {"beneficiary": ensure_beneficiary(op)}
    if path == "/api/kill":
        SVC.kill(op, body.get("reason") or f"{op} pressed STOP")
        return {}
    if path == "/api/unkill":
        pol = SVC.s.policy()
        if pol["killed_by"] == "system" and len(OPERATORS) > 1:
            # The desk stopped itself (payee edited, unknown transfers...). Two people resume it.
            # Bound to THIS stop: if the desk stops again for a different reason, the request no longer applies.
            return {"pending": SVC.request_action("unkill", "desk|" + _short(pol["killed_reason"]), body.get("note"), op)}
        SVC.unkill(op, single_operator_mode=len(OPERATORS) <= 1)
        return {}
    if len(p) == 4 and p[1] == "pending" and p[3] == "confirm":
        pend = SVC.s.one("SELECT * FROM pending_actions WHERE id=?", int(p[2]))
        if pend and not pend["done_by"]:
            # Check the request still describes the world BEFORE consuming it.
            if pend["kind"] == "reverify":
                ben, _, want = pend["target"].partition("|")
                checked = API.get_beneficiary(ben)
                if not want or details_hash(checked) != want:
                    raise Refused("the payee's bank details at Airwallex changed after this request was made - "
                                  "the request no longer matches what was checked; ask again")
            if pend["kind"] == "unkill":
                pol = SVC.s.policy()
                if not pol["killed"] or "desk|" + _short(pol["killed_reason"]) != pend["target"]:
                    raise Refused("the desk stopped again for a different reason since this request - ask again")
        a = SVC.confirm_action(int(p[2]), op)
        if a["kind"] == "unkill":
            SVC.unkill(a["requested_by"], confirmed_by=op)
        elif a["kind"] == "reverify":
            reverify(a["requested_by"], f"{a['note']} (confirmed by {op})", details=checked)
        elif a["kind"] == "reopen":
            SVC.reopen(int(a["target"]), f"{a['requested_by']}+{op}", a["note"])
        return {"done": a["kind"]}
    if path == "/api/demo/edit-payee":
        # Demo of an attacker / insider editing the payee inside Airwallex after it was approved.
        ben = ensure_beneficiary()
        cur = API.get_beneficiary(ben)
        bd = cur["beneficiary"]["bank_details"]
        bd["account_number"] = "000111222333" if bd.get("account_number") != "000111222333" else "123456789"
        bd.pop("bank_name", None)
        API.update_beneficiary(ben, {"beneficiary": cur["beneficiary"], "nickname": cur.get("nickname"), "transfer_methods": cur.get("transfer_methods", ["LOCAL"])})
        SVC.s.log("system", "demo_payee_edited_at_airwallex", "operator", None, beneficiary_id=ben, new_account_last4=bd["account_number"][-4:])
        return {"account_last4": bd["account_number"][-4:]}
    if path == "/api/payee/reverify":
        # Accepting changed bank details is a two-person action with a written note (BEC defence).
        if len(OPERATORS) > 1:
            ben = ensure_beneficiary()
            cur = API.get_beneficiary(ben)
            last4 = ((cur.get("beneficiary") or cur).get("bank_details") or {}).get("account_number", "")[-4:]
            note = (body.get("note") or "").strip()
            # The request is bound to the exact bank details shown now; both people see the same masked account.
            return {"pending": SVC.request_action("reverify", f"{ben}|{details_hash(cur)}",
                                                  (note + f" [account ****{last4}]") if len(note) >= 15 else note, op)}
        reverify(op, body.get("note"))
        return {}
    if path == "/api/drop-next":
        PROV.armed = True
        return {}
    if path == "/api/incidents":
        ben = ensure_beneficiary()
        iid = SVC.open_incident(body.get("supplier") or "Maple Lumber", body.get("invoice") or f"INV-{int(time.time()) % 10000}",
                                ben, cents(body.get("amount", "250")), "USD", deadline=time.time() + float(body.get("deadline_hours", 24)) * 3600,
                                allow_similar=bool(body.get("separate_invoice")))
        return {"id": iid}
    if len(p) >= 4 and p[1] == "incidents":
        iid, act = int(p[2]), p[3]
        inc = SVC.s.one("SELECT * FROM incidents WHERE id=?", iid)
        if act == "approve":
            return {"approval": SVC.approve(iid, body["role"], op, ttl=int(body.get("ttl", 900)))}
        if act == "cosign":
            # The co-signer signs from THEIR OWN session; nobody types anyone else's secret.
            SVC.cosign(unused_approval(iid, body["role"]), op)
            return {"cosigned_by": op}
        if act == "pay":
            role = body["role"]
            amt = cents(body["amount"]) if body.get("amount") else inc["amount"]
            ben = body.get("beneficiary_id") or inc["beneficiary_id"]
            return SVC.pay(unused_approval(iid, role), role, ben, amt, "USD")
        if act == "agent":
            return run(SVC, iid, LLMAgent())  # one agent object per request: no shared per-call flags between threads
        if act == "supplier":
            SVC.supplier_message(iid, body.get("text", ""))
            return {}
        if act == "reopen":
            serious = inc["alert"] in ("DOUBLE_PAID", "BENEFICIARY_CHANGED", "CANCELLED_THEN_PAID", "PAID_THEN_CANCELLED", "FRAUD_SIGNAL")
            last = SVC.s.one("SELECT failure_type FROM payments WHERE incident_id=? ORDER BY created_at DESC LIMIT 1", iid)
            from allowance.service import DETAILS_PROBLEM, DELIBERATE
            if (serious or (last and last["failure_type"] in DETAILS_PROBLEM | DELIBERATE)) and len(OPERATORS) > 1:
                return {"pending": SVC.request_action("reopen", iid, body.get("note"), op)}
            SVC.reopen(iid, op, body.get("note", ""))
            return {}
        if act == "release":
            return SVC.release(body["request_id"], op, body.get("note"))
        if act == "resend":
            return SVC.resend(body["request_id"], op)
        if act == "close":
            SVC.close(iid, op, note=body.get("note"))
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
            # A webhook only makes us re-read Airwallex; it never sets a status by itself. If a signing
            # secret is configured, unsigned / stale / forged deliveries are dropped before that.
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= 65536:
                return self._send(400, {"error": "bad length"})
            raw = self.rfile.read(n) or b"{}"
            secret = os.environ.get("AWX_WEBHOOK_SECRET")
            if secret:
                ts, sig = self.headers.get("x-timestamp", ""), self.headers.get("x-signature", "")
                good = hmac.new(secret.encode(), ts.encode() + raw, "sha256").hexdigest()
                try:
                    fresh = abs(time.time() * 1000 - float(ts)) < 5 * 60 * 1000 or abs(time.time() - float(ts)) < 300
                except ValueError:
                    fresh = False
                if not (fresh and hmac.compare_digest(good, sig)):
                    SVC.s.log("provider", "webhook_rejected", "untrusted", None, reason="bad signature or timestamp")
                    return self._send(401, {"error": "bad signature"})
            try:
                SVC.on_webhook(json.loads(raw))
            except Exception:
                pass
            return self._send(200, {"ok": True})
        if not path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        op = who(self.headers.get("X-Operator-Token", ""))
        if not op:
            return self._send(401, {"error": "operator token required"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            out = handle(method, path, body, op)
            if method == "POST":
                WAKE.set()  # something changed: let the AI start thinking now
            if path == "/api/state":
                out["me"] = op
            self._send(200, out)
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


def sweeper():
    import threading

    def loop():
        while True:
            time.sleep(60)
            try:
                SVC.sweep(stale_after=300)
                SVC.scan_orphans()
            except Exception as e:  # never let the safety net crash the desk
                print("sweep error:", e)
    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    SVC.s.validate_policy()
    if not SVC._meta("orphan_scan_since"):
        SVC._meta("orphan_scan_since", str(time.time()))  # transfers made before the desk existed aren't orphans
    sweeper()
    threading.Thread(target=think_ahead, args=(SVC, LLMAgent), kwargs={"wake": WAKE}, daemon=True).start()
    if not OPERATORS:
        sys.exit("Set OPERATOR_TOKEN (or OPERATOR_TOKENS=name:secret,name2:secret2) first.")
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", 8710))
    print(f"Operator desk on http://{host}:{port}  (Airwallex SANDBOX, agent={AGENT.label})")
    ThreadingHTTPServer((host, port), H).serve_forever()
