// Runs the real Agent Allowance Python code (allowance/, server.py, demo.py) inside the browser with Pyodide.
// No server, no keys: the Airwallex sandbox is the same in-memory stand-in `python3 demo.py` uses, and the
// AI is the free open-source GPT-OSS model (falls back to the code's rulebook if it can't be reached).
"use strict";
try { sessionStorage.setItem("op-token", sessionStorage.getItem("op-token") || "zach"); } catch {}

const SHIM = `
import os, sys, json, threading
os.environ.update(DEMO="1", OPERATOR_TOKENS="zach:zach,jordan:jordan", ALLOWANCE_DB="/tmp/demo.db",
                  WATCHDOG_EVERY="0", AGENT_PROVIDER="pollinations", THINK_AHEAD="0", DEMO_COSIGN_OVER="20000")
sys.path.insert(0, "/app")
sys.argv = ["demo.py"]

class _SyncThread:                      # the browser has no threads: run background work right away
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **k):
        self._t, self._a, self._k = target, args, kwargs or {}
    def start(self):
        if self._t: self._t(*self._a, **self._k)
    def join(self, *a): pass
    def is_alive(self): return False
threading.Thread = _SyncThread

from js import XMLHttpRequest
import allowance.agent as AG
import time as _time
def _post(self, url, body, headers):       # same request, sent by the browser
    t0 = _time.time()
    x = XMLHttpRequest.new(); x.open("POST", url, False)
    x.setRequestHeader("content-type", "application/json")
    for k, v in headers.items(): x.setRequestHeader(k, v)
    x.send(json.dumps(body))
    if _time.time() - t0 > 8: os.environ["AGENT_PROVIDER"] = ","   # too slow for a browser tab: rulebook from now on
    if x.status != 200: raise RuntimeError(f"AI HTTP {x.status}")
    return json.loads(x.responseText)
AG.Brain._post = _post
AG._clock = threading.RLock()            # work now runs inline, so the same caller may re-enter the lock

import server
server.main = lambda: None
exec(compile(open("/app/demo.py").read(), "/app/demo.py", "exec"), {"__name__": "demo", "__file__": "/app/demo.py"})

def route(method, path, token, body):
    op = server.who(token)
    if not op: return (401, json.dumps({"error": "operator token required"}))
    try:
        b = json.loads(body) if body else {}
        out = server.handle(method, path, b, op)
        if path == "/api/state": out["me"] = op
        return (200, json.dumps(out, default=str))
    except server.Refused as e: return (409, json.dumps({"refused": str(e)}))
    except server.ProviderError as e: return (502, json.dumps({"provider": e.code, "message": e.message}))
    except server.Ambiguous as e: return (504, json.dumps({"ambiguous": str(e)}))
    except (KeyError, ValueError, TypeError) as e: return (400, json.dumps({"error": f"bad request: {e}"}))
    except Exception as e: return (500, json.dumps({"error": f"internal error ({type(e).__name__})"}))
`;

const setStatus = t => { const e = document.getElementById("bootmsg"); if (e) e.textContent = t; };
window.__pyReady = (async () => {
  setStatus("Loading Python in your browser…");
  const py = await loadPyodide();
  setStatus("Loading the database engine…");
  await py.loadPackage("sqlite3");
  setStatus("Loading Agent Allowance…");
  const files = await (await fetch("py.json")).json();
  for (const [p, src] of Object.entries(files)) {
    const parts = ("/app/" + p).split("/"); let d = "";
    for (const part of parts.slice(1, -1)) { d += "/" + part; try { py.FS.mkdir(d); } catch {} }
    py.FS.writeFile("/app/" + p, src);
  }
  setStatus("Setting up four example payments…");
  py.runPython(SHIM);
  // The free AI is called synchronously from Python, so only use it if it answers quickly right now.
  try {
    const ac = new AbortController(); const tm = setTimeout(() => ac.abort(), 6000);
    const r = await _fetch("https://text.pollinations.ai/openai", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: "openai-fast", messages: [{ role: "user", content: "Reply OK" }] }), signal: ac.signal });
    clearTimeout(tm); if (!r.ok) throw new Error("AI " + r.status);
  } catch { py.runPython('import os; os.environ["AGENT_PROVIDER"] = ","'); window.__aiOff = true; }
  const boot = document.getElementById("boot"); if (boot) { boot.style.opacity = 0; setTimeout(() => boot.remove(), 400); }
  return py;
})().catch(e => { setStatus("Sorry - the demo could not start in this browser: " + e.message); throw e; });

const _fetch = window.fetch.bind(window);
window.fetch = async (url, opt = {}) => {
  const u = new URL(url, location.href);
  const i = u.pathname.indexOf("/api/");
  if (i < 0) return _fetch(url, opt);
  const py = await window.__pyReady;
  await new Promise(r => setTimeout(r, 20));          // let the page repaint ("Agent thinking…") before Python runs
  const h = opt.headers || {};
  const res = py.globals.get("route")(opt.method || "GET", u.pathname.slice(i), h["X-Operator-Token"] || "", opt.body || "");
  const [status, body] = res.toJs(); res.destroy();
  return new Response(body, { status, headers: { "Content-Type": "application/json" } });
};

// Second person on the same screen: switch who is signed in (the desk treats zach and jordan as two people).
window.__pyReady.then(() => {
  const who = () => { try { return sessionStorage.getItem("op-token") || "zach"; } catch { return "zach"; } };
  const b = document.createElement("button"); b.id = "switchop"; b.className = "advbtn";
  const label = () => { b.textContent = who() === "zach" ? "You are Zach · switch to Jordan" : "You are Jordan · switch to Zach"; };
  b.onclick = () => { const next = who() === "zach" ? "jordan" : "zach"; document.getElementById("tok").value = next;
    document.getElementById("gateform").requestSubmit(); label(); };
  label(); const k = document.getElementById("kill"); k.parentNode.insertBefore(b, k);
});
