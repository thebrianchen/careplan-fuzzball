"""H1 breakdown: supported recall per condition, split by where each planted conflict's evidence
lives (spoken in the transcript, record only, tract only). C2 beating C0 should come from the
record-only and tract-only rows; on spoken conflicts the two should tie.

    python scripts/h1_report.py runs/<c0> runs/<c1> runs/<c2> [--cases cases/dev] [--out reports/h1_dev.json]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from careplan.cases import load_cases  # noqa: E402
from careplan.data import ResourceTable, TractTable  # noqa: E402
from careplan.runner import apply_case_tracts  # noqa: E402
from careplan.scoring import check_evidence  # noqa: E402


def conflict_kind(c: dict) -> str:
    srcs = {e["source"] for e in c["evidence"]}
    if "transcript" in srcs:
        return "spoken"
    return "tract_only" if srcs == {"tract"} else "record_only"


def boot_ci(hits: dict[str, list[int]], iters: int = 2000, seed: int = 7):
    ids = list(hits)
    if len(ids) < 2:
        return None
    rng, vals = random.Random(seed), []
    for _ in range(iters):
        sample = [h for cid in (rng.choice(ids) for _ in ids) for h in hits[cid]]
        if sample:
            vals.append(sum(sample) / len(sample))
    vals.sort()
    return round(vals[int(0.025 * len(vals))], 3), round(vals[int(0.975 * len(vals)) - 1], 3)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--cases", default="cases/dev")
    ap.add_argument("--out", type=Path, default=Path("reports/h1_dev.json"))
    a = ap.parse_args(argv)
    cases = {c.case_id: c for c in load_cases(Path(a.cases))}
    apply_case_tracts(list(cases.values()))
    tracts, resources = TractTable.load(), ResourceTable.load()
    # (condition, kind) -> case_id -> [hit per planted conflict per rep]
    table: dict[tuple, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    fails, false_flags, runs_n = defaultdict(int), defaultdict(int), defaultdict(int)
    for run in a.runs:
        for f in sorted((run / "results").glob("*.json")):
            rec = json.loads(f.read_text())
            case = cases.get(rec["case_id"])
            if case is None:
                continue
            cond = rec["condition"] + ("" if "reason" not in run.name or "noreason" in run.name else "-reasoning")
            runs_n[cond] += 1
            a_ = rec.get("assessment")
            fails[cond] += a_ is None
            flags = {}
            for it in (a_ or {}).get("items", []):
                if it["status"] == "likely_ok":
                    continue
                for b in it.get("barriers", []):
                    flags.setdefault((it["item_id"], b["barrier"]), []).extend(b["evidence"])
            planted = {(c["item_id"], c["barrier"]): c for c in case.data["planted_conflicts"]}
            false_flags[cond] += sum(1 for k in flags if k not in planted)
            for key, c in planted.items():
                hit = key in flags and check_evidence(case, key[1], flags[key], tracts.get(case.tract_geoid), resources).ok
                for kind in (conflict_kind(c), "all"):
                    table[(cond, kind)][case.case_id].append(int(hit))
    out = {}
    for (cond, kind), hits in sorted(table.items()):
        flat = [h for v in hits.values() for h in v]
        out.setdefault(cond, {})[kind] = {"conflict_reps": len(flat), "supported_recall": round(sum(flat) / len(flat), 3),
                                          "ci95": boot_ci(hits)}
    for cond in out:
        out[cond]["false_flags_per_case"] = round(false_flags[cond] / runs_n[cond], 2)
        out[cond]["failed_runs"] = fails[cond]
        out[cond]["runs"] = runs_n[cond]
    a.out.write_text(json.dumps({"runs": [str(r) for r in a.runs], "cases": sorted(cases), "by_condition": out}, indent=2))
    kinds = ["spoken", "record_only", "tract_only", "all"]
    print(f"{'condition':14}" + "".join(f"{k:>22}" for k in kinds) + f"{'false/case':>12}{'failed':>8}")
    for cond, d in out.items():
        cells = "".join(f"{(str(d[k]['supported_recall']) + ' ' + str(d[k]['ci95'] or '')) if k in d else '-':>22}" for k in kinds)
        print(f"{cond:14}{cells}{d['false_flags_per_case']:>12}{d['failed_runs']:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
