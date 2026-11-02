"use strict";
const $ = (s, r = document) => r.querySelector(s);
const el = (t, cls, text) => { const e = document.createElement(t); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
let token = ""; try { token = sessionStorage.getItem("op-token") || ""; } catch {}
let S = null, current = null; const fxCache = {};
const usd = c => "$" + (c / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (c, ccy) => !ccy || ccy === "USD" ? usd(c) : (ccy === "EUR" ? "€" : ccy + " ") + (c / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const when = t => new Date(t * 1000).toLocaleTimeString("en-US", { timeZone: "America/Chicago", hour: "numeric", minute: "2-digit", second: "2-digit" }) + " CT";

async function api(path, body) {
  const r = await fetch(path, { method: body ? "POST" : "GET", headers: { "Content-Type": "application/json", "X-Operator-Token": token }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) { $("#gate").hidden = false; throw new Error("sign in"); }
  if (!r.ok) throw new Error(j.refused ? "Refused by code: " + j.refused : j.message || j.ambiguous || j.error || ("HTTP " + r.status));
  return j;
}
function toast(msg, bad) { const t = $("#toast"); t.textContent = msg; t.dataset.bad = bad ? "1" : ""; t.classList.add("on"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), 5200); }
async function act(path, body, ok) {
  try { const r = await api(path, body || {}); if (ok) toast(typeof ok === "function" ? ok(r) : ok); await load(); return r; }
  catch (e) { toast(e.message, true); await load().catch(() => {}); }
}

// The 3-second refresh only rebuilds the screen when something actually changed, and never while a finger or mouse
// button is down - rebuilding mid-click used to swallow clicks.
let lastSig = "", pressing = false;
for (const t of ["pointerdown"]) document.addEventListener(t, () => { pressing = true; }, true);
for (const t of ["pointerup", "pointercancel"]) document.addEventListener(t, () => setTimeout(() => { pressing = false; }, 250), true);
let loadSeq = 0;
async function load(quiet) {
  const seq = ++loadSeq;
  const next = await api("/api/state");
  if (seq !== loadSeq) return; // a newer state request has been made since; this reply is obsolete and must not repaint
  const sig = JSON.stringify(next);
  if (quiet && (sig === lastSig || pressing)) return;
  lastSig = sig; S = next;
  $("#gate").hidden = true;
  if (S.env) $("#env").textContent = S.env.replace(" - ", " · ");
  const p = S.policy;
  $("#budget").textContent = `Today: ${usd(p.committed_today)} of ${usd(p.daily_cap)} committed · max ${usd(p.per_tx_cap)} per payment`;
  $("#agentname").textContent = S.agent;
  if (p.log) $("#agentname").append(el("span", p.log.intact ? "adv" : null, p.log.intact ? ` · Log intact ✓ (${p.log.entries})` : ` · LOG TAMPERED at #${p.log.first_bad}`));
  $("#benname").textContent = S.beneficiaries[0] ? `${S.beneficiaries[0].name} (${S.beneficiaries[0].bank_fingerprint})` : "created on first incident";
  const pend = $("#pending"); pend.replaceChildren();
  for (const a of S.pending || []) {
    const li = el("div", "pend"); li.append(el("b", null, a.kind.toUpperCase()), el("span", null, ` requested by ${a.requested_by}: "${a.note}"`));
    if (a.requested_by !== S.me) { const b = el("button", "primary", "Confirm as " + S.me); b.onclick = () => act(`/api/pending/${a.id}/confirm`, {}, r => "Confirmed: " + r.done); li.append(b); }
    else li.append(el("small", null, " - waiting for someone else"));
    pend.append(li);
  }
  pend.hidden = !pend.children.length;
  const wd = $("#wdbar"), ws = S.watchdog || {}; wd.replaceChildren();
  if (ws.state === "awaiting_approval") {
    const names = (ws.fixed || []).map(f => f[0].replace(/_/g, " ").toLowerCase()).join(", ");
    wd.append(el("b", null, "Self-test found a problem and paused payments. "), el("span", null, `It wrote a fix (${names}) that passed all 6 checks. A person decides when it goes live: `));
    const ok = el("button", "primary", "Apply fix and resume"); ok.onclick = () => act("/api/watchdog/apply", {}, "Fix applied - the desk restarts on the new code");
    const no = el("button", null, "Reject fix"); no.onclick = () => act("/api/watchdog/reject", {}, "Fix rejected and undone - payments stay stopped");
    wd.append(ok, no);
  } else if (ws.state === "repaired" && ws.applied_by) wd.append(el("span", null, `Self-test: last fix applied by ${ws.applied_by}. All checks green.`));
  wd.hidden = !wd.children.length;
  const k = $("#kill"); k.textContent = p.killed ? "STOPPED - tap to resume" : "STOP all payments"; k.dataset.on = p.killed ? "1" : "";
  const list = $("#list"); list.replaceChildren();
  for (const i of S.incidents) {
    const li = el("li"); const b = el("button", "inc" + (current === i.id ? " sel" : ""));
    b.append(el("b", null, i.invoice), el("span", "amt", money(i.amount, i.currency)), el("span", "st st-" + i.state, stateLabel(i.state)));
    b.onclick = () => { current = i.id; render(); }; li.append(b); list.append(li);
  }
  if (!current && S.incidents[0]) current = (S.incidents.find(i => i.invoice === "INV-1042") || S.incidents[0]).id;
  render();
}

const PLAIN_STATE = { OPEN: "Open", ESCALATED: "Needs a person", RESOLVED: "Done" };
const stateLabel = st => PLAIN_STATE[st] || st;
const PLAIN_FAIL = { CHANNEL_TIMEOUT: "bank connection timed out", BENEFICIARY_BANK_RETURNED: "the supplier's bank sent it back", INVALID_ACCOUNT: "account number not valid", ACCOUNT_CLOSED: "account is closed", INSUFFICIENT_FUNDS: "not enough money in the account", COMPLIANCE_REJECTED: "blocked by a compliance check", CANCELLED_BY_USER: "cancelled by a person" };
const failLabel = f => PLAIN_FAIL[f] || String(f || "").replace(/_/g, " ").toLowerCase();
function pill(status) { return el("span", "pill p-" + status, status); }
let advanced = false; try { advanced = localStorage.getItem("adv") === "1"; } catch {}
const PLAIN_STATUS = { RESERVED: "being sent", UNKNOWN: "unclear - checking with Airwallex", CREATED: "on its way", SCHEDULED: "on its way", PROCESSING: "on its way",
  SENT: "on its way", OVERDUE: "late", PAID: "paid", CANCELLED: "failed", FAILED: "failing" };
const PLAIN_DECISION = { ESCALATE: "Get a person to check", REPLACE: "Safe to send it again", WAIT: "Wait - it's still on its way", CLOSE: "Done - it was paid" };
function whatHappened(i) {
  const last = i.payments[i.payments.length - 1];
  if (last && last.status === "PAID") { const got = (i.events || []).filter(e => e.kind === "supplier_message").map(e => /\$\s?([\d,]+(?:\.\d\d)?)/.exec(e.body.text || "")).filter(Boolean).map(m => Math.round(parseFloat(m[1].replace(/,/g, "")) * 100)).find(c => c > 0 && c < last.amount);
    if (got) return `Paid ${money(last.amount, last.currency)} via Airwallex, but the supplier says only ${money(got, last.currency)} arrived - a ${money(last.amount - got, last.currency)} gap (often a bank or middle-bank fee). Topping up a short payment is left to a person on purpose: confirm the fee with the supplier first.`; }
  if (i.alert) return `A safety alarm is on (${i.alert.replace(/_/g, " ").toLowerCase()}). Payments here are frozen until a person checks.`;
  if (!last) return "Nothing has been sent yet. A person needs to approve the payment first.";
  const why = last.failure_message ? ` (bank said: "${last.failure_message}")` : "";
  return last.status === "CANCELLED" ? `The last payment of ${money(last.amount, last.currency)} failed${why}. Nothing was paid.` : `The last payment of ${money(last.amount, last.currency)} is ${PLAIN_STATUS[last.status] || last.status.toLowerCase()}.`;
}

function render() {
  for (const b of document.querySelectorAll(".inc")) b.classList.toggle("sel", false);
  const i = S && S.incidents.find(x => x.id === current); const d = $("#detail"); d.replaceChildren();
  if (!i) { d.append(el("p", "empty", "Open an incident to start.")); return; }
  [...document.querySelectorAll(".inc")][S.incidents.indexOf(i)]?.classList.add("sel");
  const head = el("div", "dhead");
  head.append(el("h2", null, `${i.supplier} · ${i.invoice}`), el("span", "big", money(i.amount, i.currency)), el("span", "st st-" + i.state, stateLabel(i.state)));
  d.append(head);
  if (i.alert) { const al = el("div", "alert"); al.append(el("b", null, "Safety alarm: " + i.alert.replace(/_/g, " ")), el("span", null, " - new payments on this incident are frozen until a person resolves it.")); d.append(al); }
  if (S.policy.killed) { const al = el("div", "alert"); al.append(el("b", null, "STOP is on: "), el("span", null, S.policy.killed_reason || "")); d.append(al); }

  // Plain-English summary, what the supplier said, and the three steps
  const sum = el("section", "card story");
  sum.append(el("h3", null, "What happened"), el("p", "plain", whatHappened(i)));
  const said = (i.events || []).filter(e => e.kind === "supplier_message").slice(0, 2);
  if (said.length) { sum.append(el("h3", null, "What the supplier said")); for (const e of said) sum.append(el("blockquote", "said", `"${e.body.text}"`)); }
  if (i.alert === "FRAUD_SIGNAL") sum.append(el("p", "callback", "Before paying anyone: call the supplier on a phone number you already have - never one from the email."));
  const steps = el("ol", "steps");
  for (const [n, t] of [["1", "Read what happened"], ["2", "Ask the AI what to do"], ["3", "Approve and send - or get a person to check"]]) steps.append(el("li", null, t));
  sum.append(steps); d.append(sum);

  // Agent card
  const ag = el("section", "card agent");
  const top = el("div", "agtop"); top.append(el("h3", null, "The AI's advice"));
  const aiTag = { ready: ["ai-ready", "AI answer ready ✓"], thinking: ["ai-think", "AI is reading the evidence…"], }[i.ai];
  if (aiTag) top.append(el("span", "aitag " + aiTag[0], aiTag[1]));
  const ask = el("button", "primary", "Ask the AI");
  ask.onclick = async () => { ask.disabled = true; ask.textContent = "Agent thinking…";
    await act(`/api/incidents/${i.id}/agent`, {}, r => `Advice ready: ${PLAIN_DECISION[r.decision] || r.decision}`); };
  if (!i.payments.length) { ask.disabled = true; ask.title = "Nothing has been sent yet - approve and send it below. The AI is for payments that went wrong."; }
  top.append(ask);
  ag.append(top);
  if (i.decision) {
    ag.append(el("div", "decision adv d-" + i.decision.decision, i.decision.decision), el("div", "plaindec d-" + i.decision.decision, PLAIN_DECISION[i.decision.decision] || ""));
    ag.append(el("p", "answered", i.decision.provenance === "model" || /AI|Claude|GPT/i.test(i.decision.answered_by || "") ? "Answered by: " + (i.decision.answered_by || "the AI") : "Answered by the safety rulebook (the AI wasn't reachable just then, so plain code answered)"));
    const ul = el("ul"); for (const r of i.decision.reasons || []) ul.append(el("li", /^\(/.test(r) ? "adv" : null, r)); ag.append(ul);
    ag.append(el("p", "hint adv", `Allowed by code at the time: ${(i.decision.allowed || []).join(", ")} · ${when(i.decision.at)} · answered by: ${i.decision.answered_by || (i.decision.provenance === "model" ? "AI model" : "Rulebook (code)")}`));
  } else if (!i.payments.length) ag.append(el("p", "hint", "Nothing has gone wrong yet. The AI helps once a payment fails or the supplier disputes it."));
  else ag.append(el("p", "hint", "The AI can only advise. It cannot pay, approve or change who gets paid."));
  d.append(ag);

  // Payments
  const ps = el("section", "card"); ps.append(el("h3", null, "Payments on this incident"));
  if (!i.payments.length) ps.append(el("p", "hint", "Nothing sent yet."));
  for (const p of i.payments) {
    const row = el("div", "pay");
    const l = el("div"); l.append(el("b", null, p.role === "original" ? "Original" : "Replacement"), pill(p.status));
    if (p.failure_type) l.append(el("span", "fail", failLabel(p.failure_type) + (p.failure_message ? ` - "${p.failure_message}"` : "")));
    if (p.anomaly) l.append(el("span", "fail", "⚠ " + p.anomaly.replace(/_/g, " ")));
    if (p.fx_quote_id) l.append(el("div", "fxbig", `${money(p.amount, p.currency)} = ${usd(p.cost_amount)} - Airwallex rate locked · checked against your ${usd(S.policy.per_tx_cap)} limit`));
    if (p.fx_quote_id) l.append(el("small", "adv", `Locked FX: ${money(p.amount, p.currency)} = ${usd(p.cost_amount)} at ${p.fx_rate} (Airwallex quote, counted against USD caps)`));
    if (p.fee) l.append(el("small", null, "Airwallex fee " + usd(p.fee)));
    l.append(el("small", "adv", `request ${p.request_id.slice(0, 8)}… · Airwallex ${p.provider_id ? p.provider_id.slice(0, 8) + "…" : "unknown"} · source: ${p.status_source}`));
    const r = el("div", "payacts");
    if (["UNKNOWN", "RESERVED"].includes(p.status)) {
      const rs = el("button", "warnbtn", "Re-send (same request ID)"); rs.title = "Only if Airwallex has never heard of it. Same request ID, all checks run again; Airwallex refuses a true duplicate.";
      rs.onclick = () => act(`/api/incidents/${i.id}/resend`, { request_id: p.request_id }, x => "Status now " + x.status); r.append(rs);
      const rl = el("button", null, "Release hold"); rl.title = "Only for a payment Airwallex has no record of (e.g. its approval expired). Needs a note; frees the budget.";
      rl.onclick = () => { const n = ($(".note") || {}).value || ""; act(`/api/incidents/${i.id}/release`, { request_id: p.request_id, note: n }, x => "Payment now " + x.status); };
      r.append(rl);
    }
    const rec = el("button", null, "Check Airwallex"); rec.title = "Reconcile: look this request up at Airwallex"; rec.onclick = () => act(`/api/payments/${p.request_id}/reconcile`, {}, x => "Airwallex says " + x.status); r.append(rec);
    if (!["PAID", "CANCELLED"].includes(p.status) && p.provider_id) {
      const sim = el("span", "sim adv"); sim.append(el("small", null, "Sandbox bank simulator:"));
      if (p.status !== "SENT") { const b = el("button", null, "Bank picks it up (SENT)"); b.onclick = () => act(`/api/payments/${p.request_id}/simulate`, { status: "SENT" }, "Simulated SENT"); sim.append(b); }
      else {
        const b = el("button", null, "Arrives (PAID)"); b.onclick = () => act(`/api/payments/${p.request_id}/simulate`, { status: "PAID" }, "Simulated PAID"); sim.append(b);
        const sel = el("select"); for (const f of S.failure_types) { const o = el("option", null, f); o.value = f; sel.append(o); } sel.value = "CHANNEL_TIMEOUT";
        const fb = el("button", "warnbtn", "Fails with"); fb.onclick = () => act(`/api/payments/${p.request_id}/simulate`, { status: "FAILED", failure_type: sel.value }, "Simulated failure " + sel.value);
        sim.append(fb, sel);
      }
      r.append(sim);
    }
    if (p.status === "PAID" && p.provider_id) {
      const proof = el("button", null, "Copy payment proof for the supplier");
      proof.onclick = async () => { const t = `Hi ${i.supplier}, here is proof of payment for ${i.invoice}: ${money(p.amount, p.currency)} sent via Airwallex, transfer reference ${p.provider_id}, status PAID${p.updated_at ? " (confirmed " + new Date(p.updated_at * 1000).toISOString().slice(0, 10) + ")" : ""}. If it hasn't reached you, please check with your bank using that reference.`;
        try { await navigator.clipboard.writeText(t); toast("Payment proof copied - paste it into your reply"); } catch { prompt("Copy this payment proof:", t); } };
      r.prepend(proof);
    }
    if (p.status === "PAID" && p.provider_id) { const sim = el("span", "sim adv"); sim.append(el("small", null, "Sandbox bank simulator:")); const b = el("button", "warnbtn", "Bank returns it days later"); b.onclick = () => act(`/api/payments/${p.request_id}/simulate`, { status: "FAILED", failure_type: "BENEFICIARY_BANK_RETURNED" }, "Simulated a return after PAID"); sim.append(b); r.append(sim); }
    row.append(l, r); ps.append(row);
  }
  d.append(ps);

  // Operator actions
  const op = el("section", "card"); op.append(el("h3", null, "You"));
  const ops = el("div", "btns");
  // a payment that was blocked before reaching Airwallex never went out, so the next one is still the original
  const role = i.payments.some(p => !(p.status === "CANCELLED" && p.status_source === "none")) ? "replacement" : "original";
  const unused = i.approvals.find(a => a.role === role && !a.used_by_request && a.expires_at * 1000 > Date.now());
  const ap = el("button", "primary", `Approve ${role} ${money(i.amount, i.currency)}`); ap.onclick = () => act(`/api/incidents/${i.id}/approve`, { role }, "Approved for 15 minutes"); ap.disabled = !!unused || i.state !== "OPEN" || (role === "replacement" && !i.allowed.includes("REPLACE"));
  const pay = el("button", "primary", `Send ${role}`); pay.onclick = () => { pay.disabled = true; return act(`/api/incidents/${i.id}/pay`, { role }, r => `Sent - ${PLAIN_STATUS[r.status] || r.status.toLowerCase()}`); }; pay.disabled = !unused || (unused && !unused.cosigned_by && (unused.needs_cosign || i.amount > S.policy.dual_approval_over));
  const note = el("input", "note"); note.placeholder = "Note: what you checked (needed to close)"; 
  const close = el("button", null, "Close incident"); close.onclick = () => act(`/api/incidents/${i.id}/close`, { note: note.value }, "Incident resolved"); close.disabled = !i.allowed.includes("CLOSE") || i.state === "RESOLVED";
  close.onclick = () => { if (!note.value.trim()) { note.focus(); return toast("Write a short note first - what you checked before closing", true); } act(`/api/incidents/${i.id}/close`, { note: note.value }, "Incident closed"); };
  const rc = el("a", "btnlink", "Download receipt"); rc.href = "#"; rc.onclick = async e => { e.preventDefault(); try { const r = await api(`/api/incidents/${i.id}/receipt`); const L = [`RECEIPT - ${i.supplier} ${i.invoice} - ${money(i.amount, i.currency)}`, `Environment: ${r.environment}`, `Log intact: ${r.log_integrity.intact ? "yes" : "NO"} (${r.log_integrity.entries_checked} entries checked)`, ""];
    for (const x of r.proof_of_payment || []) L.push(`PAID: Airwallex transfer ${x.airwallex_transfer_id} (${x.role})`);
    for (const a of i.approvals || []) L.push(`APPROVAL #${a.id}: ${a.role} ${money(a.amount, i.currency)} - approved by ${a.approved_by}${a.cosigned_by ? ", co-signed by " + a.cosigned_by : ""}${a.used_by_request ? " (used once)" : ""}`);
    L.push("", "What happened, step by step (source in brackets):");
    for (const e of r.events.filter(e => e.incident_id)) L.push(`${new Date(e.at * 1000).toISOString().slice(0, 19).replace("T", " ")}  [${e.provenance}] ${e.kind.replace(/_/g, " ")}${e.actor && e.provenance === "operator" ? " by " + e.actor : ""}  #${(e.hash || "").slice(0, 10)}`);
    L.push("", "Sources: observed = read back from Airwallex · decided = enforced by code · model = AI opinion · operator = a person · untrusted = supplier text", "", "Full machine-readable receipt follows:", JSON.stringify(r, null, 1));
    const a = el("a"); a.href = URL.createObjectURL(new Blob([L.join("\n")], { type: "text/plain" })); a.download = `receipt-${i.invoice}.txt`; a.click(); } catch (err) { toast(err.message, true); } };
  const reopen = el("button", null, "Reopen after checking"); reopen.title = "After you've investigated (e.g. called the supplier on a number you already had). Your note is kept in the log.";
  reopen.onclick = () => act(`/api/incidents/${i.id}/reopen`, { note: note.value }, r => r && r.pending ? "Reopen request saved - a second person must confirm it" : "Incident reopened"); reopen.hidden = i.state !== "ESCALATED";
  if (i.state === "ESCALATED") note.placeholder = "What did you check? (needed to reopen or close)";
  const csv = el("a", "btnlink", "Log as spreadsheet (CSV)"); csv.href = "#"; csv.onclick = async e => { e.preventDefault(); try {
    const r = await api(`/api/incidents/${i.id}/receipt`); const q = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const dollars = c => c == null || c === "" ? "" : (c / 100).toFixed(2);
    const rows = [["time_utc", "invoice", "supplier", "event", "by", "source", "amount", "currency", "status", "request_id", "airwallex_transfer_id", "details", "hash"].join(",")].concat(r.events.filter(e => e.incident_id).map(e => { const b = e.body || {};
      return [new Date(e.at * 1000).toISOString(), i.invoice, i.supplier, e.kind.replace(/_/g, " "), e.actor, e.provenance, dollars(b.amount), b.currency || "", b.status || "", b.request_id || "", b.provider_id || "", JSON.stringify(b), e.hash].map(q).join(","); }));
    const a = el("a"); a.href = URL.createObjectURL(new Blob([rows.join("\n")], { type: "text/csv" })); a.download = `log-${i.invoice}.csv`; a.click(); } catch (err) { toast(err.message, true); } };
  ops.append(ap, pay, note, reopen, close, rc, csv); op.append(ops);
  if (i.currency !== S.policy.currency && !i.payments.some(p => p.status === "PAID")) {   // show the dollar cost before anyone approves
    const fx = el("p", "fxbig", "Checking today's Airwallex rate…"); op.append(fx);
    const c = fxCache[i.id]; const get = c && Date.now() - c.t < 120000 ? c.p : (fxCache[i.id] = { t: Date.now(), p: api(`/api/incidents/${i.id}/fx-preview`, {}) }).p;
    get.then(q => { if (q.cost) fx.textContent = `${money(i.amount, i.currency)} ≈ ${usd(q.cost)} at Airwallex's rate ${q.rate}${q.mid_rate ? " (market " + q.mid_rate + ")" : ""}. This dollar amount is what the ${usd(S.policy.dual_approval_over)} co-sign and ${usd(S.policy.per_tx_cap)} limits check. The rate is locked when you press Send.`; })
      .catch(() => { delete fxCache[i.id]; fx.textContent = "Couldn't get a rate right now - the exact dollar cost is locked and checked when you press Send."; });
  }
  if (unused && !unused.cosigned_by && (unused.needs_cosign || i.amount > S.policy.dual_approval_over)) {
    if (unused.approved_by !== S.me) {
      const cb = el("button", "primary", `Co-sign ${money(i.amount, i.currency)} as ${S.me}`); cb.onclick = () => act(`/api/incidents/${i.id}/cosign`, { role }, r => "Co-signed by " + r.cosigned_by);
      op.append(cb);
    } else op.append(el("p", "hint", `Over ${usd(S.policy.dual_approval_over)} (or over it for this payee today): someone other than ${S.me} must open this desk with their own sign-in and co-sign.`));
  }
  op.append(el("p", "hint", unused ? `Approval #${unused.id} by ${unused.approved_by}${unused.cosigned_by ? " + " + unused.cosigned_by : ""} - binds this supplier, ${money(unused.amount, i.currency)}, ${i.currency}, expires ${when(unused.expires_at)}.` : (i.payments.length && i.approvals.some(a => a.used_by_request) && !["CANCELLED"].includes(i.payments[i.payments.length - 1].status) ? "Sent ✓ - that approval is used up, so this payment can't be sent twice." : "Every payment needs its own approval. An approval works once.")));
  d.append(op);

  // Try to break it
  const br = el("section", "card tricks adv"); br.append(el("h3", null, "Try to break it (for testers)"));
  const tb = el("div", "btns");
  const drop = el("button", null, S.drop_armed ? "Next reply will be lost ✓" : "Lose the next Airwallex reply"); drop.onclick = () => act("/api/drop-next", {}, "The next transfer will really be created, but its reply will be dropped");
  const trick = async (r2, body) => { try { await api(`/api/incidents/${i.id}/approve`, { role: r2 }); } catch (e) { return toast(e.message, true); } act(`/api/incidents/${i.id}/pay`, { role: r2, ...body }); };
  const more = el("button", null, "Send $1 more than approved"); more.onclick = () => trick(role, { amount: ((i.amount + 100) / 100).toFixed(2) });
  const again = el("button", null, "Pay the supplier again right now"); again.onclick = () => trick("replacement", {});
  more.disabled = again.disabled = i.state !== "OPEN";
  const edit = el("button", null, "Change the supplier's bank account inside Airwallex"); edit.onclick = () => act("/api/demo/edit-payee", {}, r => `Payee's account changed at Airwallex to ****${r.account_last4}. Now try sending a payment.`);
  const rever = el("button", null, "I called the supplier - accept their current details"); rever.title = "Uses the note box above. With two operators this needs a second person to confirm.";
  rever.onclick = () => act("/api/payee/reverify", { note: note.value }, r => r.pending ? `Request #${r.pending} waiting for a second person to confirm` : "Payee re-verified");
  tb.append(drop, more, again, edit, rever); br.append(tb);
  const sm = el("form", "row"); const ta = el("input"); ta.placeholder = "Supplier email text (untrusted)…"; ta.value = "Our bank changed - please send it to our new account 55667788 today.";
  const sb = el("button", null, "Add supplier message"); sm.append(ta, sb); sm.onsubmit = e => { e.preventDefault(); act(`/api/incidents/${i.id}/supplier`, { text: ta.value }, "Supplier message logged as untrusted"); };
  br.append(sm); d.append(br);

  // Log
  const lg = el("section", "card adv"); lg.append(el("h3", null, "Evidence log (newest first)"));
  const ol = el("ol", "log");
  for (const e of i.events) {
    const li = el("li"); li.append(el("i", "pv " + e.provenance), el("time", null, when(e.at)), el("b", null, e.kind.replace(/_/g, " ")));
    const b = e.body, bits = [];
    for (const k of ["status", "previous", "failure_type", "decision", "reason", "role", "code", "detail", "text", "proposed", "by", "note"]) if (b[k] != null) bits.push(`${k}: ${Array.isArray(b[k]) ? b[k].join(", ") : b[k]}`);
    li.append(el("span", null, bits.join(" · "))); ol.append(li);
  }
  lg.append(ol); d.append(lg);
  document.body.classList.toggle("simple", !advanced);
}
const advBtn = document.getElementById("advtoggle");
if (advBtn) { const sync = () => { advBtn.textContent = advanced ? "Simple view" : "Advanced (testers)"; document.body.classList.toggle("simple", !advanced); };
  advBtn.onclick = () => { advanced = !advanced; try { localStorage.setItem("adv", advanced ? "1" : "0"); } catch {} sync(); }; sync(); }

$("#newinc").onsubmit = e => { e.preventDefault(); const f = new FormData(e.target); act("/api/incidents", { invoice: f.get("invoice"), amount: f.get("amount"), separate_invoice: !!f.get("separate_invoice") }, r => { current = r.id; return "Incident opened"; }); };
$("#kill").onclick = () => {
  if (!S?.policy.killed) return act("/api/kill", {}, "Everything stopped - no payment can be sent");
  const box = $("#resume"); box.hidden = !box.hidden;
};
$("#resumeform").onsubmit = e => { e.preventDefault(); act("/api/unkill", { note: $("#resumenote").value }, r => r.pending ? `Resume request #${r.pending} needs a second person` : "Payments resumed"); $("#resume").hidden = true; };
$("#gateform").onsubmit = e => { e.preventDefault(); token = $("#tok").value; try { sessionStorage.setItem("op-token", token); } catch {} load().catch(err => toast(err.message, true)); };
load().catch(() => { $("#gate").hidden = false; });
setInterval(() => { if (!document.hidden && token && !document.querySelector("input:focus,textarea:focus")) load(true).catch(() => {}); }, 3000);
