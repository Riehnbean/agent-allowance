"""Build the no-install browser demo into docs/demo/ (served by GitHub Pages):
    python3 play/build.py
It packs the real Python sources (allowance/, server.py, demo.py) into py.json; boot.js runs them with Pyodide."""
import glob
import json
import os
import re
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "demo")
os.makedirs(OUT, exist_ok=True)
files = {}
for p in sorted(glob.glob(os.path.join(ROOT, "allowance", "*.py"))) + [os.path.join(ROOT, "server.py"), os.path.join(ROOT, "demo.py")]:
    files[os.path.relpath(p, ROOT)] = open(p, encoding="utf-8").read()
json.dump(files, open(os.path.join(OUT, "py.json"), "w"))
for f in ("app.js", "app.css"):
    shutil.copy(os.path.join(ROOT, "web", f), os.path.join(OUT, f))
for f in ("boot.js", "tour.js"):
    shutil.copy(os.path.join(ROOT, "play", f), os.path.join(OUT, f))
html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
html = html.replace('href="/app.css"', 'href="app.css"')
html = html.replace('<script defer src="/app.js"></script>',
                    '<script src="https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js"></script>'
                    '<script defer src="boot.js"></script><script defer src="app.js"></script><script defer src="tour.js"></script>')
boot = ('<div id="boot" style="position:fixed;inset:0;z-index:99999;background:#0f1419;color:#eef2f6;display:flex;flex-direction:column;'
        'align-items:center;justify-content:center;gap:14px;font:18px -apple-system,Segoe UI,Roboto,sans-serif;text-align:center;padding:20px;transition:opacity .4s">'
        '<div style="font-size:30px;font-weight:800;color:#ff7a45">Agent Allowance</div>'
        '<div id="bootmsg">Starting the demo…</div>'
        '<div style="color:#9aa5b1;font-size:14px;max-width:520px">The real app code runs right here in your browser - no install, no account. '
        'Airwallex is simulated, so nothing real can be paid. Everything resets when you reload.</div></div>')
html = html.replace("<body>", "<body>\n" + boot, 1)
html = re.sub(r'<span class="env" id="env">[^<]*</span>', '<span class="env" id="env">Browser demo · simulated Airwallex · fake money</span>', html)
open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(html)
print("built", OUT, len(files), "python files")
