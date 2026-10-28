# Demo video script: Agent Allowance (target 4:15, hard limit 5:00)

Narration is in Zach's own voice (cloned with Chatterbox from his recording, using the locked settings). Screen: the desk at http://127.0.0.1:8710 against the Airwallex sandbox.
Each block is about 25 to 40 seconds. **[SCREEN]** says what to show. The narration underneath is read word for word.

---

### 1. The problem (0:00–0:30)
**[SCREEN]** Title card: "Agent Allowance: Payment Incident Desk". Then the empty desk.

> A supplier emails you: "We never got paid." Now you have to decide. Pay again, and the first payment might still land, so you've paid twice. Wait, and maybe it bounced days ago. Or maybe that email isn't your supplier at all. It's a scammer asking you to send the money somewhere new. Agent Allowance is a desk where an AI agent helps with exactly this, without ever being able to lose the money.

### 2. How it works (0:30–0:55)
**[SCREEN]** Point at the four action buttons, then the "allowed actions" line.

> The AI reads the incident and recommends one of four things: wait, replace, escalate, or close. But the code decides which of those are even allowed. The AI can only pick from that list, and every rule that touches money lives in code, not in the prompt. The brain is free and open source. No paid AI.

### 3. Normal incident: WAIT (0:55–1:30)
**[SCREEN]** Open incident INV-1042, $250. Click "Approve original $250", then Send. In the bank simulator, mark it SENT. Click Ask the agent and show WAIT.

> Here's an invoice for two hundred fifty dollars. I approve it and send it through Airwallex. The bank says it's sent, but not paid yet. I ask the agent, and it says wait. Now watch what happens if I try to pay again.
**[SCREEN]** Click "Pay the supplier again right now" and show the refusal.
> Refused. There's already a payment that could still land, so the desk won't let me pay twice.

### 4. Failure, then a safe replacement (1:30–2:20)
**[SCREEN]** Simulator: fail with CHANNEL_TIMEOUT. Ask the agent and show REPLACE. Try "Send $1 more than approved" and show the refusal.

> Now the bank fails the transfer with a channel timeout. That's a safe reason to retry, and the desk checks it live with Airwallex at the moment I replace it. The agent says replace. If I try to sneak in one extra dollar, it's refused. The approval is locked to the exact payee, amount and bank details I signed off on.
**[SCREEN]** Approve and send the replacement. SENT, then PAID. Agent says CLOSE. Close, then download the receipt.
> The replacement goes through and gets paid. The agent says close, and I get a receipt showing the original cancelled and the replacement paid. Every line says where it came from: Airwallex, the code, the AI, or a person.

### 5. Lost reply (2:20–2:55)
**[SCREEN]** New incident. "Lose the next Airwallex reply", then send. Status UNKNOWN. Agent says WAIT. Click Check Airwallex to find it by request id.

> Here's a scary one. I send a payment, and the reply from Airwallex gets lost. Did it go out or not? The desk marks it unknown and keeps the money held. The agent says wait. When I check Airwallex, it finds the payment using the same request ID, so nothing gets sent twice.

### 6. The scam (2:55–3:40)
**[SCREEN]** New incident. Fail it with ACCOUNT_CLOSED. Paste the supplier email: "our bank changed, pay account 55667788". Agent says ESCALATE. Show that Replace isn't offered.

> Last one, and it's the one that costs businesses the most. The payment fails because the account is closed, and then an email shows up saying, "Our bank changed, send it to this new account." The agent escalates. Replace isn't even on the menu, because the AI has no way to change where money goes. The account number in that email was stripped out before the AI ever saw it.

### 7. Proof and honesty (3:40–4:15)
**[SCREEN]** Terminal: tests passing. Then the sandbox script passing. Then COVERAGE.md.

> A hundred and one automated tests, including attacks written by two separate AI reviewers. Six live scenarios against the Airwallex sandbox. And a map of a hundred twenty-eight ways payments go wrong, with how the desk handles each. To be straight with you: this is sandbox only, and it was built by AI coding agents working under my direction. I'm Zach. Thanks for watching.

---
Word count is about 560 words, which is about 4 minutes 15 seconds at a natural pace.

TTS notes:
- Write "$250" as "two hundred fifty dollars" and "INV-1042" as "invoice ten forty-two" in the narration file.
- Keep the master chain: highpass 55, 1.6:1 compression, loudnorm -16.
