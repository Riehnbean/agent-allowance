// "Take the tour": plain-English steps with a highlight on the thing to tap.
"use strict";
(() => {
  const STEPS = [
    { sel: "#list", t: "This is a payments desk for a company's accounts team. Each row on the left is a supplier saying \"we weren't paid\" - four real situations." },
    { sel: "#list li:nth-child(3)", t: "Tap INV-1043.", wait: () => /INV-1043/.test(document.querySelector("#detail h2")?.textContent || "") },
    { sel: "section.card.story", t: "Read \"What happened\": the supplier wants the money sent to a NEW bank account. That's a classic scam, so the code already froze payments on this one." },
    { sel: "section.card.agent", t: "Tap \"Ask the AI\" and wait a few seconds.", wait: () => !!document.querySelector("section.card.agent .plaindec") },
    { sel: "section.card.agent", t: "The AI says: get a person to check. And notice there's no way to pay this one - the code won't allow it, whatever the AI says." },
    { sel: "#list li:nth-child(4)", t: "Now tap INV-1042.", wait: () => /INV-1042/.test(document.querySelector("#detail h2")?.textContent || "") },
    { sel: "section.card.agent", t: "Tap \"Ask the AI\". This first payment failed and never arrived, so it should say: safe to send it again.", wait: () => !!document.querySelector("section.card.agent .plaindec") },
    { sel: "button.primary", text: "Approve replacement", t: "Tap \"Approve replacement\". That approval only works for this supplier and this amount, for 15 minutes.", wait: () => !!document.querySelector("p.hint") && /Approval #/.test(document.body.textContent) },
    { sel: "p.hint", text: "co-sign", t: "Blocked - on purpose. This supplier would get $550 today, over the $500 limit, so one person isn't enough. A second person has to co-sign." },
    { sel: "#switchop", t: "Tap \"switch to Jordan\" at the top - you're now the second person.", wait: () => /Jordan · switch/.test(document.querySelector("#switchop")?.textContent || "") },
    { sel: "button.primary", text: "Co-sign", t: "Tap \"Co-sign\".", wait: () => /\+ jordan/.test(document.querySelector("#detail")?.textContent || "") },
    { sel: "#switchop", t: "Switch back to Zach.", wait: () => /Zach · switch/.test(document.querySelector("#switchop")?.textContent || "") },
    { sel: "button.primary", text: "Send replacement", t: "Now tap \"Send replacement\". The payment goes out (to a pretend Airwallex - nothing real moves).", wait: () => document.querySelectorAll("#detail div.pay").length >= 2 },
    { sel: "button.primary", text: "Send replacement", t: "Try to send it again - you can't. Each approval works once, so the supplier can't be paid twice." },
    { sel: "#kill", t: "Last one: tap the red STOP button. Now nothing at all can be paid until someone resumes it.", wait: () => /STOPPED/.test(document.querySelector("#kill")?.textContent || "") },
    { sel: null, t: "That's the whole idea: the AI gives advice, the code protects the money, and a person approves every payment. \"Advanced (testers)\" shows the full evidence log and a \"try to break it\" panel." },
  ];
  const css = `
  #tourbtn{position:fixed;right:18px;bottom:18px;z-index:9000;background:#ff7a45;color:#0f1419;border:0;border-radius:999px;padding:12px 20px;font:700 16px -apple-system,Segoe UI,Roboto,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.4);cursor:pointer}
  #tour{position:fixed;right:18px;bottom:18px;z-index:9001;width:min(380px,calc(100vw - 36px));background:#18212b;color:#eef2f6;border:2px solid #ff7a45;border-radius:14px;padding:16px 18px;font:16px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;box-shadow:0 12px 40px rgba(0,0,0,.5)}
  #tour .n{color:#ff7a45;font-weight:700;font-size:13px;margin-bottom:6px}
  #tour .b{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
  #tour button{border:0;border-radius:8px;padding:8px 14px;font:600 14px -apple-system,Segoe UI,sans-serif;cursor:pointer;background:#2f3b48;color:#eef2f6}
  #tour button.go{background:#ff7a45;color:#0f1419}
  .tour-hl{outline:4px solid #ff7a45!important;outline-offset:4px;border-radius:10px;animation:tourpulse 1.4s ease-in-out infinite}
  @keyframes tourpulse{50%{outline-color:rgba(255,122,69,.35)}}`;
  const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
  let k = -1, box = null, tick = 0;
  const target = s => {
    if (!s.sel) return null;
    const all = [...document.querySelectorAll(s.sel)].filter(e => e.offsetParent !== null);
    return s.text ? all.find(e => e.textContent.includes(s.text)) : all[0];
  };
  const clearHl = () => document.querySelectorAll(".tour-hl").forEach(e => e.classList.remove("tour-hl"));
  const show = () => {
    clearHl(); clearInterval(tick);
    if (k < 0 || k >= STEPS.length) { box?.remove(); box = null; btn.hidden = false; return; }
    btn.hidden = true;
    const s = STEPS[k];
    if (!box) { box = document.createElement("div"); box.id = "tour"; document.body.appendChild(box); }
    box.innerHTML = `<div class="n">Step ${k + 1} of ${STEPS.length}</div><div>${s.t}</div><div class="b"><button data-a="x">Close</button>${k ? '<button data-a="b">Back</button>' : ""}<button class="go" data-a="n">${k === STEPS.length - 1 ? "Done" : s.wait ? "Skip" : "Next"}</button></div>`;
    box.onclick = e => { const a = e.target.dataset.a; if (a === "x") { k = -1; show(); } if (a === "b") { k--; show(); } if (a === "n") { k++; show(); } };
    let scrolled = false;
    tick = setInterval(() => {
      const t = target(s); clearHl();
      if (t) { t.classList.add("tour-hl"); if (!scrolled) { t.scrollIntoView({ block: "center", behavior: "smooth" }); scrolled = true; } }
      if (s.wait && s.wait()) { k++; setTimeout(show, 700); clearInterval(tick); }
    }, 400);
  };
  const btn = document.createElement("button"); btn.id = "tourbtn"; btn.textContent = "▶ Take the 2-minute tour";
  btn.onclick = () => { k = 0; show(); };
  document.body.appendChild(btn);
  if (!sessionStorage.getItem("tour-seen")) { try { sessionStorage.setItem("tour-seen", "1"); } catch {} window.__pyReady?.then(() => setTimeout(() => { k = 0; show(); }, 900)); }
})();
