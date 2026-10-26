"""Incident service: the only path from a decision to money movement.

The agent may *propose*. Everything that can cost money is decided here, in code:
  - kill switch, beneficiary allowlist, approval binding (who/how much/currency/expiry),
  - per-transaction and daily caps that include reserved and uncertain payments,
  - one approval -> at most one payment (unique index),
  - duplicate lock: no new payment on an incident while another one might still pay,
  - a replacement needs the original confirmed CANCELLED by a fresh Airwallex lookup,
    a failure type we consider safe to retry, and a separate operator approval.
"""
import json
import re
import time
import uuid

from .adapter import Ambiguous, ProviderError, account_key, details_hash, str_to_minor
import sqlite3

from .store import IN_FLIGHT, in_flight

# Failure types where paying the SAME beneficiary again is reasonable.
RETRYABLE = {
    "CHANNEL_TIMEOUT", "SYSTEM_ERROR", "INSUFFICIENT_FUNDS", "TRANSACTION_AMOUNT_NOT_COVERING_FEE",
}
# Failure types where the bank details themselves are suspect -> a human must verify new
# details out-of-band (classic invoice-redirection fraud lives here).
DETAILS_PROBLEM = {
    "ACCOUNT_CLOSED", "ACCOUNT_INACTIVE_OR_DORMANT", "ACCOUNT_UNDER_RESTRICTION", "ACCOUNT_CURRENCY_MISMATCH",
    "BENEFICIARY_BANK_RETURNED", "BENEFICIARY_NAME_MISMATCH", "INVALID_ACCOUNT_NAME_OR_NUMBER",
    "INVALID_BANK_INFORMATION", "INVALID_BANK_OR_BRANCH_CODE", "INVALID_BENEFICIARY_DETAILS",
    "INVALID_CORRESPONDENT_BANK_INFORMATION", "INVALID_SWIFT_BIC_CODE", "INVALID_SPECIAL_CHARACTER",
}
# Someone deliberately stopped or reversed it -> never auto-replace.
DELIBERATE = {"RECALL_REQUESTED", "CLIENT_REQUESTED", "BENEFICIARY_REQUESTED", "DUPLICATION_RETURN", "TM_SUSPENDED"}

from .store import FINAL  # noqa: E402


class Refused(Exception):
    """A policy check failed. The reason is safe to show the operator and the agent."""


class Service:
    def __init__(self, store, provider, clock=time.time):
        self.s, self.p, self.now = store, provider, clock
        self._hint_at = {}

    # ---------------------------------------------------------------- setup
    def allow_beneficiary(self, provider_id, name, fingerprint, operator, details=None, override_reason=None):
        """Put a payee on the allowlist. We read the bank details from Airwallex ourselves and remember a
        hash of them, so a later edit to the payee (in the dashboard, by an insider, by a compromised key)
        is caught before any money moves."""
        if details is None:
            details = self.p.get_beneficiary(provider_id)
        h, ak = details_hash(details), account_key(details)
        with self.s.tx():
            twin = self.s.one("SELECT id,name FROM beneficiaries WHERE account_key=? AND id<>?", ak, provider_id)
            if twin and _norm_name(twin["name"]) != _norm_name(name) and not override_reason:
                self.s.log("policy", "beneficiary_refused", "decided", None, id=provider_id, name=name, reason="same bank account as " + twin["name"])
                raise Refused(f"this bank account is already on file under a different supplier name ({twin['name']}) - a common money-mule pattern; needs an override reason")
            self.s.db.execute(
                "INSERT OR REPLACE INTO beneficiaries(id,name,bank_fingerprint,approved_by,approved_at,details_hash,account_key) VALUES(?,?,?,?,?,?,?)",
                (provider_id, name, fingerprint, operator, self.now(), h, ak),
            )
            self.s.log("operator", "beneficiary_allowed", "operator", None, id=provider_id, name=name, fingerprint=fingerprint,
                       by=operator, details_hash=h[:12], override=override_reason)

    def open_incident(self, supplier, invoice, beneficiary_id, amount, currency, deadline=None):
        invoice = clean_ref(invoice)
        if not isinstance(amount, int) or amount <= 0:
            raise Refused("amount must be a positive whole number of cents")
        key = _norm_name(supplier) + "|" + _norm_name(invoice)
        with self.s.tx():
            if not self.s.one("SELECT 1 FROM beneficiaries WHERE id=?", beneficiary_id):
                raise Refused("beneficiary is not on the operator's allowlist")
            dup = self.s.one("SELECT id FROM incidents WHERE invoice_key=?", key)
            if dup:
                raise Refused(f"invoice {invoice} from {supplier} is already handled in incident #{dup['id']} - duplicate invoice")
            twin = self.s.one("SELECT id,beneficiary_id FROM incidents WHERE amount=? AND invoice_key LIKE ? AND beneficiary_id<>?",
                              amount, "%|" + _norm_name(invoice), beneficiary_id)
            if twin:
                raise Refused(f"the same invoice number and amount was already sent to a different payee (incident #{twin['id']}) - possible redirected duplicate")
            cur = self.s.db.execute(
                "INSERT INTO incidents(supplier,invoice,beneficiary_id,amount,currency,supplier_deadline,invoice_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (supplier, invoice, beneficiary_id, amount, currency, deadline, key, self.now()),
            )
            iid = cur.lastrowid
            self.s.log("system", "incident_opened", "decided", iid, supplier=supplier, invoice=invoice, amount=amount, currency=currency)
        return iid

    def approve(self, incident_id, role, operator, ttl=3600, beneficiary_id=None, amount=None, currency=None):
        inc = self._incident(incident_id)
        b, a, c = beneficiary_id or inc["beneficiary_id"], amount or inc["amount"], currency or inc["currency"]
        if (b, a, c) != (inc["beneficiary_id"], inc["amount"], inc["currency"]):
            self.s.log("policy", "approval_refused", "decided", incident_id, reason="approval terms differ from the invoice")
            raise Refused("an approval must match the invoice exactly (payee, amount, currency)")
        if ttl > 24 * 3600:
            raise Refused("approvals expire within 24 hours")
        with self.s.tx():
            cur = self.s.db.execute(
                "INSERT INTO approvals(incident_id,role,beneficiary_id,amount,currency,expires_at,approved_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (incident_id, role, b, a, c, self.now() + ttl, operator, self.now()),
            )
            self.s.log("operator", "approved", "operator", incident_id, approval_id=cur.lastrowid, role=role, beneficiary_id=b, amount=a, currency=c, ttl=ttl, by=operator)
        return cur.lastrowid

    def reopen(self, incident_id, operator, note):
        """A person investigated an escalation (e.g. called the supplier on a known number and
        re-verified the payee). Reopening clears the alarm and is the ONLY way to allow a replacement
        after a non-retryable failure. The note is kept forever in the evidence log."""
        if not note or len(note.strip()) < 15:
            raise Refused("reopening needs a note of what you checked (at least a sentence)")
        inc = self._incident(incident_id)
        if inc["state"] == "RESOLVED":
            raise Refused("incident is already resolved")
        if any(in_flight(p["status"]) for p in self.s.q("SELECT status FROM payments WHERE incident_id=?", incident_id)):
            raise Refused("a payment on this incident may still go through - wait for a final status first")
        with self.s.tx():
            self.s.db.execute("UPDATE incidents SET state='OPEN', alert=NULL, reopened_at=? WHERE id=?", (self.now(), incident_id))
            self.s.log("operator", "incident_reopened", "operator", incident_id, by=operator, note=note.strip()[:1000], cleared_alert=inc["alert"])

    def cosign(self, approval_id, operator):
        """Second pair of eyes for large payments: a DIFFERENT operator must co-sign."""
        with self.s.tx():
            ap = self.s.one("SELECT * FROM approvals WHERE id=?", approval_id)
            if not ap:
                raise Refused("no such approval")
            if ap["approved_by"] == operator:
                raise Refused("the co-signer must be a different person than the first approver")
            if ap["used_by_request"]:
                raise Refused("approval already used")
            self.s.db.execute("UPDATE approvals SET cosigned_by=? WHERE id=?", (operator, approval_id))
            self.s.log("operator", "cosigned", "operator", ap["incident_id"], approval_id=approval_id, by=operator)

    def kill(self, operator, reason="operator stop"):
        with self.s.tx():
            self.s.db.execute("UPDATE policy SET killed=1, killed_reason=? WHERE id=1", (reason,))
            self.s.log("operator", "kill_switch_on", "operator", None, by=operator, reason=reason)

    def unkill(self, operator):
        with self.s.tx():
            self.s.db.execute("UPDATE policy SET killed=0, killed_reason=NULL WHERE id=1")
            self.s.log("operator", "kill_switch_off", "operator", None, by=operator)

    # ---------------------------------------------------------------- pay
    def pay(self, approval_id, role, beneficiary_id, amount, currency):
        """Reserve + submit one payment under an approval. Terms must match the approval exactly."""
        request_id = str(uuid.uuid4())
        try:
            try:
                return self._pay(request_id, approval_id, role, beneficiary_id, amount, currency)
            except sqlite3.IntegrityError as e:
                raise Refused(f"database duplicate lock: {e}") from None
        except Refused as e:
            ap = self.s.one("SELECT incident_id FROM approvals WHERE id=?", approval_id)
            # logged after the rollback, so the refusal itself is always on the record
            self.s.log("policy", "payment_refused", "decided", ap["incident_id"] if ap else None,
                       role=role, reason=str(e), amount=amount, beneficiary_id=beneficiary_id)
            raise

    def _pay(self, request_id, approval_id, role, beneficiary_id, amount, currency):
        with self.s.tx():
            ap = self.s.one("SELECT * FROM approvals WHERE id=?", approval_id)
            if not ap:
                raise Refused("no such approval")
            iid = ap["incident_id"]
            inc = self._incident(iid)
            self._check(ap, inc, role, beneficiary_id, amount, currency)
            now = self.now()
            self.s.db.execute(
                "INSERT INTO payments(request_id,incident_id,role,approval_id,beneficiary_id,amount,currency,status,status_source,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?, 'RESERVED','none',?,?)",
                (request_id, iid, role, approval_id, beneficiary_id, amount, currency, now, now),
            )
            self.s.db.execute("UPDATE approvals SET used_by_request=? WHERE id=?", (request_id, approval_id))
            self.s.log("policy", "payment_reserved", "decided", iid, request_id=request_id, role=role, amount=amount)
        # Provider call happens OUTSIDE the lock; the reservation already holds the budget.
        return self.submit(request_id)

    def submit(self, request_id):
        """Send (or re-send with the SAME request_id) a reserved payment."""
        pay = self.s.one("SELECT * FROM payments WHERE request_id=?", request_id)
        if self.s.policy()["killed"] and pay["status"] == "UNKNOWN":
            # We don't know if the first attempt reached Airwallex. While STOP is on we only look, never re-send.
            self.s.log("policy", "resubmit_blocked_by_kill", "decided", pay["incident_id"], request_id=request_id)
            raise Refused("STOP is on: will only look this payment up, not re-send it")
        if self.s.policy()["killed"] and pay["status"] == "RESERVED":
            self._set(request_id, "CANCELLED", "none", failure_type="KILLED_BEFORE_SUBMIT", failure_message="kill switch was on")
            self.s.log("policy", "payment_blocked_by_kill", "decided", pay["incident_id"], request_id=request_id)
            raise Refused("kill switch is on; reservation released without submitting")
        inc = self._incident(pay["incident_id"])
        if pay["status"] == "RESERVED":
            self._presubmit_checks(pay)
        try:
            t = self.p.create_transfer(request_id, pay["beneficiary_id"], pay["amount"], pay["currency"], f"{inc['invoice']} {pay['role']}")
        except Ambiguous as e:
            self._set(request_id, "UNKNOWN", "none")
            self.s.log("provider", "submit_ambiguous", "observed", pay["incident_id"], request_id=request_id, error=str(e))
            return self.payment(request_id)
        except ProviderError as e:
            # A definite rejection before acceptance: nothing was created, budget is released.
            self._set(request_id, "CANCELLED", "provider_create", failure_type="REJECTED_" + e.code.upper(), failure_message=e.message)
            self.s.log("provider", "submit_rejected", "observed", pay["incident_id"], request_id=request_id, code=e.code, message=e.message)
            return self.payment(request_id)
        self._apply_provider(request_id, t, "provider_create")
        return self.payment(request_id)

    def _presubmit_checks(self, pay):
        """Network checks done before the first submit (never before a same-request_id retry)."""
        rid, iid = pay["request_id"], pay["incident_id"]
        ben = self.s.one("SELECT * FROM beneficiaries WHERE id=?", pay["beneficiary_id"])

        def stop(code, msg, alert=None):
            self._set(rid, "CANCELLED", "none", failure_type=code, failure_message=msg)
            self.s.log("policy", "presubmit_blocked", "decided", iid, request_id=rid, code=code, reason=msg)
            if alert:
                self._alert(iid, alert)
            raise Refused(msg + " - nothing was sent")
        try:
            current = self.p.get_beneficiary(pay["beneficiary_id"])
        except (Ambiguous, ProviderError) as e:
            stop("DRIFT_CHECK_UNAVAILABLE", f"could not re-read the payee's bank details ({e})")
        cur_ccy = ((current.get("beneficiary") or current).get("bank_details") or {}).get("account_currency")
        if cur_ccy and cur_ccy != pay["currency"]:
            stop("ACCOUNT_CURRENCY_MISMATCH_PRECHECK", f"the payee's account is in {cur_ccy}, not {pay['currency']}")
        if ben["details_hash"] and details_hash(current) != ben["details_hash"]:
            self.kill("system", "payee bank details changed after approval")
            stop("BENEFICIARY_CHANGED", "the payee's bank details at Airwallex changed after they were approved", alert="BENEFICIARY_CHANGED")
        try:
            bal = self.p.usd_balance_minor()
        except (Ambiguous, ProviderError):
            bal = None
        if bal is not None and bal < pay["amount"] + self.s.policy()["max_fee"]:
            stop("INSUFFICIENT_BALANCE_PRECHECK", f"wallet balance {bal/100:.2f} would not cover this payment plus fees")

    def reconcile(self, request_id):
        """Ask Airwallex what really happened to this request_id. Safe to repeat."""
        pay = self.s.one("SELECT * FROM payments WHERE request_id=?", request_id)
        try:
            t = self.p.get_transfer(pay["provider_id"]) if pay["provider_id"] else self.p.find_by_request_id(request_id)
        except Ambiguous as e:
            self.s.log("provider", "reconcile_ambiguous", "observed", pay["incident_id"], request_id=request_id, error=str(e))
            return self.payment(request_id)
        if t is None:
            if pay["status"] == "RESERVED" and self.now() - pay["updated_at"] >= 60:
                # Crashed between reserving and sending. Airwallex has never heard of this request_id,
                # so sending it now (same id, full pre-send checks, STOP respected) is the original intent.
                self.s.log("system", "recovering_stale_reservation", "decided", pay["incident_id"], request_id=request_id)
                return self.submit(request_id)
            if pay["status"] == "UNKNOWN":
                # Not found yet. Re-submitting with the SAME request_id cannot double pay:
                # Airwallex rejects a duplicate request_id and returns the original instead.
                return self.submit(request_id)
            return self.payment(request_id)
        self._apply_provider(request_id, t, "provider_get")
        return self.payment(request_id)

    def on_webhook(self, payload):
        """Webhooks are treated as a hint to look, never as the truth: we log the claim, then
        re-read the transfer from Airwallex. A forged, duplicated or out-of-order webhook therefore
        cannot change a payment's status by itself."""
        obj = (payload.get("data") or {}).get("object") or payload.get("data") or {}
        tid, rid = obj.get("id"), obj.get("request_id")
        row = self.s.one("SELECT request_id, incident_id FROM payments WHERE provider_id=? OR request_id=?", tid or "-", rid or "-")
        self.s.log("provider", "webhook_hint", "untrusted", row["incident_id"] if row else None,
                   name=payload.get("name"), claimed_status=obj.get("status"), transfer_id=tid, request_id=rid)
        if not row:
            return None
        # Throttle: anyone can POST a webhook, so at most one Airwallex re-read per payment every 5s.
        last = self._hint_at.get(row["request_id"], 0)
        if self.now() - last < 5:
            return self.payment(row["request_id"])
        self._hint_at[row["request_id"]] = self.now()
        return self.reconcile(row["request_id"])

    # ------------------------------------------------------------ incident
    def evidence(self, incident_id):
        """Everything the agent is allowed to see. No credentials, no raw provider payloads."""
        inc = self._incident(incident_id)
        pays = [dict(p) for p in self.s.q("SELECT request_id,role,status,failure_type,failure_message,status_source,amount,currency,created_at,updated_at FROM payments WHERE incident_id=? ORDER BY created_at", incident_id)]
        pol = self.s.policy()
        return {
            "incident": {k: inc[k] for k in ("id", "supplier", "invoice", "amount", "currency", "supplier_deadline", "state", "alert")},
            "payments": pays,
            "kill_switch": bool(pol["killed"]),
            "budget_left_today": pol["daily_cap"] - self.s.committed_today(self.now()),
            "now": self.now(),
            "supplier_messages": [json.loads(e["body"]).get("text") for e in self.s.q("SELECT body FROM events WHERE incident_id=? AND kind='supplier_message' ORDER BY id DESC LIMIT 5", incident_id)][::-1],
        }

    def allowed_actions(self, incident_id):
        """Code decides which actions are even possible; the agent picks among these."""
        ev = self.evidence(incident_id)
        pays = ev["payments"]
        live = [p for p in pays if in_flight(p["status"])]
        paid = [p for p in pays if p["status"] == "PAID"]
        acts = {"ESCALATE"}
        if live:
            acts.add("WAIT")
        if paid and not live:
            acts.add("CLOSE")
        last = pays[-1] if pays else None
        if (not live and not paid and last and last["status"] == "CANCELLED" and last["status_source"] == "provider_get"
                and last["failure_type"] in RETRYABLE and not ev["kill_switch"] and not ev["incident"]["alert"]):
            acts.add("REPLACE")
        return sorted(acts)

    def supplier_message(self, incident_id, text):
        self.s.log("supplier", "supplier_message", "untrusted", incident_id, text=text[:2000])

    def record_decision(self, incident_id, decision, reasons, source):
        allowed = self.allowed_actions(incident_id)
        if decision not in allowed:
            self.s.log("policy", "agent_decision_rejected", "decided", incident_id, proposed=decision, allowed=allowed)
            raise Refused(f"agent proposed {decision}, but code only allows {allowed}")
        self.s.log("agent", "decision", source, incident_id, decision=decision, reasons=reasons, allowed=allowed)
        if decision == "ESCALATE":
            with self.s.tx():
                self.s.db.execute("UPDATE incidents SET state='ESCALATED' WHERE id=?", (incident_id,))
        return decision

    def close(self, incident_id, operator, note=None):
        inc = self._incident(incident_id)
        if inc["alert"] and not (note and len(note.strip()) >= 10):
            raise Refused(f"open alert {inc['alert']}: write a short resolution note (what you checked, what happened) to close")
        pays = self.s.q("SELECT * FROM payments WHERE incident_id=?", incident_id)
        if not any(p["status"] == "PAID" for p in pays):
            raise Refused("nothing has been paid yet")
        open_ = [p["request_id"] for p in pays if p["status"] not in FINAL]
        if open_:
            raise Refused("another payment on this incident has no final status yet: " + ", ".join(open_))
        with self.s.tx():
            self.s.db.execute("UPDATE incidents SET state='RESOLVED' WHERE id=?", (incident_id,))
            self.s.log("operator", "incident_resolved", "operator", incident_id, by=operator, note=note,
                       outcome={p["request_id"]: [p["role"], p["status"], p["failure_type"]] for p in pays})

    def receipt(self, incident_id):
        inc = dict(self._incident(incident_id))
        return {
            "incident": inc,
            "payments": [dict(p) for p in self.s.q("SELECT * FROM payments WHERE incident_id=? ORDER BY created_at", incident_id)],
            "events": [dict(e, body=json.loads(e["body"])) for e in self.s.q("SELECT * FROM events WHERE incident_id=? OR incident_id IS NULL ORDER BY id", incident_id)],
            "legend": {
                "observed": "read back from the Airwallex sandbox API",
                "decided": "enforced by code (policy engine)",
                "model": "text written by the AI agent - an opinion, not a fact",
                "operator": "a human action",
                "untrusted": "supplier-provided text; never used as an instruction",
            },
            "environment": "Airwallex SANDBOX - no real money",
        }

    def payment(self, request_id):
        return dict(self.s.one("SELECT * FROM payments WHERE request_id=?", request_id))

    # -------------------------------------------------------------- internals
    def _incident(self, iid):
        inc = self.s.one("SELECT * FROM incidents WHERE id=?", iid)
        if not inc:
            raise Refused("no such incident")
        return inc

    def _check(self, ap, inc, role, beneficiary_id, amount, currency):
        pol = self.s.policy()
        if pol["killed"]:
            raise Refused("kill switch is on")
        if inc["state"] != "OPEN":
            raise Refused(f"incident is {inc['state']}")
        if ap["used_by_request"]:
            raise Refused("this approval was already used once")
        if ap["role"] != role:
            raise Refused(f"approval is for the {ap['role']} payment, not the {role}")
        if self.now() > ap["expires_at"]:
            raise Refused("approval expired")
        if (ap["beneficiary_id"], ap["amount"], ap["currency"]) != (beneficiary_id, amount, currency):
            raise Refused("terms differ from what the operator approved (beneficiary, amount or currency)")
        if not self.s.one("SELECT 1 FROM beneficiaries WHERE id=?", beneficiary_id):
            raise Refused("beneficiary is not on the allowlist")
        if currency != pol["currency"]:
            raise Refused("currency not allowed")
        if amount > pol["per_tx_cap"]:
            raise Refused("over the per-payment cap")
        if amount > pol["dual_approval_over"] and not (ap["cosigned_by"] and ap["cosigned_by"] != ap["approved_by"]):
            raise Refused(f"payments over {pol['dual_approval_over']/100:.2f} need a second person to co-sign the approval")
        if self.s.committed_today(self.now()) + amount > pol["daily_cap"]:
            raise Refused("would exceed today's cap (reserved and uncertain payments count)")
        if self.s.committed_today_for(beneficiary_id, self.now()) + amount > pol["per_supplier_daily_cap"]:
            raise Refused("would exceed today's cap for this one payee")
        recent = self.s.one("SELECT COUNT(*) n FROM payments WHERE created_at>?", self.now() - 3600)["n"]
        if recent >= pol["max_payments_per_hour"]:
            raise Refused(f"rate limit: {recent} payments in the last hour (max {pol['max_payments_per_hour']})")
        if inc["alert"]:
            raise Refused(f"incident has an open alert ({inc['alert']}); a person must resolve it first")
        pays = self.s.q("SELECT * FROM payments WHERE incident_id=?", inc["id"])
        if any(in_flight(p["status"]) for p in pays):
            raise Refused("duplicate lock: another payment on this incident may still go through")
        if any(p["status"] == "PAID" for p in pays):
            raise Refused("this incident already has a paid transfer")
        if role == "replacement":
            orig = [p for p in pays if p["role"] == "original"]
            if not orig:
                raise Refused("no original payment to replace")
            last = pays[-1]
            if last["status"] != "CANCELLED" or last["status_source"] != "provider_get":
                raise Refused("previous payment is not confirmed failed by a fresh Airwallex lookup")
            if last["failure_type"] not in RETRYABLE and not (inc["reopened_at"] and inc["reopened_at"] > last["updated_at"]):
                raise Refused(f"failure type {last['failure_type']} is not safe to retry automatically - a person must investigate and reopen the incident first")
        elif pays:
            raise Refused("an original payment already exists for this incident")

    def _apply_provider(self, request_id, t, source):
        status = t.get("status", "UNKNOWN")
        fail = t.get("failure") or {}
        ftype = (fail.get("details") or {}).get("type") or t.get("failure_type")
        # Map the provider's failure *reason* to our retry classes via the message when needed.
        ftype = _classify_failure(fail.get("message"), ftype) if status == "CANCELLED" else None
        prev = self.payment(request_id)
        fee = None
        if t.get("fee_amount") is not None and t.get("fee_paid_by", "PAYER") == "PAYER":
            fee = str_to_minor(t["fee_amount"])
        payer_pays = str_to_minor(t["amount_payer_pays"]) if t.get("amount_payer_pays") is not None else None
        self._set(request_id, status, source, provider_id=t.get("id"), failure_type=ftype, failure_message=fail.get("message"), fee=fee)
        if (fee or 0) > self.s.policy()["max_fee"] or (payer_pays and payer_pays > prev["amount"] + self.s.policy()["max_fee"]):
            self._anomaly(request_id, "FEE_OVER_LIMIT", f"Airwallex charges {((payer_pays or prev['amount'] + (fee or 0)))/100:.2f} for a {prev['amount']/100:.2f} payment")
        if prev["status"] != status:
            self.s.log("provider", "status_observed", "observed", prev["incident_id"], request_id=request_id,
                       provider_id=t.get("id"), status=status, previous=prev["status"], failure_type=ftype,
                       failure_message=fail.get("message"), via=source)

    def _set(self, request_id, status, source, provider_id=None, failure_type=None, failure_message=None, fee=None):
        flip = None
        with self.s.tx():
            row = self.s.one("SELECT status, incident_id FROM payments WHERE request_id=?", request_id)
            cur = row["status"]
            if cur in FINAL and status != cur:
                if source == "provider_get" and status in FINAL:
                    # A FINAL status changed at the provider itself (e.g. PAID then returned, or a
                    # "cancelled" transfer that paid after all). Record the truth and stop everything
                    # on this incident until a person looks.
                    flip = f"{cur}_THEN_{status}"
                else:
                    # Stale / reordered information never reopens a final payment.
                    self.s.log("provider", "late_update_ignored", "observed", row["incident_id"], request_id=request_id, had=cur, got=status)
                    return
            self.s.db.execute(
                "UPDATE payments SET status=?, status_source=?, provider_id=COALESCE(?,provider_id), failure_type=COALESCE(?,failure_type),"
                " failure_message=COALESCE(?,failure_message), fee=COALESCE(?,fee), updated_at=? WHERE request_id=?",
                (status, source, provider_id, failure_type, failure_message, fee, self.now(), request_id),
            )
        if flip:
            self._anomaly(request_id, flip, f"Airwallex changed a final status: {cur} -> {status}")
            if status == "PAID":
                paid = self.s.q("SELECT request_id FROM payments WHERE incident_id=? AND status='PAID'", row["incident_id"])
                if len(paid) > 1:
                    self._anomaly(request_id, "DOUBLE_PAID", "two transfers on this incident are PAID - ask the supplier to refund one")

    def _anomaly(self, request_id, code, detail):
        row = self.s.one("SELECT incident_id FROM payments WHERE request_id=?", request_id)
        with self.s.tx():
            self.s.db.execute("UPDATE payments SET anomaly=? WHERE request_id=?", (code, request_id))
        self.s.log("provider", "anomaly", "observed", row["incident_id"], request_id=request_id, code=code, detail=detail)
        self._alert(row["incident_id"], code)

    def _alert(self, incident_id, code):
        with self.s.tx():
            self.s.db.execute("UPDATE incidents SET alert=?, state=CASE WHEN state='RESOLVED' THEN 'RESOLVED' ELSE 'ESCALATED' END WHERE id=?", (code, incident_id))
        self.s.log("policy", "incident_alert", "decided", incident_id, code=code)

    def sweep(self, stale_after=600, sla=72 * 3600):
        """Background safety net: re-read anything uncertain or in flight that hasn't been checked
        lately, and raise an alert when a transfer sits in flight past the SLA."""
        out = []
        for p in self.s.q("SELECT * FROM payments WHERE status NOT IN ('PAID','CANCELLED')"):
            if self.now() - p["updated_at"] >= stale_after:
                out.append(self.reconcile(p["request_id"]))
            if self.now() - p["created_at"] > sla and in_flight(self.payment(p["request_id"])["status"]):
                inc = self._incident(p["incident_id"])
                if inc["alert"] != "OVERDUE_SLA":
                    self.s.log("policy", "sla_breached", "decided", p["incident_id"], request_id=p["request_id"], hours=int((self.now() - p["created_at"]) / 3600))
                    self._alert(p["incident_id"], "OVERDUE_SLA")
        return out


ALL_FAILURE_TYPES = [
    "ACCOUNT_CLOSED", "ACCOUNT_CURRENCY_MISMATCH", "ACCOUNT_INACTIVE_OR_DORMANT", "ACCOUNT_UNDER_RESTRICTION",
    "BENEFICIARY_BANK_RETURNED", "BENEFICIARY_NAME_MISMATCH", "BENEFICIARY_REQUESTED", "CARD_ISSUER_ERROR",
    "CHANNEL_POLICY", "CHANNEL_TIMEOUT", "CLIENT_REQUESTED", "DUPLICATION_RETURN", "EXCEEDED_TRANSACTION_AMOUNT_OR_LIMIT",
    "INSUFFICIENT_FUNDS", "INVALID_ACCOUNT_NAME_OR_NUMBER", "INVALID_BANK_INFORMATION", "INVALID_BANK_OR_BRANCH_CODE",
    "INVALID_BENEFICIARY_DETAILS", "INVALID_CORRESPONDENT_BANK_INFORMATION", "INVALID_PAYMENT_PURPOSE",
    "INVALID_SPECIAL_CHARACTER", "INVALID_SWIFT_BIC_CODE", "OTHER", "RECALL_REQUESTED", "SYSTEM_ERROR", "TM_SUSPENDED",
    "TRANSACTION_AMOUNT_NOT_COVERING_FEE", "UNABLE_TO_APPLY",
]


def _norm(x):
    import re
    return re.sub(r"[^a-z0-9]+", " ", (x or "").lower().replace("/", " or ")).strip()


_BY_MESSAGE = {_norm(t.replace("_", " ")): t for t in ALL_FAILURE_TYPES}


def _classify_failure(message, detail_type):
    """Airwallex reports the failure reason in failure.message (e.g. "Channel timeout").
    In the sandbox failure.details.type is always INCORRECT_ROUTING, so we do not trust it.
    Anything we cannot classify is UNCLASSIFIED, which is never auto-retried."""
    return _BY_MESSAGE.get(_norm(message), "UNCLASSIFIED")


def _norm_name(x):
    return re.sub(r"[^A-Z0-9]", "", str(x or "").upper().replace("LLC", "").replace("INC", "").replace("LTD", ""))


_REF_OK = re.compile(r"[^A-Za-z0-9 #./_-]")


def clean_ref(x):
    """Invoice/reference text ends up in bank references and in the agent's context: keep it boring."""
    x = _REF_OK.sub("", str(x or "")).strip()[:40]
    if not x:
        raise Refused("invoice number is required (letters, digits, # . / _ -)")
    return x
