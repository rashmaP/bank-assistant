"""Run the eval scenarios against a live model.

  python -m evals.run_evals                      # uses LLM_PROVIDER / API key env vars
  python -m evals.run_evals --repeat 3 --min-pass 0.85
  python -m evals.run_evals --only cheque --json evals/report.json

Exit code is 1 if the overall pass rate is below --min-pass (useful in CI).
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from assistant.config import Settings
from assistant.llm_factory import build_llm
from evals.harness import run_scenario
from evals.scenarios import SCENARIOS


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1, help="runs per scenario (model output varies)")
    ap.add_argument("--only", default="", help="substring filter on scenario name or tag")
    ap.add_argument("--min-pass", type=float, default=0.8)
    ap.add_argument("--pause", type=float, default=0.5, help="seconds between turns (free-tier rate limits)")
    ap.add_argument("--json", default="", help="write a JSON report here")
    a = ap.parse_args(argv)

    cfg = Settings()
    llm = build_llm(cfg)
    model = cfg.openai_model if cfg.llm_provider == "openai" else cfg.anthropic_model
    chosen = [s for s in SCENARIOS if a.only in s.name or a.only in s.tags]
    print(f"Model: {cfg.llm_provider}/{model} | scenarios: {len(chosen)} x {a.repeat}\n")

    rows, passed, total = [], 0, 0
    t0 = time.time()
    for sc in chosen:
        wins, probs = 0, []
        for _ in range(a.repeat):
            r = run_scenario(sc, llm, pause=a.pause)
            total += 1
            wins += r.passed
            passed += r.passed
            probs += r.problems
        mark = "PASS" if wins == a.repeat else ("FLAKY" if wins else "FAIL")
        print(f"{mark:5} {sc.name:34} {wins}/{a.repeat}")
        for p in dict.fromkeys(probs):
            print(f"        - {p}")
        rows.append({"scenario": sc.name, "tags": sc.tags, "passed": wins, "runs": a.repeat, "problems": list(dict.fromkeys(probs))})

    rate = passed / total if total else 0.0
    print(f"\nOverall: {passed}/{total} runs passed ({rate:.0%}) in {time.time() - t0:.0f}s")
    critical = [r for r in rows if "safety" in r["tags"] and r["passed"] < r["runs"]]
    if critical:
        print("SAFETY FAILURES: " + ", ".join(r["scenario"] for r in critical))
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"model": f"{cfg.llm_provider}/{model}", "pass_rate": rate, "results": rows}, f, indent=2)
    return 0 if (rate >= a.min_pass and not critical) else 1


if __name__ == "__main__":
    sys.exit(main())
