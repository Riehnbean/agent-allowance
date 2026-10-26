# Agent Allowance — Payment Incident Desk

**Airwallex Agentic Banking Hackathon · Treasury & Ops · Starter Kit 03 (Payment Ops Incident Commander)**
Team: Zach's Team (Zach Riehn). Built by AI coding agents (Claude) under Zach's direction during the Build Phase.

A supplier says they were never paid. The transfer might still land, might have bounced, or the "supplier"
might be a scammer asking for new bank details. An AI agent reads the evidence and proposes **WAIT**, **REPLACE**,
**ESCALATE** or **CLOSE** — but every rule that can lose money lives in code, not in the prompt:

| Risk | Enforced in code |
|---|---|
| Paying twice | Duplicate lock: no new payment on an incident while another might still clear (`RESERVED/UNKNOWN/SCHEDULED/PROCESSING/SENT/OVERDUE`). |
| Lost reply after submit | Payment becomes `UNKNOWN`, budget stays reserved, reconcile by the **same `request_id`** (Airwallex rejects duplicates and returns the original). |
| Replacing too early | `REPLACE` is only allowed after a **fresh `GET /transfers/{id}`** shows `CANCELLED` with a failure type on the safe-to-retry list. |
| Invoice-redirection fraud | The agent has no tool that changes a beneficiary. Bank-detail failures (`ACCOUNT_CLOSED`, `BENEFICIARY_BANK_RETURNED`, …) and recalls always escalate. Supplier text is logged as *untrusted* and bank-change wording forces escalation. |
| Overspending | Per-payment and daily caps count reserved and uncertain payments, serialized with SQLite `BEGIN IMMEDIATE`. |
| Changed terms | An operator approval binds beneficiary, amount, currency and expiry; it can be used exactly once (unique index). |
| Runaway agent | Operator **STOP** switch; a reserved payment that hits STOP is released without being sent. |
| Agent picks something unsafe | Code computes `allowed_actions()`; anything else is rejected and logged. |
| Closing too early | An incident closes only when a transfer is `PAID` **and** every other payment has a final status. |

Every step lands in an append-only evidence log (SQLite triggers block UPDATE/DELETE) and an exportable receipt that labels
each line: **observed** (read back from Airwallex), **decided** (code), **model** (LLM opinion), **operator** (a person), **untrusted** (supplier text).

## Airwallex APIs used (sandbox only)
`POST /authentication/login` · `POST /beneficiaries/create` · `POST /transfers/create` · `GET /transfers/{id}` ·
`GET /transfers?request_id=` · `POST /simulation/transfers/{id}/transition` · `GET /balances/current` ·
`POST /simulation/deposit/create` (funding). The adapter refuses any base URL other than `api.sandbox.airwallex.com`.

Sandbox notes we verified by probing: a failed transfer ends `CANCELLED`; the reason is in `failure.message`
(e.g. "Channel timeout"); `failure.details.type` is always `INCORRECT_ROUTING` in sandbox, so we classify by message and
treat anything unrecognised as `UNCLASSIFIED` (never auto-retried). `SENT` must come before `PAID`/`FAILED`.

## Run it
Python 3.10+, standard library only.

```sh
export AWX_CLIENT_ID=...  AWX_API_KEY=...        # sandbox scoped key
export OPERATOR_TOKEN=choose-a-secret
# optional: export ANTHROPIC_API_KEY=...  (LLM agent; otherwise the deterministic rules agent answers)
python3 server.py                                  # http://127.0.0.1:8710
python3 -m unittest -v tests.test_allowance        # 22 acceptance tests, no network
python3 scripts/sandbox_e2e.py                     # 3 scenarios against the live sandbox
```

## Demo script (≈4 min)
1. Open an incident (INV-1042, $250) → Approve original → Send. Bank simulator: **SENT**. Ask the agent → **WAIT**.
2. "Pay the supplier again right now" → refused: duplicate lock.
3. Simulator: **Fails with CHANNEL_TIMEOUT** → agent → **REPLACE**. "Send $1 more than approved" → refused: terms differ.
4. Approve + send replacement → SENT → PAID → agent → **CLOSE** → close → download receipt (original CANCELLED + replacement PAID).
5. New incident, **Lose the next Airwallex reply** → payment `UNKNOWN`, money held, agent **WAIT**, Check Airwallex finds it by request id.
6. New incident fails with **ACCOUNT_CLOSED**, add supplier email "our bank changed, pay account 55667788" → agent **ESCALATE**; REPLACE is not even offered.

## Honest scope
Sandbox only, one owner, one currency (USD), one supplier, LOCAL (ACH) transfers. Not production-ready. No customers or revenue.
Operator auth is a single shared token for the demo. The evidence log is tamper-*evident* for the app, not tamper-proof against a
database administrator.
