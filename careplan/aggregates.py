"""De-identified aggregates: the middle data tier (Ryan, 2026-11-01).

PHI stays on the private box. Public data is built on the public box. In between: counts by census tract that the
private box computes nightly from its own cards and publishes to the public box, so population-level signals
(where no-shows cluster, which barriers get flagged where) can inform resource building without any patient leaving.
Small cells are suppressed (k-anonymity: tracts with fewer than K patients are dropped), only enums and counts are
written, and the file is signed with the same Ed25519 bundle key the public box already verifies.

    python -m careplan.aggregates --cases cases/dev --runs runs/<run> --out bundle/aggregates.json [--k 5]
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import ROOT
from .cases import load_cases
from .runner import apply_case_tracts

FIELDS = ("patients", "appointments", "no_shows", "flagged_items", "barriers")


def build(cases_dir: Path, run_dirs: list[Path], k: int = 5) -> dict:
    cases = load_cases(cases_dir)
    apply_case_tracts(cases)
    by_tract: dict[str, dict] = defaultdict(lambda: {"patients": 0, "appointments": 0, "no_shows": 0, "flagged_items": 0, "barriers": Counter()})
    for c in cases:
        t = c.tract_geoid
        if not t:
            continue
        row = by_tract[t]
        row["patients"] += 1
        for a in c.patient.get("appointments", []):
            row["appointments"] += 1
            row["no_shows"] += a.get("status") == "no_show"
    for rd in run_dirs:
        for f in sorted(rd.glob("results/*.json")):
            rec = json.loads(f.read_text())
            case = next((c for c in cases if c.case_id == rec["case_id"]), None)
            if not case or not case.tract_geoid or not rec.get("assessment"):
                continue
            for it in rec["assessment"]["items"]:
                if it["status"] != "likely_ok":
                    by_tract[case.tract_geoid]["flagged_items"] += 1
                    for b in it.get("barriers", []):
                        by_tract[case.tract_geoid]["barriers"][b["barrier"]] += 1
    rows, suppressed = [], 0
    for t, r in sorted(by_tract.items()):
        if r["patients"] < k:
            suppressed += 1
            continue
        rows.append({"tract_geoid": t, "patients": r["patients"], "appointments": r["appointments"], "no_shows": r["no_shows"],
                     "no_show_rate": round(r["no_shows"] / r["appointments"], 3) if r["appointments"] else None,
                     "flagged_items": r["flagged_items"], "barriers": dict(r["barriers"])})
    return {"built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tier": "deidentified-aggregates", "k": k,
            "tracts": rows, "tracts_suppressed_under_k": suppressed,
            "note": "Counts by census tract only; tracts with fewer than k patients are dropped. Synthetic patients."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, default=ROOT / "cases" / "dev")
    ap.add_argument("--runs", type=Path, nargs="*", default=[])
    ap.add_argument("--out", type=Path, default=ROOT / "bundle" / "aggregates.json")
    ap.add_argument("--k", type=int, default=5)
    a = ap.parse_args(argv)
    out = build(a.cases, a.runs, a.k)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2))
    print(f"{len(out['tracts'])} tracts published, {out['tracts_suppressed_under_k']} suppressed (k={a.k}) -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
