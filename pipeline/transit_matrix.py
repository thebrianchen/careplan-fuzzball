"""Box 1's nightly batch: earliest transit arrival from every Cook tract to every Chicago dialysis
unit, for a few early departure times. Shardable (one Fuzzball task-array element per shard).

    python -m pipeline.transit_matrix --shard 0/1                 # all tracts
    python -m pipeline.transit_matrix --shard 3/16 --out ...      # one task-array element
Output CSV: tract_geoid, ccn, depart, arrive (HH:MM, or empty if unreachable before 11:00).
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from careplan import ROOT
from careplan.data import TractTable
from .transit import Router, hhmm

FACILITIES = ROOT / "data" / "processed" / "dialysis_chicago.csv"
DEPARTURES = ("04:30", "05:00", "05:30")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--limit", type=int, default=0, help="first N tracts only (testing)")
    a = ap.parse_args(argv)
    i, n = map(int, a.shard.split("/"))
    out = a.out or ROOT / "data" / "processed" / f"transit_matrix_s{i}of{n}.csv"
    fac = list(csv.DictReader(FACILITIES.open()))
    tracts = [t for k, t in enumerate(sorted(TractTable.load().rows.values(), key=lambda r: r["tract_geoid"])) if k % n == i]
    tracts = tracts[: a.limit or None]
    r, t0 = Router(), time.time()
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tract_geoid", "ccn", "depart", "arrive"])
        for k, t in enumerate(tracts, 1):
            if t.get("intpt_lat") is None:
                continue
            for dep in DEPARTURES:
                h, m = map(int, dep.split(":"))
                best = r.one_to_all(t["intpt_lat"], t["intpt_lon"], h * 3600 + m * 60, 11 * 3600)
                for fc in fac:
                    arr = r.arrival_at(best, float(fc["lat"]), float(fc["lon"]))
                    w.writerow([t["tract_geoid"], fc["ccn"], dep, hhmm(arr) if arr else ""])
            if k % 50 == 0:
                print(f"  {k}/{len(tracts)} tracts, {time.time() - t0:.0f}s", flush=True)
    print(f"wrote {out} ({len(tracts)} tracts x {len(fac)} units x {len(DEPARTURES)} departures) in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
