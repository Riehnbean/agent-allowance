# Agent Allowance: Payment Incident Desk

**Airwallex Agentic Banking Hackathon · Treasury & Ops · Starter Kit 03 (Payment Ops Incident Commander)**
**Builder:** Zach Riehn (Zach's Team)

## How this was built
I directed the project. AI coding agents wrote the code, tests and docs. Claude did the main build, and a second AI (OpenAI Codex) reviewed it independently and acted as the attacker. I chose the problem and the safety rules, reviewed the demos and decided what shipped. Every claim in this document can be checked with the tests and scripts in the repo.

## The problem
A supplier emails: *"We never got paid."* An ops person now has to decide fast, and every choice can lose money:
- **Pay again**, and the first transfer may still land, so the supplier is paid twice.
- **Wait**, and the money may have bounced days ago, so a real supplier goes unpaid.
- **Trust the email**, and it may be from a scammer, so the money goes to the wrong account. This is invoice-redirection fraud, the most common business email compromise loss.

An AI agent can help read the situation. But a plain "AI that can send payments" is the wrong design, because one bad prompt or one confused model can lose money.

## The idea: the AI advises, the code holds the money
Agent Allowance is an incident desk on top of the Airwallex Transfers API. For each incident, the AI agent reads the evidence and recommends one of four actions:

| Action | When |
|---|---|
| **WAIT** | The original transfer could still land. |
| **REPLACE** | Airwallex confirms the original failed for a reason that is safe to retry. |
| **ESCALATE** | Bank details look wrong, fraud signals appear, or anything is unclear. |
| **CLOSE** | The money is confirmed PAID and nothing else is in flight. |

The model never touches money. Code first works out which actions are allowed. The model can only pick from that list, and anything else is rejected and logged. Every rule that could lose money is enforced in code and covered by a test:

- **No double payment.** A replacement is blocked while any payment on the incident might still clear. A lost Airwallex reply becomes `UNKNOWN`, the budget stays held, and the desk reconciles using the *same* `request_id`.
- **No early replacement.** REPLACE needs a fresh `GET /transfers/{id}` at the moment of sending, showing the original CANCELLED for a retry-safe reason.
- **No redirected payments.** The agent has no tool that changes a beneficiary. Supplier text is labelled *untrusted*, and bank-change wording forces ESCALATE. A payee edited inside Airwallex is caught by a details hash, and the desk stops.
- **No overspending.** Per-payment, per-payee and daily caps count reserved and uncertain payments, including possible fees. Writes are serialized with SQLite `BEGIN IMMEDIATE`.
- **People stay in charge.** Each approval is bound to payee, amount, currency, bank details and expiry, and can be used once. Large or split payments need a second named person. A STOP switch halts everything, and a system STOP needs two people to clear.
- **Webhooks are never trusted.** A webhook only triggers a fresh read from Airwallex. Its claimed status is logged and never applied.

Every step goes into an append-only, hash-chained evidence log. Each incident exports a receipt that labels every line by source: *observed* (read back from Airwallex), *decided* (code), *model* (AI opinion), *operator* (a person) or *untrusted* (supplier text).

## Open-source AI, no paid API
The agent's brain is free and open-weights:
1. GPT-OSS 20B through a free hosted endpoint, with no signup.
2. If that's unavailable, Qwen2.5-3B running locally (llama.cpp).
3. If both are unavailable, a deterministic rulebook.

Account numbers, IBANs and emails are removed from supplier text before *any* model sees it. Because the code limits what the agent may do, a weaker or tricked model can't create an unsafe action. It can only pick a worse *safe* one, and the operator sees which brain answered.

**Double check (optional):** Claude can answer first, with an open-source model checking each pick on its own. If the two AIs disagree, the desk takes the most careful allowed choice and shows both answers. Payments are already protected by code and human approval, so this only makes the *advice* safer.

So the operator never waits on the model, the desk asks the AI as soon as an incident changes ("think-ahead"). A click returns within 4 seconds, and if the model isn't done, the rulebook answers.

## Airwallex APIs used (sandbox)
`POST /authentication/login` · `POST /beneficiaries/create` · `POST /transfers/create` · `GET /transfers/{id}` · `GET /transfers?request_id=` · `GET /balances/current` · `POST /simulation/transfers/{id}/transition` · `POST /simulation/deposit/create`

Things we checked by probing the sandbox:
- A failed transfer ends `CANCELLED`, and the reason is in `failure.message`.
- `failure.details.type` is always `INCORRECT_ROUTING` in sandbox, so we classify by message. Anything we don't recognize is never auto-retried.
- `SENT` must come before `PAID` or `FAILED`.

## Evidence
- **101 automated tests, no network.** They include two red-team suites written by independent AI reviewers: 20 attack tests, plus 9 holes found on Oct 27, each shipped as a failing test and then fixed.
- **`scripts/sandbox_e2e.py`** runs 6 scenarios against the live Airwallex sandbox: normal pay, lost reply, channel-timeout replacement, closed account, payee edited in Airwallex, and same-payee totals needing a co-signer. All 6 pass.
- **`scripts/llm_eval.py`** runs the real model on 9 tricky incidents, including a scam email and a prompt-injection attempt, and compares it to the rulebook. The small offline backup (Qwen2.5-3B on a 2-core CPU) picked the right action on 7 of 9, including the scam and the prompt injection. Both misses were *more cautious* than needed (ESCALATE instead of REPLACE, WAIT instead of ESCALATE), so no money moved. That is the point of the design: a weak model costs time, never money.
- **`COVERAGE.md`** maps 128 researched payment failure routes to how the desk handles each: 95 built and tested, 19 partial, 14 out of scope with reasons. Sources include Airwallex docs, Nacha, FBI IC3, idempotency guidance and the OWASP LLM Top 10.

## Honest scope
- Sandbox only. It covers one owner, USD, one supplier and LOCAL (ACH) transfers.
- Operator login is a demo token. The evidence log is tamper-*evident* inside the app, not tamper-proof against a database admin.
- It is not production-ready, and it has no customers or revenue.
- What it shows is a pattern: **let an AI agent help with payment incidents without ever giving it a way to lose money.**

## Run it
See `README.md`. It needs Python 3.10+ and the standard library only. Sandbox keys go in environment variables and are never committed.
