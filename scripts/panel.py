#!/usr/bin/env python
"""A clinic's panel for the demo: made-up patients from the neighbourhoods around one clinic (Nov 14).

The 600 training patients (careplan.synth) are spread over 465 census tracts, at most 4 in each, so the aggregates' rule of
five would drop every tract. A real clinic's patients cluster near it. This generates patients with the same generator,
restricted to the tracts nearest a clinic, and writes one YAML file per patient in streams and chunks for the batch runner:

    python scripts/panel.py --n 600 --tracts 48 --out data/panel        # data/panel/s0..s2/c0..c3/S1xxxx.yaml

Not for scoring and not for training: the plans are reviewed and counted by neighbourhood (careplan.aggregates).
The default clinic location is the centre of tract 17031260300 (West Garfield Park, the hero case's tract).
"""
from __future__ import annotations

import argparse
import math
import random
import shutil
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from careplan.cases import validate_case          # noqa: E402
from careplan.data import TractTable              # noqa: E402
from careplan.synth import generate               # noqa: E402


def km(a_lat, a_lon, b_lat, b_lon) -> float:
    return math.hypot((a_lat - b_lat) * 111.0, (a_lon - b_lon) * 111.0 * math.cos(math.radians(a_lat)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--tracts", type=int, default=48, help="how many of the nearest tracts make up the catchment")
    ap.add_argument("--clinic-tract", default="17031260300")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--streams", type=int, default=3)
    ap.add_argument("--chunk", type=int, default=50)
    ap.add_argument("--out", type=Path, default=Path("data/panel"))
    a = ap.parse_args(argv)
    table = TractTable.load()
    home = table.get(a.clinic_tract)
    rows = [r for r in table.rows.values() if r.get("intpt_lat") is not None]
    rows.sort(key=lambda r: km(home["intpt_lat"], home["intpt_lon"], r["intpt_lat"], r["intpt_lon"]))
    near = rows[:a.tracts]
    catchment = TractTable({r["tract_geoid"]: r for r in near}, table.source)
    rng, cases, bad, i = random.Random(a.seed), [], 0, 0
    while len(cases) < a.n and i < a.n * 3:
        i += 1
        case = generate(rng, 10000 + i, catchment)
        if validate_case(case):
            bad += 1
            continue
        cases.append(case)
    if a.out.exists():
        shutil.rmtree(a.out)
    for k, case in enumerate(cases):
        d = a.out / f"s{k % a.streams}" / f"c{(k // a.streams) // a.chunk}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{case['case_id']}.yaml").write_text(yaml.safe_dump(case, sort_keys=False, allow_unicode=True))
    per = Counter(c["patient"]["tract_geoid"] for c in cases)
    far = km(home["intpt_lat"], home["intpt_lon"], near[-1]["intpt_lat"], near[-1]["intpt_lon"])
    print(f"wrote {len(cases)} patients to {a.out} ({bad} failed validation and were skipped)")
    print(f"catchment: {len(near)} tracts within {far:.1f} km of tract {a.clinic_tract}; tracts used {len(per)}; "
          f"patients per tract min {min(per.values())}, median {sorted(per.values())[len(per) // 2]}, max {max(per.values())}; "
          f"tracts under 5 patients: {sum(1 for v in per.values() if v < 5)}")
    print("conditions:", dict(Counter(c["title"] for c in cases).most_common(8)))
    print("planted barriers per patient:", dict(sorted(Counter(len(c["planted_conflicts"]) for c in cases).items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
