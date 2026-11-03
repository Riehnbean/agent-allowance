// "Take the tour": plain-English steps with a highlight on the thing to tap.
"use strict";
(() => {
  const det = () => document.querySelector("#detail")?.textContent || "";
  const on = inv => new RegExp(inv).test(document.querySelector("#detail h2")?.textContent || "");
  const answered = () => !!document.querySelector("section.card.agent .plaindec");
  const cosigned = () => on("INV-1042") && /co-signed by Jordan|\+ jordan/i.test(det());
  const sent = () => on("INV-1042") && document.querySelectorAll("#detail div.pay").length >= 2;
  const STEPS = [
    { sel: "#list", t: "This is a payments desk for a company's accounts team. Each row is a supplier payment that needs attention. Let's go through them one by one." },
    { sel: "#list li", text: "INV-1042", t: "We're on INV-1042. Airwallex says the first payment failed with a bank timeout, and the supplier is waiting.", wait: () => on("INV-1042"), stay: true },
    { sel: "section.card.agent", t: "Tap \"Ask the AI\" and wait a few seconds.", wait: () => on("INV-1042") && answered() },
    { sel: "section.card.agent", t: "It says: safe to send it again - the first payment failed and never arrived." },
    { sel: "button.primary", text: "Approve replacement", t: "Tap \"Approve replacement\". That approval only works for this supplier and this amount, for 15 minutes.", wait: () => on("INV-1042") && /approval #/i.test(det()), done: cosigned },
    { sel: "#switchop", t: "It's over the $200 two-person limit, so a second person must co-sign. In real use Jordan signs in on his own computer; here, tap \"switch to Jordan\" at the top.", wait: () => /Jordan · switch/.test(document.querySelector("#switchop")?.textContent || ""), done: cosigned },
    { sel: "button.primary", text: "Co-sign", t: "You're Jordan now. Tap \"Co-sign\".", wait: cosigned },
    { sel: "#switchop", t: "Switch back to Zach. (Either person can press Send once both have signed.)", wait: () => /Zach · switch/.test(document.querySelector("#switchop")?.textContent || ""), done: sent },
    { sel: "button.primary", text: "Send replacement", t: "Tap \"Send replacement\". The payment goes out (to a pretend Airwallex - nothing real moves).", wait: sent },
    { sel: "p.hint", text: "twice", t: "Send is now greyed out: each approval works once, so the supplier can't be paid twice." },
    { sel: "section.card", text: "Payments on this incident", t: "Now Airwallex delivers it. In this demo the pretend bank confirms in a few seconds - just wait.", wait: () => on("INV-1042") && [...document.querySelectorAll("#detail div.pay")].some(d => /paid/i.test(d.textContent) && /Replacement/.test(d.textContent)) },
    { sel: "section.card", text: "Payments on this incident", t: "Paid ✓. There's now a button to copy proof of payment for the supplier, and the receipt shows every step." },
    { sel: "input.note", t: "Last step for this one: type what you checked (e.g. \"Airwallex shows paid\") and tap Close incident.", wait: () => /Done/.test([...document.querySelectorAll("#list li")].find(l => /INV-1042/.test(l.textContent))?.textContent || ""),
      next: async () => { const n = document.querySelector("input.note"); if (n && !n.value) n.value = "Airwallex shows the replacement PAID"; [...document.querySelectorAll("button")].find(b => b.textContent === "Close incident")?.click(); } },
    { sel: "#list li", text: "INV-1043", t: "Now a scam. Tap INV-1043.", wait: () => on("INV-1043") },
    { sel: "section.card.agent", t: "The supplier wants the money sent to a NEW bank account, so the code froze payments the moment that email arrived. Tap \"Ask the AI\".", wait: () => on("INV-1043") && answered() },
    { sel: "section.card.agent", t: "It says: get a person to check. And there's no way to pay this one, whatever the AI says." },
    { sel: "#list li", text: "INV-1044", t: "Tap INV-1044. Airwallex says it's paid, but the supplier says only $280 of $300 arrived.", wait: () => on("INV-1044") },
    { sel: "section.card.agent", t: "Tap \"Ask the AI\". There's no scam word in this email, so keyword rules miss it - the AI reads what the supplier means.", wait: () => on("INV-1044") && answered() },
    { sel: "section.card.story", t: "The desk shows the $20 gap and won't let anyone simply pay the full $300 again. A person checks first (it's usually a bank fee)." },
    { sel: "#list li", text: "EU-2201", t: "Tap EU-2201: a German supplier billing in euros.", wait: () => on("EU-2201") },
    { sel: "p.fxbig", t: "Before anyone approves, it shows the dollar cost at Airwallex's exchange rate. That dollar amount is what the limits and the two-person rule check." },
    { sel: "#kill", t: "Last one: tap the red STOP button. Now nothing at all can be paid until someone resumes it.", wait: () => /STOPPED/.test(document.querySelector("#kill")?.textContent || "") },
    { sel: null, t: () => "That's the idea: the AI gives advice, the code protects the money, and a person approves every payment. " + (window.__aiOff || /safety rulebook/.test(document.body.textContent)
      ? "(The free AI was busy just now, so you saw Claude's saved answers for these examples, checked live by the built-in safety rules - the money rules work the same either way. The video shows the full app with live Claude.)"
      : "(Here the AI is a free open-source model; the video shows the full app with Claude.)") },
  ];
  const css = `
  #tourbtn{position:fixed;right:18px;bottom:18px;z-index:9000;background:#ff7a45;color:#0f1419;border:0;border-radius:999px;padding:12px 20px;font:700 16px -apple-system,Segoe UI,Roboto,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.4);cursor:pointer}
  #tour{position:fixed;right:18px;top:12px;z-index:9001;width:min(380px,calc(100vw - 36px));background:#18212b;color:#eef2f6;border:2px solid #ff7a45;border-radius:14px;padding:16px 18px;font:16px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;box-shadow:0 12px 40px rgba(0,0,0,.5)}
  #tour .n{color:#ff7a45;font-weight:700;font-size:13px;margin-bottom:6px}
  #tour .b{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
  #tour button{border:0;border-radius:8px;padding:8px 14px;font:600 14px -apple-system,Segoe UI,sans-serif;cursor:pointer;background:#2f3b48;color:#eef2f6}
  #tour button.go{background:#ff7a45;color:#0f1419}
  .tour-hl{outline:4px solid #ff7a45!important;outline-offset:4px;border-radius:10px;animation:tourpulse 1.4s ease-in-out infinite}
  @keyframes tourpulse{50%{outline-color:rgba(255,122,69,.35)}}
  body.touring{padding-bottom:55vh}
  @media (max-width:600px){#tourbtn{padding:8px 12px;font-size:13px;right:10px;bottom:10px;opacity:.92}}`;
  const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
  let k = -1, box = null, tick = 0;
  const target = s => {
    if (!s.sel) return null;
    const all = [...document.querySelectorAll(s.sel)].filter(e => e.offsetParent !== null);
    return s.text ? all.find(e => e.textContent.includes(s.text)) : all[0];
  };
  const topPx = () => (innerWidth >= 900 ? Math.round((document.querySelector("header")?.getBoundingClientRect().bottom || 0) + 10) : 12) + "px";   // desktop: below the header, never over STOP
  const clearHl = () => document.querySelectorAll(".tour-hl").forEach(e => e.classList.remove("tour-hl"));
  const show = () => {
    clearHl(); clearInterval(tick);
    document.body.classList.toggle("touring", k >= 0 && k < STEPS.length);
    if (k < 0 || k >= STEPS.length) { box?.remove(); box = null; btn.hidden = false; return; }
    btn.hidden = true;
    const s = STEPS[k];
    if ((s.done && s.done()) || (!s.stay && s.wait && s.wait() && k < STEPS.length - 1)) { k++; return show(); }   // already done: skip ahead
    if (!box) { box = document.createElement("div"); box.id = "tour"; document.body.appendChild(box); box.style.top = topPx(); }
    box.innerHTML = `<div class="n">Step ${k + 1} of ${STEPS.length}</div><div>${typeof s.t === "function" ? s.t() : s.t}</div><div class="b"><button data-a="x">Close</button>${k ? '<button data-a="b">Back</button>' : ""}<button class="go" data-a="n">${k === STEPS.length - 1 ? "Done" : s.wait && !s.stay ? "Skip" : "Next"}</button></div>`;
    box.onclick = e => { const a = e.target.dataset.a; if (a === "x") { k = -1; show(); } if (a === "b") { k--; show(); }
      if (a === "n" && s.next) { clearInterval(tick); box.querySelector('[data-a="n"]').disabled = true; Promise.resolve(s.next()).finally(() => setTimeout(() => { k++; show(); }, 600)); return; }
      if (a === "n") { const t = s.wait && !s.stay && target(s); const c = t && (t.matches("button") ? t : (t.matches("li") ? t.querySelector("button") : t.querySelector("button.primary"))); if (c && !c.disabled) { c.click(); return; } k++; show(); } };   // Skip does the step for you
    let scrolled = false, lastC = null;
    tick = setInterval(() => {
      const t = target(s); clearHl();
      if (t) { t.classList.add("tour-hl");
        // the box sits at the top; bring the thing to tap (or the card's main button) into the lower part of the screen
        const c = t.matches("button, li") ? t : (t.querySelector("button.primary") || t);
        if (!scrolled || (c.tagName + c.textContent) !== lastC) { lastC = c.tagName + c.textContent; const r = c.getBoundingClientRect(), bh = box.getBoundingClientRect().height + 30;
          const want = bh + (innerHeight - bh) * 0.45; if (!c.closest("header")) window.scrollBy({ top: r.top + r.height / 2 - want, behavior: "smooth" }); else if (r.top < 0 || r.bottom > innerHeight) window.scrollTo({ top: 0, behavior: "smooth" }); scrolled = true; }
        const cr = c.getBoundingClientRect(), br = box.getBoundingClientRect();         // never sit on top of the thing to tap
        const hit = !(cr.right < br.left || cr.left > br.right || cr.bottom < br.top || cr.top > br.bottom);
        if (hit) { if (box.style.bottom) { box.style.bottom = ""; box.style.top = topPx(); } else { box.style.top = "auto"; box.style.bottom = "12px"; } } }
      if ((!s.stay && s.wait && s.wait()) || (s.done && s.done())) { k++; setTimeout(show, 700); clearInterval(tick); }
    }, 400);
  };
  const btn = document.createElement("button"); btn.id = "tourbtn"; btn.textContent = "▶ Take the short guided tour";
  btn.onclick = () => { k = 0; show(); };
  document.body.appendChild(btn);
  if (!sessionStorage.getItem("tour-seen")) { try { sessionStorage.setItem("tour-seen", "1"); } catch {} window.__pyReady?.then(() => setTimeout(() => { k = 0; show(); }, 900)); }
})();
