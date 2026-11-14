"""Census Geocoder helpers: street address or lat/lon -> 2020 census tract GEOID.

    python -m pipeline.geocode cases            # writes data/processed/case_tracts.csv
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

import requests

from careplan import ROOT
from careplan.cases import load_cases
from .sources import GEOCODER_COORDS, GEOCODER_ONELINE, GEOCODER_PARAMS

OUT = ROOT / "data" / "processed" / "case_tracts.csv"


def _tract_from_geographies(geo: dict) -> str | None:
    for key in ("Census Tracts", "2020 Census Tracts"):
        if geo.get(key):
            return geo[key][0]["GEOID"]
    return None


def geocode_address(address: str, session: requests.Session | None = None) -> dict:
    s = session or requests.Session()
    r = s.get(GEOCODER_ONELINE, params={"address": address, **GEOCODER_PARAMS}, timeout=60)
    r.raise_for_status()
    matches = r.json().get("result", {}).get("addressMatches", [])
    if not matches:
        return {"matched": False}
    m = matches[0]
    return {"matched": True, "matched_address": m.get("matchedAddress"),
            "lat": m["coordinates"]["y"], "lon": m["coordinates"]["x"],
            "tract_geoid": _tract_from_geographies(m.get("geographies", {}))}


def tract_for_point(lat: float, lon: float, session: requests.Session | None = None) -> str | None:
    s = session or requests.Session()
    r = s.get(GEOCODER_COORDS, params={"x": lon, "y": lat, **GEOCODER_PARAMS}, timeout=60)
    r.raise_for_status()
    return _tract_from_geographies(r.json().get("result", {}).get("geographies", {}))


def main(argv=None) -> int:
    root = Path((argv or sys.argv[1:] or ["cases"])[0])
    cases = load_cases(root)
    s = requests.Session()
    rows = []
    for c in cases:
        try:
            g = geocode_address(c.patient["address"], s)
        except requests.RequestException as e:
            g = {"matched": False, "error": str(e)}
        rows.append({"case_id": c.case_id, "address": c.patient["address"], "matched": g.get("matched"),
                     "matched_address": g.get("matched_address", ""), "lat": g.get("lat", ""),
                     "lon": g.get("lon", ""), "tract_geoid": g.get("tract_geoid") or ""})
        print(f"{c.case_id}: {rows[-1]['tract_geoid'] or 'NO MATCH'}  {c.patient['address']}")
        time.sleep(0.2)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["case_id"])
        w.writeheader()
        w.writerows(rows)
    matched = sum(1 for r in rows if r["tract_geoid"])
    print(f"{matched}/{len(rows)} matched -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
