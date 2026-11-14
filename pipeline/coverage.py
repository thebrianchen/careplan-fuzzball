"""E2 report: does the civic join work? Writes reports/e2_coverage.md.

    python -m pipeline.coverage
"""
from __future__ import annotations

import sys
from datetime import date

import pandas as pd

from careplan import ROOT

TRACTS = ROOT / "data" / "processed" / "tracts.csv"
CASE_TRACTS = ROOT / "data" / "processed" / "case_tracts.csv"
OUT = ROOT / "reports" / "e2_coverage.md"
CORE = ["no_vehicle_share", "transit_stops_800m", "early_service_stops", "lila_1_10", "lila_half_10",
        "places_lacktrpt", "places_foodinsecu"]
P75 = ["no_vehicle_share", "places_lacktrpt", "places_foodinsecu", "places_shututility", "places_housinsecu"]
CASE_COLS = ["no_vehicle_share", "early_service_stops", "lila_1_10", "lila_half_10", "places_lacktrpt", "places_foodinsecu"]


def report(tracts: pd.DataFrame, cases: pd.DataFrame | None) -> tuple[str, bool]:
    lines = [f"# E2 civic join coverage ({date.today().isoformat()})", "",
             f"Tracts in table: **{len(tracts)}**", "", "## Missing values per signal (all tracts)", "",
             "| Signal | Missing | Share |", "|---|---|---|"]
    passed = True
    for col in [c for c in tracts.columns if c not in ("tract_geoid", "data_vintage")]:
        miss = int(tracts[col].isna().sum())
        lines.append(f"| {col} | {miss} | {miss / len(tracts):.1%} |")
    complete = tracts[CORE].notna().all(axis=1).mean()
    lines += ["", f"Tracts with every core signal: **{complete:.1%}** (pass: >= 95%)"]
    passed &= complete >= 0.95
    lines += ["", "## Barrier thresholds (75th percentile; copy into rules/barrier_rules.yaml)", "",
              "| Signal | p75 | Share of tracts with value 0 |", "|---|---|---|"]
    for col in [c for c in P75 if c in tracts.columns]:
        lines.append(f"| {col} | {tracts[col].quantile(0.75):.3g} | {(tracts[col] == 0).mean():.1%} |")
    for col in ("early_service_stops", "lila_1_10", "lila_half_10"):
        if col in tracts.columns:
            lines.append(f"| {col} | n/a | {(tracts[col] == 0).mean():.1%} |")
    if cases is not None and len(cases):
        rate = (cases["tract_geoid"].astype(str).str.len() == 11).mean()
        lines += ["", "## Case addresses", "", f"Geocode match rate: **{rate:.1%}** of {len(cases)} (pass: >= 95%)"]
        passed &= rate >= 0.95
        j = cases.merge(tracts, on="tract_geoid", how="left")
        nv = j["no_vehicle_share"].dropna()
        if len(nv):
            lines += [f"No-vehicle share across case tracts: min {nv.min():.0%}, median {nv.median():.0%}, max {nv.max():.0%} "
                      "(pass: spans at least 10% to 40%)"]
            passed &= nv.min() <= 0.10 and nv.max() >= 0.40
        cols = [c for c in CASE_COLS if c in j.columns]
        lines += ["", "| Case | Tract | " + " | ".join(cols) + " |", "|---" * (len(cols) + 2) + "|"]
        for _, r in j.iterrows():
            lines.append(f"| {r['case_id']} | {r['tract_geoid']} | " + " | ".join(str(r[c]) for c in cols) + " |")
    lines += ["", f"**H2 overall: {'PASS' if passed else 'FAIL'}**",
              "", "Spot-check 10 tracts against the USDA atlas map and a transit map before trusting this."]
    return "\n".join(lines) + "\n", passed


def main() -> int:
    if not TRACTS.exists():
        print("run `python -m pipeline.build_tracts` first", file=sys.stderr)
        return 1
    tracts = pd.read_csv(TRACTS, dtype={"tract_geoid": str})
    cases = pd.read_csv(CASE_TRACTS, dtype={"tract_geoid": str}) if CASE_TRACTS.exists() else None
    text, _ = report(tracts, cases)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
