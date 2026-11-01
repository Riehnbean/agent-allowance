"""Held-out evaluation: cases written by a separate AI that never saw the agent's prompt or the 13 builder cases
(heldout/cases.json). Scores the rulebook alone and the AI chain exactly as the desk runs them; code clamps apply.

    AGENT_PROVIDER=anthropic python3 scripts/heldout_eval.py heldout/cases.json
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.test_allowance import Base  # noqa: E402
from allowance.agent import NICE, LLMAgent, RulesAgent  # noqa: E402


class H(Base):
    def runTest(self):
        pass


def build(case):
    h = H()
    h.setUp()
    i = h.incident()
    p = h.pay_original(i)
    if case["situation"] == "failed_timeout":
        h.fail_with(p["request_id"], "CHANNEL_TIMEOUT")
    elif case["situation"] == "paid":
        h.fake.simulate(p["provider_id"], "PAID")
        h.svc.reconcile(p["request_id"])
    for m in case["messages"]:
        h.svc.supplier_message(i, m)
    return h, i


def main(path):
    cases = json.load(open(path))
    agent = LLMAgent()
    agent.budget = None
    print("Model chain:", " -> ".join(NICE.get(b.model, b.label) for b in agent.brains) or "(none)", "-> rulebook",
          "(cross-check on)" if agent.crosscheck else "")
    ok = rules_ok = 0
    times = []
    for c in cases:
        h, i = build(c)
        ev, allowed = h.svc.evidence(i), h.svc.allowed_actions(i)
        rd, _ = RulesAgent().decide(ev, allowed)
        t0 = time.time()
        d, reasons = agent.decide(ev, allowed)
        times.append(time.time() - t0)
        exp = c["expected"]
        ok += d == exp
        rules_ok += rd == exp
        clamp = "" if exp in allowed else f" (code only allowed {allowed})"
        print(f"{'PASS' if d == exp else 'FAIL'}  {c['name'][:46]:46s} expected={exp:8s} AI={d:8s} rulebook={rd:8s}{clamp}")
    n = len(cases)
    print(f"\nHeld-out ({n} cases written independently): AI {ok}/{n} · rulebook alone {rules_ok}/{n} · avg {sum(times)/n:.1f}s")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "heldout", "cases.json"))
