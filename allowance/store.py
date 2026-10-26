"""Durable state in SQLite. Every money decision is serialized with BEGIN IMMEDIATE,
so two workers (or two agent calls) can never both reserve the same budget."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy(
  id INTEGER PRIMARY KEY CHECK(id=1),
  currency TEXT NOT NULL,
  per_tx_cap INTEGER NOT NULL,
  daily_cap INTEGER NOT NULL,
  killed INTEGER NOT NULL DEFAULT 0,
  killed_reason TEXT,
  per_supplier_daily_cap INTEGER NOT NULL DEFAULT 150000,
  max_payments_per_hour INTEGER NOT NULL DEFAULT 10,
  max_fee INTEGER NOT NULL DEFAULT 2500
);
CREATE TABLE IF NOT EXISTS beneficiaries(
  id TEXT PRIMARY KEY,            -- Airwallex beneficiary id
  name TEXT NOT NULL,
  bank_fingerprint TEXT NOT NULL, -- last4 + routing, shown to the operator
  approved_by TEXT NOT NULL,
  approved_at REAL NOT NULL,
  details_hash TEXT,             -- sha256 of the full bank details at approval time (drift check)
  account_key TEXT               -- account+routing only, to spot one account reused under different names
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
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_approval_use ON payments(approval_id);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  incident_id INTEGER,
  actor TEXT NOT NULL,                  -- operator | agent | provider | policy | system
  kind TEXT NOT NULL,
  provenance TEXT NOT NULL,             -- observed (from Airwallex) | decided (code) | model (LLM text) | operator | untrusted (supplier text)
  body TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
"""

# Statuses where money may still leave the account (or already has).
LIVE = ("RESERVED", "UNKNOWN", "SCHEDULED", "PROCESSING", "SENT", "OVERDUE", "PAID")
IN_FLIGHT = ("RESERVED", "UNKNOWN", "SCHEDULED", "PROCESSING", "SENT", "OVERDUE")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    # transactions -----------------------------------------------------------
    def tx(self):
        return _Tx(self.db)

    def q(self, sql, *a):
        return self.db.execute(sql, a).fetchall()

    def one(self, sql, *a):
        return self.db.execute(sql, a).fetchone()

    def log(self, actor, kind, provenance, incident_id=None, **body):
        self.db.execute(
            "INSERT INTO events(at,incident_id,actor,kind,provenance,body) VALUES(?,?,?,?,?,?)",
            (time.time(), incident_id, actor, kind, provenance, json.dumps(body, default=str)),
        )

    # policy ----------------------------------------------------------------
    def init_policy(self, currency="USD", per_tx_cap=100000, daily_cap=200000, **extra):
        self.db.execute(
            "INSERT OR IGNORE INTO policy(id,currency,per_tx_cap,daily_cap) VALUES(1,?,?,?)",
            (currency, per_tx_cap, daily_cap),
        )
        for k, v in extra.items():
            if k in ("per_supplier_daily_cap", "max_payments_per_hour", "max_fee"):
                self.db.execute(f"UPDATE policy SET {k}=? WHERE id=1", (v,))

    def committed_today_for(self, beneficiary_id, now=None):
        now = now or time.time()
        day0 = now - (now % 86400)
        r = self.one(
            f"SELECT COALESCE(SUM(amount+fee),0) s FROM payments WHERE beneficiary_id=? AND status IN ({','.join('?'*len(LIVE))})"
            f" AND (created_at>=? OR status IN ({','.join('?'*len(IN_FLIGHT))}))",
            beneficiary_id, *LIVE, day0, *IN_FLIGHT,
        )
        return r["s"]

    def policy(self):
        return self.one("SELECT * FROM policy WHERE id=1")

    def committed_today(self, now=None):
        """Every payment that might cost money, counted against today's cap -
        including reserved and uncertain ones, whatever day they started on."""
        now = now or time.time()
        day0 = now - (now % 86400)
        r = self.one(
            f"SELECT COALESCE(SUM(amount+fee),0) s FROM payments WHERE status IN ({','.join('?'*len(LIVE))})"
            f" AND (created_at>=? OR status IN ({','.join('?'*len(IN_FLIGHT))}))",
            *LIVE, day0, *IN_FLIGHT,
        )
        return r["s"]


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, et, ev, tb):
        self.db.execute("ROLLBACK" if et else "COMMIT")
        return False
