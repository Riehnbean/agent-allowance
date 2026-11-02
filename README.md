# Agent Allowance — Payment Incident Desk

### ▶ [Try the live demo - opens in about 5 seconds, nothing to install](https://riehnbean.github.io/agent-allowance/demo/)

The real app code runs right in your browser (via Pyodide), with a short guided tour. Airwallex is simulated there, so nothing real can be paid.

Prefer a full dev environment? [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Riehnbean/agent-allowance?quickstart=1) - wait about a minute and the desk opens in a new tab. Sign in with token `zach` (a second operator is `jordan`). It runs on an offline stand-in for the Airwallex sandbox (no keys, nothing real moves) with a free open-source AI (GPT-OSS via Pollinations) giving the advice.


**Airwallex Agentic Banking Hackathon · Treasury & Ops · Starter Kit 03 (Payment Ops Incident Commander)**

**Why it matters.** Business email compromise - fake "our bank changed" and "we were never paid" emails - cost US businesses **$3.05 billion in 2025** ([FBI IC3 2025 report](https://www.mcdonaldhopkins.com/insights/news/the-sobering-truth-of-the-fbis-2025-internet-crime-complaint-center-report)), and **63%** of organizations were hit by attempted or actual BEC fraud, with vendor-impersonation fraud up to 45% ([AFP 2025 Payments Fraud Survey](https://www.financialprofessionals.org/about/learn-more/press-releases/Details/survey-79-percent-of-organizations-were-victims-of-attempted-or-actual-payments-fraud-activity-in-2024)).

**Who it's for.** Accounts-payable teams at growing companies (roughly 20-500 people) that pay suppliers through Airwallex, often across currencies, without a fraud team.

**How it differs.** AP platforms and payee-verification tools (Bill.com, Ramp, Trustpair, Eftsure) screen payments *before* they go out: duplicate checks, approval rules, bank-detail verification. Agent Allowance works *after* a payment has gone wrong - failed, partly paid, or disputed by email - using the live Airwallex transfer status to decide whether it is safe to send again, a likely scam, or short-paid. The AI only recommends; code and people hold the money.

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


## Try it in one minute (no keys, nothing leaves your machine)

```
python3 demo.py
```
Open http://127.0.0.1:8710 and sign in with token `zach` (a second browser as `jordan` to try co-signing). Four seeded incidents: a failed transfer to replace, a scam "new bank details" email the desk escalates, a "we only received $280" reply that needs judgment, and a EUR invoice paid through a locked FX quote. Set `ANTHROPIC_API_KEY` to have Claude answer, or `AGENT_PROVIDER=pollinations` for the free open-source model; otherwise the rulebook does.

## Airwallex APIs used (sandbox only)
`POST /authentication/login` · `POST /beneficiaries/create` · `GET /beneficiaries/{id}` · `POST /beneficiaries/verify_account` (account check before a SEPA/UK payee is allowed) ·
`POST /fx/quotes/create` (locked rate) · `POST /transfers/create` (with `quote_id`) · `GET /transfers/{id}` · `GET /transfers?request_id=` · `GET /transfers?from_created_at=` ·
`GET /balances/current` · `POST /simulation/transfers/{id}/transition` · `POST /simulation/deposit/create` (funding) · webhooks (only ever trigger a fresh read). The adapter refuses any base URL other than `api.sandbox.airwallex.com`.

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
python3 -W ignore::ResourceWarning -m unittest -b tests.test_allowance tests.test_break tests.test_break_server tests.test_codex_review tests.test_nov1_review tests.test_nov1_verify tests.test_indep tests.test_r3_provider tests.test_r3_normalizer tests.test_r3_concurrency tests.test_watchdog tests.test_fx tests.test_wd_approve tests.test_verify_payee tests.test_r4_review tests.test_r5_review  # 181 test runs, no network
python3 -m watchdog.cycle --minutes 3   # Watchdog: attack, auto-repair behind gates (needs ANTHROPIC_API_KEY to repair), report
python3 scripts/sandbox_e2e.py                     # scenarios against the live sandbox
AGENT_PROVIDER=local python3 scripts/llm_eval.py   # the real model on 9 tricky incidents (scam, prompt injection...)
```

## Demo script (≈3 min, same as the guided tour and the video)
1. **INV-1042** (first payment failed with a bank timeout) → **Ask the AI** → "Safe to send it again".
2. **Approve replacement** → it's over $200, so **switch to Jordan** → **Co-sign** → switch back → **Send replacement**. Send then greys out: the approval is used up.
3. **INV-1043** (supplier asks to pay a new bank account) → payments already frozen by code → **Ask the AI** → "Get a person to check". There is no pay button.
4. **INV-1044** (supplier says $280 of $300 arrived) → the desk shows the $20 gap; the AI says get a person, and a full re-send is blocked.
5. **EU-2201** (EUR 250) → the dollar cost at Airwallex's rate shows before you approve; it needs a co-sign; Send locks the rate.
6. **STOP** → nothing can be paid until someone resumes it with a written reason. **Download receipt** → log intact.

Testers: **Advanced (testers)** shows a bank simulator and "Try to break it" buttons (pay twice, send $1 more, lose Airwallex's reply, change the payee's bank account inside Airwallex).

## Independent reviews
Two separate AI reviewers attacked this build. The first wrote 20 red-team tests (`tests/test_break*.py`); the second (Oct 27)
found 9 more holes, each delivered as a failing test (`tests/test_codex_review.py`) and now fixed:
possible fees are held against the budget while a payment is uncertain, a failed transfer's fee still counts, a replacement
re-reads the original from Airwallex at the moment it is sent, a PAID payment that comes back as failed is no longer shown as paid,
a provider record that doesn't match our transfer is refused (PROVIDER_MISMATCH), an operator re-send needs an unexpired approval
(an expired hold can be released with a note), pressing STOP can't lower a system STOP's two-person rule, a two-person
"accept new bank details" request is bound to the exact details both people saw, and unknown webhooks are dropped before anything is stored.

## Honest scope
Sandbox only, one owner, USD home currency, a small supplier allowlist (US ACH, plus EUR via a locked Airwallex FX quote). Not production-ready. No customers or revenue.
Sign-in is a demo token per operator (`OPERATOR_TOKENS`), with no SSO. The evidence log is tamper-*evident* for the app, not tamper-proof against a
database administrator.
