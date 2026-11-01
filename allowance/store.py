"""Durable state in SQLite. Every money decision is serialized with BEGIN IMMEDIATE,
so two workers (or two agent calls) can never both reserve the same budget."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy(
  id INTEGER PRIMARY KEY CHECK(id=1),
  currency TEXT NOT NULL,
  per_tx_cap INTEGER NOT NULL,
  daily_cap INTEGER NOT NULL,
  killed INTEGER NOT NULL DEFAULT 0,
  killed_reason TEXT,
  killed_by TEXT,
  per_supplier_daily_cap INTEGER NOT NULL DEFAULT 150000,
  max_payments_per_hour INTEGER NOT NULL DEFAULT 10,
  max_fee INTEGER NOT NULL DEFAULT 2500,
  changed_payee_cap INTEGER NOT NULL DEFAULT 10000,      -- first payments after a bank-detail change stay small
  changed_payee_window INTEGER NOT NULL DEFAULT 86400,
  max_attempts INTEGER NOT NULL DEFAULT 3,               -- original + at most 2 replacements (each failure burns a fee)
  max_incidents_per_hour INTEGER NOT NULL DEFAULT 30,
  allowed_countries TEXT NOT NULL DEFAULT 'US',
  dual_approval_over INTEGER NOT NULL DEFAULT 50000,
  fx_currencies TEXT NOT NULL DEFAULT '',               -- invoice currencies paid via a locked Airwallex FX quote (e.g. 'EUR')
  max_fx_spread_bps INTEGER NOT NULL DEFAULT 150         -- refuse a quote marked up more than 1.5% over the mid-market rate
);
CREATE TABLE IF NOT EXISTS beneficiaries(
  id TEXT PRIMARY KEY,            -- Airwallex beneficiary id
  name TEXT NOT NULL,
  bank_fingerprint TEXT NOT NULL, -- last4 + routing, shown to the operator
  approved_by TEXT NOT NULL,
  approved_at REAL NOT NULL,
  details_hash TEXT,             -- sha256 of the full bank details at approval time (drift check)
  account_key TEXT,              -- account+routing only, to spot one account reused under different names
  details_changed_at REAL        -- when re-verified details differed from the previous ones
);
CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  supplier TEXT NOT NULL,
  invoice TEXT NOT NULL,
  beneficiary_id TEXT NOT NULL REFERENCES beneficiaries(id),
  amount INTEGER NOT NULL,
  currency TEXT NOT NULL,
  supplier_deadline REAL,
  state TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN, ESCALATED, RESOLVED
  invoice_key TEXT,                     -- normalized supplier+invoice, for duplicate-invoice detection
  alert TEXT,                           -- latest anomaly that forced escalation
  reopened_at REAL,                     -- operator reopened after investigating (note in the log)
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_incident_per_invoice ON incidents(invoice_key);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  role TEXT NOT NULL,                   -- original | replacement
  beneficiary_id TEXT NOT NULL,
  amount INTEGER NOT NULL,
  currency TEXT NOT NULL,
  expires_at REAL NOT NULL,
  approved_by TEXT NOT NULL,
  used_by_request TEXT,
  cosigned_by TEXT,
  payee_hash TEXT,                      -- bank-details hash the approver saw; must still match at send time
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payments(
  request_id TEXT PRIMARY KEY,          -- idempotency key, assigned BEFORE the provider call
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  role TEXT NOT NULL,
  approval_id INTEGER NOT NULL REFERENCES approvals(id),
  beneficiary_id TEXT NOT NULL,
  amount INTEGER NOT NULL,
  currency TEXT NOT NULL,
  provider_id TEXT,
  status TEXT NOT NULL,                 -- RESERVED, UNKNOWN, SCHEDULED, PROCESSING, SENT, OVERDUE, PAID, CANCELLED
  failure_type TEXT,
  failure_message TEXT,
  status_source TEXT,                   -- provider_get | provider_create | none
  fee INTEGER NOT NULL DEFAULT 0,       -- provider fee in minor units, counted against budget
  anomaly TEXT,                         -- e.g. PAID_THEN_RETURNED, PAID_AFTER_CANCELLED
  checked_at REAL,                      -- last time we read it from Airwallex (updated_at = last CHANGE)
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_approval_use ON payments(approval_id);
-- Database-level duplicate lock: at most ONE not-final payment per incident, whatever the code above does.
CREATE UNIQUE INDEX IF NOT EXISTS one_live_payment_per_incident ON payments(incident_id) WHERE status NOT IN ('PAID','CANCELLED');
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  incident_id INTEGER,
  actor TEXT NOT NULL,                  -- operator | agent | provider | policy | system
  kind TEXT NOT NULL,
  provenance TEXT NOT NULL,             -- observed (from Airwallex) | decided (code) | model (LLM text) | operator | untrusted (supplier text)
  body TEXT NOT NULL,
  prev_hash TEXT,                       -- hash chain: editing or removing any past row breaks every later hash
  hash TEXT
);
CREATE TABLE IF NOT EXISTS pending_actions(   -- two-person controls: one asks, a DIFFERENT person confirms
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, target TEXT, note TEXT, requested_by TEXT NOT NULL, created_at REAL NOT NULL,
  done_by TEXT, done_at REAL
);
CREATE TABLE IF NOT EXISTS webhook_events(id TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
"""

# Statuses where money may still leave the account (or already has).
# Fail closed: ANY status other than the two final ones counts as "might still pay" - including
# statuses we have never seen (CREATED, IN_APPROVAL, FAILED-before-auto-cancel, new enums...).
FINAL = ("PAID", "CANCELLED")


def in_flight(status):
    return status not in FINAL


def counts_against_budget(status):
    return status != "CANCELLED"


# kept for readability in messages/tests
IN_FLIGHT = ("RESERVED", "UNKNOWN", "CREATED", "SCHEDULED", "OVERDUE", "PROCESSING", "SENT", "FAILED")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        for table, col in (("policy", "fx_currencies TEXT NOT NULL DEFAULT ''"), ("policy", "max_fx_spread_bps INTEGER NOT NULL DEFAULT 150"),
                           ("payments", "cost_amount INTEGER"), ("payments", "fx_quote_id TEXT"), ("payments", "fx_rate REAL"),
                           ("payments", "fx_expires_at REAL")):
            try:  # older databases: add the FX columns in place
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass

    # transactions -----------------------------------------------------------
    def tx(self):
        return _Tx(self)

    # Reads share the one connection with other threads' transactions, so they take the same lock
    # (a read interleaved with another thread's statements on one sqlite3 connection can come back wrong).
    def q(self, sql, *a):
        with self.lock:
            return self.db.execute(sql, a).fetchall()

    def one(self, sql, *a):
        with self.lock:
            return self.db.execute(sql, a).fetchone()

    def log(self, actor, kind, provenance, incident_id=None, _guard=None, **body):
        """_guard: optional check run under the same lock, immediately before the append. It raises
        to stop the write, so "check the facts, then record" cannot be split by another thread."""
        with self.lock:
            if _guard is not None:
                _guard()
            self._log(actor, kind, provenance, incident_id, **body)

    def _log(self, actor, kind, provenance, incident_id=None, **body):
        own = not self.db.in_transaction
        if own:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            prev = self.db.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            prev = prev["hash"] if prev and prev["hash"] else "GENESIS"
            at, b = time.time(), json.dumps(body, default=str, sort_keys=True)
            h = _chain(prev, at, incident_id, actor, kind, provenance, b)
            self.db.execute(
                "INSERT INTO events(at,incident_id,actor,kind,provenance,body,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?)",
                (at, incident_id, actor, kind, provenance, b, prev, h),
            )
            if own:
                self.db.execute("COMMIT")
        except Exception:
            if own:
                self.db.execute("ROLLBACK")
            raise

    def verify_chain(self):
        """Recompute every hash. Returns (ok, rows_checked, first_bad_id)."""
        prev, n = "GENESIS", 0
        for e in self.db.execute("SELECT * FROM events ORDER BY id"):
            n += 1
            if e["prev_hash"] != prev or e["hash"] != _chain(prev, e["at"], e["incident_id"], e["actor"], e["kind"], e["provenance"], e["body"]):
                return False, n, e["id"]
            prev = e["hash"]
        return True, n, None

    # policy ----------------------------------------------------------------
    def init_policy(self, currency="USD", per_tx_cap=100000, daily_cap=200000, **extra):
        self.db.execute(
            "INSERT OR IGNORE INTO policy(id,currency,per_tx_cap,daily_cap) VALUES(1,?,?,?)",
            (currency, per_tx_cap, daily_cap),
        )
        for k, v in extra.items():
            if k in ("per_supplier_daily_cap", "max_payments_per_hour", "max_fee", "dual_approval_over", "changed_payee_cap", "changed_payee_window", "max_attempts", "max_incidents_per_hour", "allowed_countries", "fx_currencies", "max_fx_spread_bps"):
                self.db.execute(f"UPDATE policy SET {k}=? WHERE id=1", (v,))

    def committed_today_for(self, beneficiary_id, now=None):
        now = now or time.time()
        day0 = now - 86400  # rolling 24 hours (a calendar-day reset at UTC midnight = 7pm Central)
        r = self.one(
            # per BANK ACCOUNT, not per payee record - two records for one account share one cap
            "SELECT COALESCE(SUM(" + _COST.replace("{p}", "p.") + "),0) s FROM payments p JOIN beneficiaries b ON b.id=p.beneficiary_id"
            " WHERE b.account_key=(SELECT account_key FROM beneficiaries WHERE id=?)"
            " AND (p.created_at>=? OR p.status NOT IN ('PAID','CANCELLED'))",
            beneficiary_id, day0,
        )
        return r["s"]

    def policy(self):
        return self.one("SELECT * FROM policy WHERE id=1")

    def validate_policy(self):
        """Refuse to run with limits that make no sense (MON-10)."""
        p = self.policy()
        problems = []
        if not (0 < p["per_tx_cap"] <= p["daily_cap"]):
            problems.append("per-payment cap must be positive and not above the daily cap")
        if not (0 < p["per_supplier_daily_cap"] <= p["daily_cap"]):
            problems.append("per-payee cap must be positive and not above the daily cap")
        if p["dual_approval_over"] > p["per_tx_cap"]:
            problems.append("co-sign threshold above the per-payment cap means co-sign never applies")
        if not (1 <= p["max_attempts"] <= 5):
            problems.append("max attempts must be 1-5")
        if problems:
            raise ValueError("unsafe policy: " + "; ".join(problems))
        return True

    def committed_today(self, now=None):
        """Every payment that might cost money, counted against today's cap -
        including reserved and uncertain ones, whatever day they started on."""
        now = now or time.time()
        day0 = now - 86400  # rolling 24 hours (a calendar-day reset at UTC midnight = 7pm Central)
        r = self.one(
            "SELECT COALESCE(SUM(" + _COST.replace("{p}", "") + "),0) s FROM payments"
            " WHERE (created_at>=? OR status NOT IN ('PAID','CANCELLED'))",
            day0,
        )
        return r["s"]


# What a payment can cost against the caps:
#   PAID       -> amount + the fee Airwallex reported
#   CANCELLED  -> the fee only (a failed transfer's fee is not refunded; locally cancelled ones have fee 0)
#   anything else (reserved / uncertain / in flight) -> amount + the larger of the reported fee and the
#   policy's max_fee, because the real fee isn't known until Airwallex says so.
# Cost is always in the policy (home) currency: a foreign-currency payment counts its locked FX cost, not its face value.
_COST = ("CASE WHEN {p}status='CANCELLED' THEN {p}fee WHEN {p}status='PAID' THEN COALESCE({p}cost_amount,{p}amount)+{p}fee"
         " ELSE COALESCE({p}cost_amount,{p}amount)+MAX({p}fee,(SELECT max_fee FROM policy WHERE id=1)) END")


def _chain(prev, at, incident_id, actor, kind, provenance, body):
    import hashlib
    return hashlib.sha256("|".join(map(str, (prev, repr(at), incident_id, actor, kind, provenance, body))).encode()).hexdigest()


class _Tx:
    """One connection is shared by the server threads and the sweeper, so every transaction (and
    every log write) holds a process-wide re-entrant lock. Nested tx() in the same thread joins the
    outer transaction instead of failing."""
    def __init__(self, store):
        self.st = store

    def __enter__(self):
        self.st.lock.acquire()
        self.outer = not self.st.db.in_transaction
        if self.outer:
            self.st.db.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, et, ev, tb):
        try:
            if self.outer:
                self.st.db.execute("ROLLBACK" if et else "COMMIT")
        finally:
            self.st.lock.release()
        return False
