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


REQ_NS = uuid.UUID("5b1e8a0e-6b8f-4d0a-9a51-0a1d5f2c7e11")


ALERT_RANK = {"CANCELLED_BUT_LIVE": 96, "PROVIDER_MISMATCH": 97, "PAID_THEN_FAILED": 92, "DOUBLE_PAID": 100, "BENEFICIARY_CHANGED": 95, "CANCELLED_THEN_PAID": 90, "CANCELLED_LOCALLY_BUT_EXISTS": 85,
              "PAID_THEN_CANCELLED": 80, "FRAUD_SIGNAL": 70, "OVERDUE_SLA": 40, "FEE_OVER_LIMIT": 10}


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
            old = self.s.one("SELECT details_hash, details_changed_at FROM beneficiaries WHERE id=?", provider_id)
            changed_at = (self.now() if old and old["details_hash"] and old["details_hash"] != h else (old["details_changed_at"] if old else None))
            twin = self.s.one("SELECT id,name FROM beneficiaries WHERE account_key=? AND id<>?", ak, provider_id)
            if twin and _norm_name(twin["name"]) != _norm_name(name) and not override_reason:
                self.s.log("policy", "beneficiary_refused", "decided", None, id=provider_id, name=name, reason="same bank account as " + twin["name"])
                raise Refused(f"this bank account is already on file under a different supplier name ({twin['name']}) - a common money-mule pattern; needs an override reason")
            self.s.db.execute(
                "INSERT OR REPLACE INTO beneficiaries(id,name,bank_fingerprint,approved_by,approved_at,details_hash,account_key,details_changed_at) VALUES(?,?,?,?,?,?,?,?)",
                (provider_id, name, fingerprint, operator, self.now(), h, ak, changed_at),
            )
            self.s.log("operator", "beneficiary_allowed", "operator", None, id=provider_id, name=name, fingerprint=fingerprint,
                       by=operator, details_hash=h[:12], override=override_reason)

    def open_incident(self, supplier, invoice, beneficiary_id, amount, currency, deadline=None, allow_similar=False):
        invoice = clean_ref(invoice)
        if not isinstance(amount, int) or amount <= 0:
            raise Refused("amount must be a positive whole number of cents")
        # Dedupe on WHO GETS PAID + invoice number. The supplier name is free text and must not matter.
        key = beneficiary_id + "|" + _norm_invoice(invoice)
        with self.s.tx():
            if not self.s.one("SELECT 1 FROM beneficiaries WHERE id=?", beneficiary_id):
                raise Refused("beneficiary is not on the operator's allowlist")
            dup = self.s.one("SELECT id FROM incidents WHERE invoice_key=?", key)
            if dup:
                raise Refused(f"invoice {invoice} for this payee is already handled in incident #{dup['id']} - duplicate invoice")
            recent = self.s.one("SELECT COUNT(*) n FROM incidents WHERE created_at>?", self.now() - 3600)["n"]
            if recent >= self.s.policy()["max_incidents_per_hour"]:
                raise Refused("too many new payment requests in the last hour - intake paused")
            if not allow_similar:
                same = self.s.one("SELECT id,invoice FROM incidents WHERE beneficiary_id=? AND amount=? AND created_at>?",
                                  beneficiary_id, amount, self.now() - 30 * 86400)
                if same:
                    raise Refused(f"same payee and same amount as {same['invoice']} (incident #{same['id']}) within 30 days - looks like a reminder, "
                                  "statement or corrected copy of the same bill; confirm it is a separate invoice to continue")
            twin = self.s.one("SELECT id,beneficiary_id FROM incidents WHERE amount=? AND invoice_key LIKE ? AND beneficiary_id<>?",
                              amount, "%|" + _norm_invoice(invoice), beneficiary_id)
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
        if role == "replacement":
            pays = self.s.q("SELECT status,status_source FROM payments WHERE incident_id=? ORDER BY created_at", incident_id)
            if not pays or any(in_flight(p["status"]) for p in pays) or pays[-1]["status"] != "CANCELLED" or pays[-1]["status_source"] != "provider_get":
                raise Refused("a replacement can only be approved after Airwallex confirms the previous payment failed")
        if (b, a, c) != (inc["beneficiary_id"], inc["amount"], inc["currency"]):
            self.s.log("policy", "approval_refused", "decided", incident_id, reason="approval terms differ from the invoice")
            raise Refused("an approval must match the invoice exactly (payee, amount, currency)")
        if ttl > 24 * 3600:
            raise Refused("approvals expire within 24 hours")
        with self.s.tx():
            ph = self.s.one("SELECT details_hash FROM beneficiaries WHERE id=?", b)
            cur = self.s.db.execute(
                "INSERT INTO approvals(incident_id,role,beneficiary_id,amount,currency,expires_at,approved_by,payee_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (incident_id, role, b, a, c, self.now() + ttl, operator, ph["details_hash"] if ph else None, self.now()),
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

    def request_action(self, kind, target, note, operator):
        if not note or len(note.strip()) < 15:
            raise Refused("write what you checked (at least a sentence) - it is kept in the evidence log")
        with self.s.tx():
            cur = self.s.db.execute("INSERT INTO pending_actions(kind,target,note,requested_by,created_at) VALUES(?,?,?,?,?)",
                                    (kind, str(target), note.strip()[:1000], operator, self.now()))
        self.s.log("operator", "two_person_requested", "operator", int(target) if kind == "reopen" else None,
                   action=kind, target=target, by=operator, note=note.strip()[:1000])
        return cur.lastrowid

    def confirm_action(self, pending_id, operator):
        """Returns the pending row once a DIFFERENT person confirms it (within 1 hour)."""
        with self.s.tx():
            a = self.s.one("SELECT * FROM pending_actions WHERE id=?", pending_id)
            if not a or a["done_by"]:
                raise Refused("no such open request")
            if a["requested_by"] == operator:
                raise Refused("a different person must confirm this")
            if self.now() - a["created_at"] > 3600:
                raise Refused("request expired - ask again")
            self.s.db.execute("UPDATE pending_actions SET done_by=?, done_at=? WHERE id=?", (operator, self.now(), pending_id))
        self.s.log("operator", "two_person_confirmed", "operator", int(a["target"]) if a["kind"] == "reopen" else None,
                   action=a["kind"], target=a["target"], requested_by=a["requested_by"], confirmed_by=operator)
        return dict(a)

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

    def _desk_id(self):
        r = self.s.one("SELECT v FROM meta WHERE k='desk_id'")
        if r:
            return r["v"]
        v = str(uuid.uuid4())
        with self.s.lock:
            self.s.db.execute("INSERT OR IGNORE INTO meta VALUES('desk_id',?)", (v,))
        return self.s.one("SELECT v FROM meta WHERE k='desk_id'")["v"]

    def kill(self, operator, reason="operator stop"):
        with self.s.tx():
            pol = self.s.policy()
            if pol["killed"] and pol["killed_by"] == "system" and operator != "system":
                # Pressing STOP on top of a system STOP must not lower what it takes to resume
                # (a system STOP needs two people to clear).
                self.s.log("operator", "kill_switch_pressed_again", "operator", None, by=operator, kept_reason=pol["killed_reason"])
                return
            self.s.db.execute("UPDATE policy SET killed=1, killed_reason=?, killed_by=? WHERE id=1", (reason, operator))
            self.s.log("operator", "kill_switch_on", "operator", None, by=operator, reason=reason)

    def unkill(self, operator, confirmed_by=None, single_operator_mode=False):
        """A STOP the desk raised itself (payee edited, unknown transfers...) needs two different people.
        single_operator_mode is only for a one-token demo setup; it is logged so the receipt shows it."""
        with self.s.tx():
            pol = self.s.policy()
            if pol["killed"] and pol["killed_by"] == "system" and not single_operator_mode:
                if not confirmed_by or confirmed_by == operator:
                    raise Refused("the desk stopped itself; a second person must confirm before it resumes")
            if pol["killed"] and pol["killed_by"] == "system" and single_operator_mode:
                self.s.log("policy", "two_person_rule_unavailable", "decided", None, by=operator,
                           note="single-operator setup: system STOP cleared by one person")
            self.s.db.execute("UPDATE policy SET killed=0, killed_reason=NULL, killed_by=NULL WHERE id=1")
            self.s.log("operator", "kill_switch_off", "operator", None, by=operator)

    # ---------------------------------------------------------------- pay
    def pay(self, approval_id, role, beneficiary_id, amount, currency):
        """Reserve + submit one payment under an approval. Terms must match the approval exactly."""
        try:
            if role == "replacement":
                self._fresh_read_before_replacement(approval_id)
            try:
                return self._pay(None, approval_id, role, beneficiary_id, amount, currency)
            except sqlite3.IntegrityError as e:
                raise Refused(f"database duplicate lock: {e}") from None
        except Refused as e:
            ap = self.s.one("SELECT incident_id FROM approvals WHERE id=?", approval_id)
            # logged after the rollback, so the refusal itself is always on the record
            self.s.log("policy", "payment_refused", "decided", ap["incident_id"] if ap else None,
                       role=role, reason=str(e), amount=amount, beneficiary_id=beneficiary_id)
            raise

    def _fresh_read_before_replacement(self, approval_id):
        """A cached CANCELLED is not proof: Airwallex can still move a transfer. Re-read every earlier
        payment on the incident right now; if any read fails, no replacement is sent."""
        ap = self.s.one("SELECT incident_id FROM approvals WHERE id=?", approval_id)
        if not ap:
            raise Refused("no such approval")
        for p in self.s.q("SELECT request_id FROM payments WHERE incident_id=?", ap["incident_id"]):
            # strict=True: a failed or not-found read raises here. A cached CANCELLED, however recent,
            # never counts - the evidence has to come from the read made in this very attempt.
            try:
                after = self.reconcile(p["request_id"], strict=True)
            except (Ambiguous, ProviderError) as e:
                raise Refused(f"could not re-read the earlier payment from Airwallex right now ({e}) - no replacement sent") from None
            if after["status_source"] != "provider_get" or after["checked_at"] is None or self.now() - after["checked_at"] > 30:
                raise Refused("could not get a fresh Airwallex status for the earlier payment - no replacement sent")

    def _pay(self, request_id, approval_id, role, beneficiary_id, amount, currency):
        with self.s.tx():
            ap = self.s.one("SELECT * FROM approvals WHERE id=?", approval_id)
            if not ap:
                raise Refused("no such approval")
            iid = ap["incident_id"]
            inc = self._incident(iid)
            self._check(ap, inc, role, beneficiary_id, amount, currency)
            # Deterministic idempotency key: (incident, attempt number). The same attempt always maps to
            # the same request_id, so even a crash before this row commits can't mint a second transfer.
            attempt = self.s.one("SELECT COUNT(*) n FROM payments WHERE incident_id=?", iid)["n"] + 1
            request_id = str(uuid.uuid5(REQ_NS, f"{self._desk_id()}:incident:{iid}:attempt:{attempt}"))
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

    def submit(self, request_id, first=True):
        """Send a reserved payment (first=True, called right after reserving), or - only when an
        OPERATOR clicks re-send - send it again with the SAME request_id (first=False).
        Background jobs and webhooks never call this: they only look things up."""
        pay = self.payment(request_id)
        iid = pay["incident_id"]

        def block(code, msg, alert=None):
            if first and pay["status"] == "RESERVED":
                # We know nothing was sent in this call -> release the reservation.
                self._set(request_id, "CANCELLED", "none", failure_type=code, failure_message=msg)
                tail = " - nothing was sent"
            else:
                # A previous attempt MIGHT have reached Airwallex: never release, a person decides.
                tail = " - payment kept on hold (it may already have been sent); nothing new was sent"
            self.s.log("policy", "presubmit_blocked", "decided", iid, request_id=request_id, code=code, reason=msg, first=first)
            if alert:
                self._alert(iid, alert)
            raise Refused(msg + tail)

        self._gate(pay, block)
        self._presubmit_checks(pay, block)
        with self.s.tx():  # last look right before the money call: STOP / alarms may have changed meanwhile
            now_row = self.payment(request_id)
            if now_row["status"] not in ("RESERVED", "UNKNOWN"):
                # Someone released or settled it while the checks ran: sending now would create money the desk
                # no longer tracks. Nothing is sent.
                self.s.log("policy", "presubmit_blocked", "decided", iid, request_id=request_id, code="STATE_CHANGED",
                           reason=f"payment became {now_row['status']} during the pre-send checks", first=first)
                raise Refused(f"this payment became {now_row['status']} while the send was being checked - nothing was sent")
            self._gate(now_row, block)
            # Stamp the attempt inside the same lock: release() refuses for 15 minutes after this.
            self.s.db.execute("UPDATE payments SET updated_at=? WHERE request_id=?", (self.now(), request_id))
        inc = self._incident(iid)
        try:
            t = self.p.create_transfer(request_id, pay["beneficiary_id"], pay["amount"], pay["currency"], f"{inc['invoice']} {pay['role']}")
        except Ambiguous as e:
            self._set(request_id, "UNKNOWN", "none")
            self.s.log("provider", "submit_ambiguous", "observed", iid, request_id=request_id, error=str(e))
            return self.payment(request_id)
        except ProviderError as e:
            if first:
                # A definite rejection of a brand-new request: nothing was created, budget is released.
                self._set(request_id, "CANCELLED", "provider_create", failure_type="REJECTED_" + e.code.upper(), failure_message=e.message)
            else:
                self._set(request_id, "UNKNOWN", "none")  # an error on a re-send proves nothing about the first attempt
            self.s.log("provider", "submit_rejected", "observed", iid, request_id=request_id, code=e.code, message=e.message, first=first)
            return self.payment(request_id)
        self._apply_provider(request_id, t, "provider_create")
        return self.payment(request_id)

    def _gate(self, pay, block):
        pol, inc = self.s.policy(), self._incident(pay["incident_id"])
        if pol["killed"]:
            block("KILLED_BEFORE_SUBMIT", "STOP is on")
        if inc["state"] != "OPEN" or inc["alert"]:
            block("INCIDENT_NOT_OPEN", f"incident is {inc['state']}" + (f" with alarm {inc['alert']}" if inc["alert"] else ""))

    def _presubmit_checks(self, pay, block):
        """Network checks before EVERY send (first or operator re-send)."""
        ben = self.s.one("SELECT * FROM beneficiaries WHERE id=?", pay["beneficiary_id"])
        ap = self.s.one("SELECT payee_hash FROM approvals WHERE id=?", pay["approval_id"])
        try:
            current = self.p.get_beneficiary(pay["beneficiary_id"])
        except (Ambiguous, ProviderError) as e:
            block("DRIFT_CHECK_UNAVAILABLE", f"could not re-read the payee's bank details ({e})")
        bd = (current.get("beneficiary") or current).get("bank_details") or {}
        allowed = {c.strip() for c in self.s.policy()["allowed_countries"].split(",")}
        if bd.get("bank_country_code") and bd["bank_country_code"] not in allowed:
            block("COUNTRY_NOT_ALLOWED", f"payee bank is in {bd['bank_country_code']}, outside the allowed countries ({', '.join(sorted(allowed))})")
        cur_ccy = bd.get("account_currency")
        if cur_ccy and cur_ccy != pay["currency"]:
            block("ACCOUNT_CURRENCY_MISMATCH_PRECHECK", f"the payee's account is in {cur_ccy}, not {pay['currency']}")
        h = details_hash(current)
        if (ben["details_hash"] and h != ben["details_hash"]) or (ap and ap["payee_hash"] and h != ap["payee_hash"]):
            self.kill("system", "payee bank details changed after approval")
            block("BENEFICIARY_CHANGED", "the payee's bank details at Airwallex changed after they were approved", alert="BENEFICIARY_CHANGED")
        try:
            bal = self.p.usd_balance_minor()
        except (Ambiguous, ProviderError):
            bal = None
        if bal is not None and bal < pay["amount"] + self.s.policy()["max_fee"]:
            block("INSUFFICIENT_BALANCE_PRECHECK", f"wallet balance {bal/100:.2f} would not cover this payment plus fees")

    def reconcile(self, request_id, strict=False):
        """Ask Airwallex what really happened to this request_id. Read-only towards Airwallex:
        it never sends anything, so the sweeper and webhooks can call it safely.
        strict=True (used before a replacement): a failed or empty read raises instead of returning the cached row."""
        pay = self.payment(request_id)
        try:
            t = self.p.get_transfer(pay["provider_id"]) if pay["provider_id"] else self.p.find_by_request_id(request_id)
        except (Ambiguous, ProviderError) as e:
            self.s.log("provider", "reconcile_failed", "observed", pay["incident_id"], request_id=request_id, error=str(e))
            if strict:
                raise
            return self.payment(request_id)
        if t is not None and not self._usable_transfer(t):
            # An empty or half-filled reply is not an observation. Never let it stand in for one.
            self.s.log("provider", "reconcile_failed", "observed", pay["incident_id"], request_id=request_id,
                       error="incomplete transfer record from Airwallex", got=str(t)[:200])
            if strict:
                raise Ambiguous("Airwallex returned an incomplete record for the earlier payment")
            return self.payment(request_id)
        if t is None:
            if strict:
                raise Ambiguous("Airwallex has no record of the earlier payment")
            if pay["status"] in ("UNKNOWN", "RESERVED"):
                last = self._hint_at.get(("nf", request_id), 0)
                if self.now() - last >= 3600:
                    self._hint_at[("nf", request_id)] = self.now()
                    self.s.log("provider", "not_found_at_airwallex", "observed", pay["incident_id"], request_id=request_id,
                               note="still held; an operator can re-send with the same request_id")
            return self.payment(request_id)
        read_at = self.now()
        applied = self._apply_provider(request_id, t, "provider_get")
        after = self.payment(request_id)
        if strict and applied is False:
            raise Ambiguous("Airwallex returned a record that does not describe our transfer")
        if strict and (after["status"] != t["status"] or after["status_source"] != "provider_get" or (after["checked_at"] or 0) < read_at):
            # The read was refused (mismatch, alarm) or could not be applied as-is: it proves nothing.
            raise Ambiguous(f"Airwallex says {t['status']} but that could not be recorded as-is ({after['status']})")
        return after

    @staticmethod
    def _usable_transfer(t):
        return (isinstance(t, dict) and isinstance(t.get("status"), str) and t["status"] in IN_FLIGHT + ("PAID", "CANCELLED", "RETURNED", "REVERSED", "SUSPENDED")
                and t["status"] not in ("RESERVED", "UNKNOWN") and bool(t.get("id") or t.get("request_id"))
                and (t.get("failure") is None or (isinstance(t["failure"], dict)
                     and (t["failure"].get("details") is None or isinstance(t["failure"]["details"], dict)))))

    def resend(self, request_id, operator):
        """Operator action for a payment stuck UNKNOWN / RESERVED that Airwallex has never heard of.
        Same request_id (Airwallex dedupes), full checks again, alarms and STOP respected."""
        pay = self.payment(request_id)
        if pay["status"] not in ("UNKNOWN", "RESERVED"):
            raise Refused(f"payment is {pay['status']}, nothing to re-send")
        if pay["status"] == "RESERVED" and self.now() - pay["updated_at"] < 60:
            raise Refused("the first send may still be in progress - try again in a minute")
        ap = self.s.one("SELECT expires_at FROM approvals WHERE id=?", pay["approval_id"])
        if not ap or self.now() >= ap["expires_at"]:
            self.s.log("policy", "resend_refused", "decided", pay["incident_id"], request_id=request_id, reason="approval expired")
            raise Refused("the approval for this payment has expired, so it cannot be sent on that approval. It stays held "
                          "(it is checked against Airwallex automatically); if Airwallex never received it, release it and approve again")
        r = self.reconcile(request_id)
        if r["status"] not in ("UNKNOWN", "RESERVED"):
            return r  # it existed after all
        self.s.log("operator", "resend_requested", "operator", pay["incident_id"], request_id=request_id, by=operator)
        return self.submit(request_id, first=False)

    def release(self, request_id, operator, note):
        """Free a held payment (RESERVED/UNKNOWN) that Airwallex has no record of, e.g. after its approval
        expired. Looks it up first; if it appears later anyway, _set raises CANCELLED_LOCALLY_BUT_EXISTS."""
        if not note or len(note.strip()) < 15:
            raise Refused("write why it is safe to release this payment (at least a sentence)")
        pay = self.payment(request_id)
        if pay["status"] not in ("UNKNOWN", "RESERVED"):
            raise Refused(f"payment is {pay['status']}; only a held payment can be released")
        if self.now() - pay["updated_at"] < 15 * 60:
            raise Refused("wait at least 15 minutes after the last send attempt before releasing a held payment")
        r = self.reconcile(request_id)
        if r["status"] not in ("UNKNOWN", "RESERVED"):
            return r  # Airwallex has it after all - nothing to release
        if self.p.find_by_request_id(request_id) is not None:
            raise Refused("Airwallex now shows this payment - reconcile instead of releasing")
        with self.s.tx():  # a re-send may have started meanwhile: re-check under the same lock submit() uses
            row = self.payment(request_id)
            if row["status"] not in ("UNKNOWN", "RESERVED") or self.now() - row["updated_at"] < 15 * 60:
                raise Refused("a send attempt started on this payment while you were releasing it - nothing released")
            self._set(request_id, "CANCELLED", "none", failure_type="RELEASED_BY_OPERATOR", failure_message=note.strip()[:300])
        self.s.log("operator", "payment_released", "operator", pay["incident_id"], request_id=request_id, by=operator, note=note.strip()[:1000])
        return self.payment(request_id)

    def on_webhook(self, payload, event_id=None):
        """Webhooks are treated as a hint to look, never as the truth: we log the claim, then
        re-read the transfer from Airwallex. A forged, duplicated or out-of-order webhook therefore
        cannot change a payment's status by itself."""
        event_id = event_id or payload.get("id")
        obj = (payload.get("data") or {}).get("object") or payload.get("data") or {}
        tid, rid = obj.get("id"), obj.get("request_id")
        row = self.s.one("SELECT request_id, incident_id FROM payments WHERE provider_id=? OR request_id=?", tid or "-", rid or "-")
        if not row:
            return None  # not ours: dropped before anything is stored (else anyone could grow the database)
        if event_id:
            try:
                with self.s.tx():
                    self.s.db.execute("DELETE FROM webhook_events WHERE at<?", (self.now() - 7 * 86400,))  # bounded retention
                    self.s.db.execute("INSERT INTO webhook_events VALUES(?,?)", (str(event_id)[:100], self.now()))
            except sqlite3.IntegrityError:
                return None  # same event delivered again (Airwallex retries for ~3 days) - already handled
        self.s.log("provider", "webhook_hint", "untrusted", row["incident_id"],
                   name=str(payload.get("name"))[:80], claimed_status=str(obj.get("status"))[:40], transfer_id=tid, request_id=rid)
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
        from .agent import fraud_signals
        with self.s.lock:  # message + alarm land together; a decision can't be recorded in between
            self.s.log("supplier", "supplier_message", "untrusted", incident_id, text=text[:2000])
            if fraud_signals([text]):
                # Sticky: later "any update?" messages can't push it out of view.
                self._alert(incident_id, "FRAUD_SIGNAL")

    @staticmethod
    def _evidence_key(ev):
        """What a decision was based on: payments, alerts, supplier messages, state - not the clock."""
        e = dict(ev or {})
        for k in ("now", "budget_left_today"):  # desk-wide figures: another incident's payment doesn't make this advice stale
            e.pop(k, None)
        return json.dumps(e, sort_keys=True, default=str)

    def record_decision(self, incident_id, decision, reasons, source, evidence=None, answered_by=None):
        if self._incident(incident_id)["state"] == "RESOLVED":
            raise Refused("incident is resolved; the agent has nothing to decide")
        allowed = self.allowed_actions(incident_id)
        if decision not in allowed:
            self.s.log("policy", "agent_decision_rejected", "decided", incident_id, proposed=decision, allowed=allowed)
            raise Refused(f"agent proposed {decision}, but code only allows {allowed}")
        import hashlib
        ev_hash = hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()[:16] if evidence else None
        stale = []

        def still_current():
            # Runs under the store lock, right before the append (supplier messages and alarms take the same lock).
            if evidence is None or decision == "ESCALATE":
                return
            if self._evidence_key(self.evidence(incident_id)) != self._evidence_key(evidence) or decision not in self.allowed_actions(incident_id):
                stale.append(1)
                raise Refused("the incident changed while the agent was deciding; that advice is stale and was not recorded")
        try:
            self.s.log("agent", "decision", source, incident_id, _guard=still_current, decision=decision, reasons=reasons,
                       allowed=allowed, evidence_hash=ev_hash, evidence=evidence, answered_by=answered_by)
        except Refused:
            if stale:
                self.s.log("policy", "agent_decision_stale", "decided", incident_id, proposed=decision, allowed=allowed)
            raise
        if decision == "ESCALATE":
            with self.s.tx():
                self.s.db.execute("UPDATE incidents SET state='ESCALATED' WHERE id=? AND state='OPEN'", (incident_id,))
        return decision

    def close(self, incident_id, operator, note=None):
        with self.s.tx():  # every check and the close itself in one transaction: a reversal can't land in between
            self._close_locked(incident_id, operator, note)

    def _close_locked(self, incident_id, operator, note):
        inc = self._incident(incident_id)
        if inc["alert"] and not (note and len(note.strip()) >= 10):
            raise Refused(f"open alert {inc['alert']}: write a short resolution note (what you checked, what happened) to close")
        pays = self.s.q("SELECT * FROM payments WHERE incident_id=?", incident_id)
        if not any(p["status"] == "PAID" for p in pays):
            raise Refused("nothing has been paid yet")
        open_ = [p["request_id"] for p in pays if p["status"] not in FINAL]
        if open_:
            raise Refused("another payment on this incident has no final status yet: " + ", ".join(open_))
        if True:
            self.s.db.execute("UPDATE incidents SET state='RESOLVED' WHERE id=?", (incident_id,))
            self.s.log("operator", "incident_resolved", "operator", incident_id, by=operator, note=note,
                       outcome={p["request_id"]: [p["role"], p["status"], p["failure_type"]] for p in pays})

    def receipt(self, incident_id):
        inc = dict(self._incident(incident_id))
        return {
            "incident": inc,
            "payments": [dict(p) for p in self.s.q("SELECT * FROM payments WHERE incident_id=? ORDER BY created_at", incident_id)],
            "events": [dict(e, body=json.loads(e["body"])) for e in self.s.q(
                "SELECT * FROM events WHERE incident_id=? OR (incident_id IS NULL AND kind IN ('kill_switch_on','kill_switch_off','beneficiary_allowed','orphan_transfer')) ORDER BY id", incident_id)],
            "legend": {
                "observed": "read back from the Airwallex sandbox API",
                "decided": "enforced by code (policy engine)",
                "model": "text written by the AI agent - an opinion, not a fact",
                "operator": "a human action",
                "untrusted": "supplier-provided text; never used as an instruction",
            },
            "environment": "Airwallex SANDBOX - no real money",
            "proof_of_payment": [{"role": p["role"], "airwallex_transfer_id": p["provider_id"], "status": p["status"]}
                                 for p in self.s.q("SELECT role,provider_id,status FROM payments WHERE incident_id=? AND status='PAID'", incident_id)],
            "log_integrity": dict(zip(("intact", "entries_checked", "first_bad_id"), self.s.verify_chain())),
            "log_head": (self.s.one("SELECT hash FROM events ORDER BY id DESC LIMIT 1") or {"hash": None})["hash"],
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
        if self.now() >= ap["expires_at"]:
            raise Refused("approval expired")
        if (ap["beneficiary_id"], ap["amount"], ap["currency"]) != (beneficiary_id, amount, currency):
            raise Refused("terms differ from what the operator approved (beneficiary, amount or currency)")
        ben = self.s.one("SELECT * FROM beneficiaries WHERE id=?", beneficiary_id)
        if not ben:
            raise Refused("beneficiary is not on the allowlist")
        if ap["payee_hash"] and ap["payee_hash"] != ben["details_hash"]:
            raise Refused("the payee's verified bank details changed after this approval was given - approve again")
        if ben["details_changed_at"] and self.now() - ben["details_changed_at"] < pol["changed_payee_window"] and amount > pol["changed_payee_cap"]:
            raise Refused(f"this payee's bank details changed recently - payments are capped at {pol['changed_payee_cap']/100:.2f} for {pol['changed_payee_window']//3600}h (send a small test payment first)")
        if currency != pol["currency"]:
            raise Refused("currency not allowed")
        if amount > pol["per_tx_cap"]:
            raise Refused("over the per-payment cap")
        day_to_payee = self.s.one(
            "SELECT COALESCE(SUM(p.amount),0) s FROM payments p WHERE p.status<>'CANCELLED' AND p.created_at>? AND p.beneficiary_id IN "
            "(SELECT id FROM beneficiaries WHERE account_key=(SELECT account_key FROM beneficiaries WHERE id=?) UNION SELECT ?)",
            self.now() - 86400, beneficiary_id, beneficiary_id)["s"]
        cosigned = ap["cosigned_by"] and ap["cosigned_by"] != ap["approved_by"]
        if amount > pol["dual_approval_over"] and not cosigned:
            raise Refused(f"payments over {pol['dual_approval_over']/100:.2f} need a second person to co-sign the approval")
        if day_to_payee + amount > pol["dual_approval_over"] and not cosigned:
            raise Refused(f"this payee would receive {(day_to_payee + amount)/100:.2f} in 24h - splitting payments under the co-sign limit still needs a co-signer")
        if self.s.committed_today(self.now()) + amount + pol["max_fee"] > pol["daily_cap"]:
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
        if len(pays) >= pol["max_attempts"]:
            raise Refused(f"already {len(pays)} attempts on this invoice (max {pol['max_attempts']}); each failed transfer can cost a non-refundable fee - escalate instead")
        if role == "replacement":
            orig = [p for p in pays if p["role"] == "original"]
            if not orig:
                raise Refused("no original payment to replace")
            last = pays[-1]
            if ap["created_at"] < last["updated_at"]:
                raise Refused("this approval was given before the last payment's outcome was known - approve again")
            if last["status"] != "CANCELLED" or last["status_source"] != "provider_get":
                raise Refused("previous payment is not confirmed failed by a fresh Airwallex lookup")
            if last["failure_type"] not in RETRYABLE and not (inc["reopened_at"] and inc["reopened_at"] > last["updated_at"]):
                raise Refused(f"failure type {last['failure_type']} is not safe to retry automatically - a person must investigate and reopen the incident first")
        elif any(not (p["status"] == "CANCELLED" and p["status_source"] == "none") for p in pays):
            # Only payments that were blocked here before anything was sent (low balance, STOP, payee re-read)
            # leave room for a fresh original; anything Airwallex may have seen needs the replacement path.
            raise Refused("an original payment already exists for this incident")

    def _mismatch(self, prev, t):
        """Does this provider record actually describe OUR transfer? Only fields present are compared."""
        why = []
        if t.get("request_id") and t["request_id"] != prev["request_id"]:
            why.append("request_id")
        if prev["provider_id"] and t.get("id") and t["id"] != prev["provider_id"]:
            why.append("transfer id")
        if t.get("beneficiary_id") and t["beneficiary_id"] != prev["beneficiary_id"]:
            why.append("payee")
        if t.get("transfer_currency") and t["transfer_currency"] != prev["currency"]:
            why.append("currency")
        if t.get("transfer_amount") is not None:
            try:
                if str_to_minor(t["transfer_amount"]) != prev["amount"]:
                    why.append("amount")
            except (TypeError, ValueError):
                why.append("amount")
        return why

    def _apply_provider(self, request_id, t, source):
        prev0 = self.payment(request_id)
        bad = self._mismatch(prev0, t)
        if bad:
            self.s.log("provider", "provider_mismatch", "observed", prev0["incident_id"], request_id=request_id,
                       fields=bad, got_id=str(t.get("id"))[:80], got_request_id=str(t.get("request_id"))[:80], got_status=str(t.get("status"))[:40])
            self._anomaly(request_id, "PROVIDER_MISMATCH", "Airwallex returned a transfer that does not match ours (" + ", ".join(bad) + ")")
            return False
        status = t.get("status", "UNKNOWN")
        fail = t.get("failure") if isinstance(t.get("failure"), dict) else {}
        det = fail.get("details") if isinstance(fail.get("details"), dict) else {}
        ftype = det.get("type") or t.get("failure_type")
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
            row = self.s.one("SELECT status, status_source, incident_id FROM payments WHERE request_id=?", request_id)
            cur = row["status"]
            if cur == "CANCELLED" and row["status_source"] in ("none", "provider_create") and source in ("provider_get", "provider_create") and status != "CANCELLED":
                # We had assumed "never created", but Airwallex has it after all: the truth wins.
                flip = "CANCELLED_LOCALLY_BUT_EXISTS"
            elif cur == "PAID" and source == "provider_get" and status not in FINAL and status in ("FAILED", "RETURNED", "REVERSED", "SUSPENDED", "OVERDUE"):
                # A fresh read says a PAID transfer is now failing/returned: it is no longer paid.
                flip = "PAID_THEN_FAILED"
            elif cur in FINAL and status != cur:
                if source == "provider_get" and status in FINAL:
                    # A FINAL status changed at the provider itself (e.g. PAID then returned, or a
                    # "cancelled" transfer that paid after all). Record the truth and stop everything
                    # on this incident until a person looks.
                    flip = f"{cur}_THEN_{status}"
                elif source == "provider_get" and cur == "CANCELLED":
                    # A fresh read says a transfer we saw cancelled is moving again. It may be an out-of-order read,
                    # so the status is not reopened - but the incident is frozen and alarmed until a later read or a
                    # person settles it (found by the Watchdog). The sweeper keeps re-reading recent cancellations.
                    self.s.log("provider", "cancelled_but_live_read", "observed", row["incident_id"], request_id=request_id, got=status)
                    self._anomaly(request_id, "CANCELLED_BUT_LIVE", f"Airwallex shows a cancelled transfer as {status}")
                    return
                else:
                    # Stale / reordered information never reopens a final payment.
                    self.s.log("provider", "late_update_ignored", "observed", row["incident_id"], request_id=request_id, had=cur, got=status)
                    return
            changed = (status != cur or flip)
            self.s.db.execute(
                "UPDATE payments SET status=?, status_source=?, provider_id=COALESCE(?,provider_id), failure_type=COALESCE(?,failure_type),"
                " failure_message=COALESCE(?,failure_message), fee=COALESCE(?,fee), checked_at=?,"
                " updated_at=CASE WHEN ? THEN ? ELSE updated_at END WHERE request_id=?",
                (status, source, provider_id, failure_type, failure_message, fee, self.now(), 1 if changed else 0, self.now(), request_id),
            )
            # The alarm is raised INSIDE the same transaction as the status change: no other thread can see
            # the new status (e.g. CANCELLED after PAID) before the incident is frozen for a person to look.
            if flip:
                self._anomaly(request_id, flip, f"Airwallex changed a final status: {cur} -> {status}")
                if status == "PAID":
                    paid = self.s.q("SELECT request_id FROM payments WHERE incident_id=? AND status='PAID'", row["incident_id"])
                    if len(paid) > 1:
                        self._anomaly(request_id, "DOUBLE_PAID", "two transfers on this incident are PAID - ask the supplier to refund one")

    def _anomaly(self, request_id, code, detail):
        row = self.s.one("SELECT incident_id, anomaly FROM payments WHERE request_id=?", request_id)
        if row["anomaly"] == code:
            return  # already raised for this payment; don't re-alarm on every re-read
        with self.s.tx():
            self.s.db.execute("UPDATE payments SET anomaly=? WHERE request_id=?", (code, request_id))
        self.s.log("provider", "anomaly", "observed", row["incident_id"], request_id=request_id, code=code, detail=detail)
        self._alert(row["incident_id"], code)

    def _alert(self, incident_id, code):
        """Raise an alarm. A more serious alarm is never replaced by a milder one, and any alarm
        re-opens a RESOLVED incident (a return after close means the supplier is unpaid again)."""
        with self.s.tx():
            cur = self.s.one("SELECT alert FROM incidents WHERE id=?", incident_id)["alert"]
            if cur and ALERT_RANK.get(cur, 50) > ALERT_RANK.get(code, 50):
                self.s.log("policy", "incident_alert_kept", "decided", incident_id, kept=cur, ignored=code)
                return
            self.s.db.execute("UPDATE incidents SET alert=?, state='ESCALATED' WHERE id=?", (code, incident_id))
        self.s.log("policy", "incident_alert", "decided", incident_id, code=code)

    def scan_orphans(self, since=None):
        """Any transfer on the Airwallex account that this desk did not create is a red flag (leaked key,
        someone paying from the dashboard, a second system). Raise a desk-wide alarm and STOP."""
        since = since or float(self._meta("orphan_scan_since") or self.now())
        try:
            items = self.p.list_transfers_since(since)
        except (Ambiguous, ProviderError):
            return []
        ours = {r["request_id"] for r in self.s.q("SELECT request_id FROM payments")}
        seen = set(json.loads(self._meta("orphans_seen") or "[]"))
        new = [t for t in items if t.get("request_id") not in ours and t.get("id") not in seen]
        for t in new:
            self.s.log("provider", "orphan_transfer", "observed", None, transfer_id=t.get("id"), request_id=t.get("request_id"),
                       amount=t.get("transfer_amount"), currency=t.get("transfer_currency"), status=t.get("status"), created_at=t.get("created_at"))
        if new:
            self._meta("orphans_seen", json.dumps(sorted(seen | {t.get("id") for t in new})))
            self.kill("system", f"{len(new)} transfer(s) on the Airwallex account did not come from this desk")
        return new

    def _meta(self, k, v=None):
        if v is None:
            r = self.s.one("SELECT v FROM meta WHERE k=?", k)
            return r["v"] if r else None
        with self.s.lock:
            self.s.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))

    def sweep(self, stale_after=600, sla=72 * 3600, paid_watch=30 * 86400, cancel_watch=72 * 3600):
        """Background safety net: re-read anything uncertain or in flight that hasn't been checked
        lately, and raise an alert when a transfer sits in flight past the SLA."""
        out = []
        for p in self.s.q("SELECT * FROM payments WHERE status NOT IN ('PAID','CANCELLED')"):
            if self.now() - (p["checked_at"] or p["updated_at"]) >= stale_after:
                try:
                    out.append(self.reconcile(p["request_id"]))
                except Exception as e:  # one bad payment must not stop the safety net for the rest
                    self.s.log("system", "sweep_error", "decided", p["incident_id"], request_id=p["request_id"], error=str(e)[:200])
            if self.now() - p["created_at"] > sla and in_flight(self.payment(p["request_id"])["status"]):
                inc = self._incident(p["incident_id"])
                if inc["alert"] != "OVERDUE_SLA":
                    self.s.log("policy", "sla_breached", "decided", p["incident_id"], request_id=p["request_id"], hours=int((self.now() - p["created_at"]) / 3600))
                    self._alert(p["incident_id"], "OVERDUE_SLA")
        # CANCELLED is not proof either: a "cancelled" transfer can still pay out, and a payment we released
        # locally can turn up at Airwallex after all. Keep re-reading recent ones (found by the Watchdog).
        for p in self.s.q("SELECT * FROM payments WHERE status='CANCELLED' AND updated_at>?", self.now() - cancel_watch):
            if self.now() - (p["checked_at"] or p["updated_at"]) >= min(stale_after, 3600):
                try:
                    out.append(self.reconcile(p["request_id"]))
                except Exception as e:
                    self.s.log("system", "sweep_error", "decided", p["incident_id"], request_id=p["request_id"], error=str(e)[:200])
        # PAID is provisional: Airwallex can still move it to FAILED. Keep re-reading for a few days.
        for p in self.s.q("SELECT * FROM payments WHERE status='PAID' AND created_at>?", self.now() - paid_watch):
            if self.now() - (p["checked_at"] or p["updated_at"]) >= (3600 if self.now() - p["created_at"] < 86400 else 6 * 3600):
                try:
                    out.append(self.reconcile(p["request_id"]))
                except Exception as e:
                    self.s.log("system", "sweep_error", "decided", p["incident_id"], request_id=p["request_id"], error=str(e)[:200])
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


def _norm_invoice(x):
    """INV-0077, inv 77, #77 -> INV77. Letters kept, punctuation and leading zeros of numbers dropped."""
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", _norm_name(x).replace("NO", "").replace("NUMBER", ""))
