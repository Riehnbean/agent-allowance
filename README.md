# Agent Allowance — Payment Incident Desk

**Airwallex Agentic Banking Hackathon · Treasury & Ops · Starter Kit 03 (Payment Ops Incident Commander)**
Team: Zach's Team (Zach Riehn). Built by AI coding agents (Claude) under Zach's direction during the Build Phase.

A supplier says they were never paid. The transfer might still land, might have bounced, or the "supplier"
might be a scammer asking for new bank details. An AI agent reads the evidence and proposes **WAIT**, **REPLACE**,
**ESCALATE** or **CLOSE** — but the money rules below live in code, not in the prompt (each has a test; `COVERAGE.md` lists what is and is not covered):

| Risk | Enforced in code |
|---|---|
| Paying twice | Duplicate lock: no new payment on an incident while another might still clear (`RESERVED/UNKNOWN/SCHEDULED/PROCESSING/SENT/OVERDUE`). |
| Lost reply after submit | Payment becomes `UNKNOWN`, budget stays reserved, reconcile by the **same `request_id`** (Airwallex rejects duplicates and returns the original). |
| Replacing too early | `REPLACE` is only allowed after a **fresh `GET /transfers/{id}`** shows `CANCELLED` with a failure type on the safe-to-retry list. |
| Invoice-redirection fraud | The agent has no tool that changes a beneficiary. Bank-detail failures (`ACCOUNT_CLOSED`, `BENEFICIARY_BANK_RETURNED`, …) and recalls always escalate. Supplier text is logged as *untrusted*; bank-change wording it recognises (a best-effort filter, see `tests/test_nov1_*` and `tests/test_indep.py`) forces escalation. |
| Overspending | Per-payment and daily caps count reserved and uncertain payments, serialized with SQLite `BEGIN IMMEDIATE`. |
| Changed terms | An operator approval binds beneficiary, amount, currency and expiry; it can be used exactly once (unique index). |
| Runaway agent | Operator **STOP** switch; a reserved payment that hits STOP is released without being sent. |
| Agent picks something unsafe | Code computes `allowed_actions()`; anything else is rejected and logged. |
| Forged / duplicate / late webhooks | A webhook only triggers a fresh read from Airwallex (throttled); its claimed status is logged as *untrusted* and never applied. A PAID or CANCELLED state changes only on a fresh read from Airwallex, and such reversals are logged and alarmed. |
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
export OPERATOR_TOKEN=choose-a-secret   # single-operator demo: two-person rules (co-sign, system-STOP resume) cannot apply with one person;
# for those, use OPERATOR_TOKENS="zach:secret1,jordan:secret2"
# AI brain (free, open-source). The local model runs on this machine; the default chain asks a free hosted
# open-weights model (GPT-OSS 20B via Pollinations) first. Account/routing numbers, IBANs and emails are stripped
# from supplier text before ANY model sees it; for a strictly local setup use AGENT_PROVIDER=local.
#   pip install 'llama-cpp-python[server]'
#   curl -L -o qwen.gguf https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf
#   python3 -m llama_cpp.server --model qwen.gguf --model_alias qwen2.5-3b-instruct --port 8711 &
export AGENT_PROVIDER=pollinations,local   # tried in order: free GPT-OSS 20B (no signup), then the local model, then the rulebook
#   other options: ollama | groq (GROQ_API_KEY) | gemini (GEMINI_API_KEY) | anthropic (ANTHROPIC_API_KEY)
# Double check: with AGENT_PROVIDER=anthropic,pollinations,local and AGENT_CROSSCHECK=1, Claude answers first and an
# open-source model checks it independently. If they disagree, the desk takes the most careful allowed choice
# (ESCALATE > WAIT > CLOSE > REPLACE) and says so. Either way the AI only recommends - it has no tool that moves money.
# Think-ahead: the desk asks the AI as soon as an incident changes, so the operator's click is instant;
# a click never waits more than AGENT_CLICK_BUDGET (default 6s) - if the AI isn't done, the rulebook answers.
# With no model running, the deterministic rules agent answers automatically.
python3 server.py                                  # http://127.0.0.1:8710
python3 -m unittest tests.test_allowance tests.test_break tests.test_break_server tests.test_codex_review tests.test_nov1_review tests.test_nov1_verify tests.test_indep tests.test_r3_provider tests.test_r3_normalizer tests.test_r3_concurrency tests.test_watchdog  # 150 test runs, no network
python3 -m watchdog.cycle --minutes 3   # Watchdog: attack, auto-repair behind gates (needs ANTHROPIC_API_KEY to repair), report
python3 scripts/sandbox_e2e.py                     # scenarios against the live sandbox
AGENT_PROVIDER=local python3 scripts/llm_eval.py   # the real model on 9 tricky incidents (scam, prompt injection...)
```

## Demo script (≈4 min)
1. Open an incident (INV-1042, $250) → Approve original → Send. Bank simulator: **SENT**. Ask the agent → **WAIT**.
2. "Pay the supplier again right now" → refused: duplicate lock.
3. Simulator: **Fails with CHANNEL_TIMEOUT** → agent → **REPLACE**. "Send $1 more than approved" → refused: terms differ.
4. Approve + send replacement → SENT → PAID → agent → **CLOSE** → close → download receipt (original CANCELLED + replacement PAID).
5. New incident, **Lose the next Airwallex reply** → payment `UNKNOWN`, money held, agent **WAIT**, Check Airwallex finds it by request id.
6. New incident fails with **ACCOUNT_CLOSED**, add supplier email "our bank changed, pay account 55667788" → agent **ESCALATE**; REPLACE is not even offered.

## Independent reviews
Two separate AI reviewers attacked this build. The first wrote 20 red-team tests (`tests/test_break*.py`); the second (Oct 27)
found 9 more holes, each delivered as a failing test (`tests/test_codex_review.py`) and now fixed:
possible fees are held against the budget while a payment is uncertain, a failed transfer's fee still counts, a replacement
re-reads the original from Airwallex at the moment it is sent, a PAID payment that comes back as failed is no longer shown as paid,
a provider record that doesn't match our transfer is refused (PROVIDER_MISMATCH), an operator re-send needs an unexpired approval
(an expired hold can be released with a note), pressing STOP can't lower a system STOP's two-person rule, a two-person
"accept new bank details" request is bound to the exact details both people saw, and unknown webhooks are dropped before anything is stored.

## Honest scope
Sandbox only, one owner, one currency (USD), one supplier, LOCAL (ACH) transfers. Not production-ready. No customers or revenue.
Operator auth is a single shared token for the demo. The evidence log is tamper-*evident* for the app, not tamper-proof against a
database administrator.
