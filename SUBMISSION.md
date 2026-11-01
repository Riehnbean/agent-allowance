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

The model never touches money. Code first works out which actions are allowed. The model can only pick from that list, and anything else is rejected and logged. Each money rule below is enforced in code and has a named test; `COVERAGE.md` lists what is tested, what is partial and what is out of scope:

- **Double payment is blocked.** A replacement is blocked while any payment on the incident might still clear. A lost Airwallex reply becomes `UNKNOWN`, the budget stays held, and the desk reconciles using the *same* `request_id`.
- **No early replacement.** REPLACE needs a fresh `GET /transfers/{id}` at the moment of sending, showing the original CANCELLED for a retry-safe reason.
- **Redirected payments are blocked.** The agent has no tool that changes a beneficiary. Supplier text is labelled *untrusted*, and bank-change wording forces ESCALATE. A payee edited inside Airwallex is caught by a details hash, and the desk stops.
- **Caps hold.** Per-payment, per-payee and daily caps count reserved and uncertain payments, including possible fees. Writes are serialized with SQLite `BEGIN IMMEDIATE`.
- **People stay in charge.** Each approval is bound to payee, amount, currency, bank details and expiry, and can be used once. Large or split payments need a second named person. A STOP switch halts everything, and a system STOP needs two people to clear.
- **Webhook claims are not applied.** A webhook only triggers a fresh read from Airwallex. Its claimed status is logged and never applied.

Every step goes into an append-only, hash-chained evidence log. Each incident exports a receipt that labels every line by source: *observed* (read back from Airwallex), *decided* (code), *model* (AI opinion), *operator* (a person) or *untrusted* (supplier text).

## Two AIs and a rulebook
The demo desk runs this chain:
1. **Claude** (Sonnet 5.5, using the hackathon's Claude API credits) recommends an action.
2. **A second, open-source AI checks it.** That's GPT-OSS 20B through a free hosted endpoint, or Qwen2.5-3B running on the machine. If the two AIs disagree, the desk takes the most careful allowed choice (ESCALATE, then WAIT, then CLOSE, then REPLACE) and shows both answers.
3. If no AI answers, a deterministic rulebook does.

The desk also runs with **no paid API at all**: open-source models only, then the rulebook. That's one setting.

Account numbers, IBANs and emails are stripped from supplier text before *any* model sees it, including the disguises our reviewer tried: spelled out, spaced, dashed, dotted, lower-case, HTML entities, invisible characters, non-Latin digits and base64 (a best-effort text filter, tested in `tests/test_nov1_review.py` and `tests/test_nov1_verify.py`, not a guarantee against every possible encoding). The code limits what the agent may do, so a weaker or tricked model can't create an unsafe action. It can only pick a worse *safe* one, and the operator sees which brain answered. Payments are already protected by code and human approval, so the double check only makes the *advice* safer.

So the operator never waits on the model, the desk asks the AI as soon as an incident changes ("think-ahead"). The model wait is capped at 6 seconds (the two AIs think in parallel, usually 3–4 seconds), and if they are not done, the rulebook answers. The re-read of in-flight payments from Airwallex that happens before the models are asked has its own 30-second network timeout and is not inside that cap.

## Airwallex APIs used (sandbox)
`POST /authentication/login` · `POST /beneficiaries/create` · `POST /transfers/create` · `GET /transfers/{id}` · `GET /transfers?request_id=` · `GET /balances/current` · `POST /simulation/transfers/{id}/transition` · `POST /simulation/deposit/create`

Things we checked by probing the sandbox:
- A failed transfer ends `CANCELLED`, and the reason is in `failure.message`.
- `failure.details.type` is always `INCORRECT_ROUTING` in sandbox, so we classify by message. Anything we don't recognize is never auto-retried.
- `SENT` must come before `PAID` or `FAILED`.

## Evidence
- **123 automated offline test runs** (no network; the six live-sandbox scenarios in `scripts/sandbox_e2e.py` are separate and need sandbox keys). They include red-team suites written by an independent AI reviewer (OpenAI Codex): 20 attack tests, 9 holes found on Oct 27, and two more review rounds on Nov 1 (a replacement payment allowed after a failed or empty fresh read, disguised bank details, stale advice, an off-by-one approval expiry, and out-of-order screen updates). Each was shipped as a failing test and then fixed.
- **`scripts/sandbox_e2e.py`** runs 6 scenarios against the live Airwallex sandbox: normal pay, lost reply, channel-timeout replacement, closed account, payee edited in Airwallex, and same-payee totals needing a co-signer. All 6 pass.
- **`scripts/llm_eval.py`** runs the real models on 9 tricky incidents, including a scam email and a prompt-injection attempt. Claude (Sonnet 5.5, via the hackathon's Claude API credits) got 9/9 at 2.0 seconds average. With the open-source GPT-OSS 20B configured as the parallel cross-checker, the final answers were still 9/9 at 2.0 seconds average (Nov 1 run; the receipt records the final answer per case, not each model's separate vote). The small offline backup (Qwen2.5-3B on a 2-core CPU) got 7/9 on its own (Oct 27 run, 35 seconds average), and both of its misses were the cautious choice, so no money moved. In the cases we tested, a weak or tricked model cost time, not money; the money checks are code, and the tests in `tests/` are the list of what that code actually covers. The raw, dated output of each eval run is in `receipts/`.
- **`COVERAGE.md`** maps 128 researched payment failure routes to how the desk handles each: 95 built and tested, 19 partial, 14 out of scope with reasons. Sources include Airwallex docs, Nacha, FBI IC3, idempotency guidance and the OWASP LLM Top 10.

## Honest scope
- Sandbox only. It covers one owner, USD, one supplier and LOCAL (ACH) transfers.
- Operator login is a demo token. The evidence log is tamper-*evident* inside the app, not tamper-proof against a database admin.
- It is not production-ready, and it has no customers or revenue.
- What it shows is a pattern: **let an AI agent help with payment incidents while every path that moves money stays in tested code the agent cannot call.**

## Run it
See `README.md`. It needs Python 3.10+ and the standard library only. Sandbox keys go in environment variables and are never committed.
