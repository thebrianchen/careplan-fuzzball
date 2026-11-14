"""Per-card detail for a run directory: timing, steps, tokens, which tools the model called, and the
scored recall vs supported recall. Quick triage when a speed lever changes quality.

    python scripts/run_detail.py runs/<run> [--cases cases/dev]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from careplan.cases import load_cases  # noqa: E402
from careplan.data import ResourceTable, TractTable  # noqa: E402
from careplan.runner import apply_case_tracts  # noqa: E402
from careplan.scoring import score_assessment  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--cases", type=Path, default=Path("cases/dev"))
    a = ap.parse_args()
    cases = {c.case_id: c for c in load_cases(a.cases)}
    apply_case_tracts(list(cases.values()))
    tracts, resources = TractTable.load(), ResourceTable.load()
    print(f"{'case':6} rep {'wall':>6} steps {'in':>6} {'out':>5} items flag  rec  sup  fp  model tool calls")
    for f in sorted((a.run / "results").glob("*.json")):
        r = json.loads(f.read_text())
        case = cases.get(r["case_id"])
        a_ = r["assessment"] or {"items": []}
        flagged = sum(1 for i in a_["items"] if i["status"] != "likely_ok")
        tools = sorted({t["tool"] for t in r["tool_calls"] if not t.get("prefetched")})
        rec = sup = fp = "-"
        if case and r["assessment"]:
            s = score_assessment(case, r["assessment"], r["condition"], r["rep"], tracts, resources)
            rec, sup, fp = f"{s.tp}/{s.planted}", f"{s.evidence_ok}/{s.planted}", s.fp
        print(f"{r['case_id']:6} r{r['rep']}  {r['wall_s']:6.1f} {r['steps']:5} {r['usage']['prompt_tokens']:6} "
              f"{r['usage']['completion_tokens']:5} {len(a_['items']):5} {flagged:4} {rec:>5} {sup:>5} {fp!s:>3}  "
              f"{','.join(tools)}{'  ERR ' + r['error'] if r['error'] else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
