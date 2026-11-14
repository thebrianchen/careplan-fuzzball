"""Community resources -> data/processed/resources.csv (the resource table box 1 serves).

Two inputs, merged:
  1. Open data (downloaded by `python -m pipeline.download`): City of Chicago warming/cooling
     centers and libraries, HRSA health center sites, USDA SNAP-authorized grocery stores.
  2. Optional hand-curated rows in data/curated/resources.csv (e.g. food pantries copied from
     their own websites); rows without lat/lon are geocoded.
Every row keeps its source URL and a content hash. Hours that can't be parsed are left empty and
shown as "hours not listed", never guessed.

    python -m pipeline.build_resources
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import zipfile
from string import capwords
from pathlib import Path

import requests

from careplan import DAYS, NEEDS, ROOT
from careplan.data import haversine_km
from .geocode import geocode_address
from .hours import format_schedule, parse_hours
from .sources import SOURCES

RAW = ROOT / "data" / "raw"
CURATED = ROOT / "data" / "curated" / "resources.csv"
OUT = ROOT / "data" / "processed" / "resources.csv"
FIELDS = ["record_id", "name", "need", "kind", "address", "lat", "lon", "days", "open", "close", "schedule",
          "eligibility", "phone", "source_url", "source_hash", "untrusted_notes"]
COUNTY = "17031"
SNAP_GROCERY_TYPES = {"Super Store", "Supermarket", "Large Grocery Store", "Medium Grocery Store",
                      "Farmers' Market", "Fruits/Veg Specialty"}


def row_hash(r: dict) -> str:
    key = "|".join(str(r.get(k, "")) for k in ("name", "address", "days", "open", "close", "schedule",
                                                "eligibility", "source_url"))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _row(rid, name, need, kind, address, lat, lon, hours_text, eligibility, phone, url) -> dict:
    sched = parse_hours(hours_text)
    return {"record_id": rid, "name": " ".join(name.split()), "need": need, "kind": kind, "address": address,
            "lat": round(float(lat), 6), "lon": round(float(lon), 6), "days": "", "open": "", "close": "",
            "schedule": format_schedule(sched), "eligibility": eligibility, "phone": phone or "",
            "source_url": url, "untrusted_notes": ""}


def city_centers(path: Path, prefix: str, kind: str, eligibility: str, skip_types: set[str] = frozenset()) -> list[dict]:
    page = SOURCES["chi_warming" if kind == "warming_center" else "chi_cooling"]["page"]
    out = []
    for i, x in enumerate(json.loads(path.read_text()), start=1):
        loc = (x.get("location") or {}).get("coordinates")
        if not loc or x.get("site_type") in skip_types:
            continue
        out.append(_row(f"{prefix}{i:03d}", f"{x.get('site_name', '')} ({x.get('site_type', '')})", "utilities", kind,
                        f"{x.get('address', '')}, Chicago, IL {x.get('zip', '')}".strip(), loc[1], loc[0],
                        x.get("hours_of_operation"), eligibility, x.get("phone"), page))
    return out


def libraries(path: Path) -> list[dict]:
    out = []
    for i, x in enumerate(json.loads(path.read_text()), start=1):
        loc = x.get("location") or {}
        if not loc.get("latitude") or "further notice" in (x.get("service_hours") or "").lower():
            continue
        out.append(_row(f"LIB{i:03d}", f"Chicago Public Library, {x.get('branch_', '')} branch", "digital", "library",
                        f"{x.get('address', '')}, Chicago, IL {x.get('zip', '')}", loc["latitude"], loc["longitude"],
                        x.get("service_hours"), "Public library branch, open to the public",
                        x.get("phone"), (x.get("website") or {}).get("url") or SOURCES["chi_libraries"]["page"]))
    return out


def health_centers(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8-sig", errors="replace") as f:
        for i, x in enumerate(csv.DictReader(f), start=1):
            if (x.get("State and County Federal Information Processing Standard Code") != COUNTY
                    or x.get("Site Status Description") != "Active"
                    or not x.get("Geocoding Artifact Address Primary Y Coordinate")):
                continue
            kind = "fqhc_lookalike" if "Look-Alike" in x.get("Health Center Type", "") else "fqhc"
            hrs = x.get("Operating Hours per Week", "").split(".")[0]
            out.append(_row(f"HC{i:05d}", x["Site Name"], "med_cost", kind,
                            f"{x['Site Address']}, {x['Site City']}, IL {x['Site Postal Code'][:5]}",
                            x["Geocoding Artifact Address Primary Y Coordinate"],
                            x["Geocoding Artifact Address Primary X Coordinate"], None,
                            f"Federally funded health center ({capwords(x.get('Health Center Name', ''))}): sliding-fee "
                            f"discounts by income, serves uninsured patients. Open ~{hrs} h/week; hours not listed.",
                            x.get("Site Telephone Number"), x.get("Site Web Address") or SOURCES["hrsa_sites"]["page"]))
    return out


def snap_groceries(path: Path) -> list[dict]:
    out = []
    with zipfile.ZipFile(path) as z:
        f = io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8-sig", errors="replace")
        for x in csv.DictReader(f):
            if (x.get("State") != "IL" or not x.get("County", "").upper().startswith("COOK")
                    or x.get("End Date", "").strip() or x.get("Store Type") not in SNAP_GROCERY_TYPES
                    or not x.get("Latitude")):
                continue
            addr = " ".join(f"{x['Street Number']} {x['Street Name']}".split())
            out.append(_row(f"SNAP{x['Record ID']}", capwords(x["Store Name"]), "food", "snap_grocery",
                            f"{addr}, {capwords(x['City'])}, IL {x['Zip Code']}", x["Latitude"], x["Longitude"], None,
                            f"{x['Store Type']} authorized to accept SNAP (EBT)", "", SOURCES["snap_retailers"]["page"]))
    return out


def plentiful_pantries(path: Path) -> list[dict]:
    """Food pantries from Plentiful's public directory (see pipeline/pantries.py). No hours."""
    if not path.exists():
        return []
    out = []
    for p in json.loads(path.read_text())["pantries"]:
        slug = p["url"].rstrip("/").rsplit("-", 1)[-1]
        r = _row(f"PL{slug}", p["name"], "food", "food_pantry", p["address"], p["lat"], p["lon"], None,
                 "Food pantry in Plentiful's public directory (verified by food banks and community "
                 "organizations); hours and reservations in the Plentiful app", "", p["url"])
        r["untrusted_notes"] = p.get("description", "")
        out.append(r)
    return out


def open_data(raw: Path = RAW) -> list[dict]:
    rows = []
    rows += city_centers(raw / "chi_warming_centers.json", "WARM", "warming_center",
                         "City warming center (winter); open to the public")
    rows += city_centers(raw / "chi_cooling_centers.json", "COOL", "cooling_center",
                         "City cooling center (summer); open to the public", {"Park District Spray Feature"})
    rows += libraries(raw / "chi_libraries.json")
    rows += health_centers(raw / "hrsa_health_center_sites.csv")
    rows += snap_groceries(raw / "snap_retailers_2005_2025.zip")
    rows += plentiful_pantries(raw / "plentiful_chicago.json")
    return rows


def curated(path: Path, problems: list[str]) -> list[dict]:
    if not path.exists():
        return []
    s, out = requests.Session(), []
    with path.open(newline="") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            if r.get("record_id", "").startswith("#"):
                continue
            if r["need"] not in NEEDS:
                problems.append(f"curated line {i}: need {r['need']!r} not in {NEEDS}")
                continue
            bad_days = [d for d in r.get("days", "").split(",") if d and d not in DAYS]
            if bad_days:
                problems.append(f"curated line {i}: days {bad_days} must be {DAYS}")
            if not r.get("lat") and r.get("address"):
                try:
                    g = geocode_address(r["address"], s)
                    if g.get("matched"):
                        r["lat"], r["lon"] = g["lat"], g["lon"]
                except requests.RequestException as e:
                    problems.append(f"curated line {i}: geocode failed: {e}")
            if not r.get("lat"):
                problems.append(f"curated line {i}: no location for {r['name']!r}; skipped")
                continue
            out.append({k: r.get(k, "") for k in FIELDS})
    return out


def drop_superseded(open_rows: list[dict], cur_rows: list[dict], within_km: float = 0.06) -> list[dict]:
    """A curated row (with hours) replaces an open-data row of the same need at the same spot."""
    def near(a, b):
        return a["need"] == b["need"] and haversine_km(float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"])) <= within_km
    return [r for r in open_rows if not any(near(r, c) for c in cur_rows)]


def build(raw: Path = RAW, curated_path: Path = CURATED) -> tuple[list[dict], list[str]]:
    problems: list[str] = []
    cur = curated(curated_path, problems)
    rows = drop_superseded(open_data(raw), cur) + cur
    seen = set()
    for r in rows:
        if r["record_id"] in seen:
            problems.append(f"duplicate record_id {r['record_id']}")
        seen.add(r["record_id"])
        r["source_hash"] = row_hash(r)
    return rows, problems


def main() -> int:
    try:
        rows, problems = build()
    except FileNotFoundError as e:
        print(f"missing raw file: {e.filename}\nrun `python -m pipeline.download` first", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    for p in problems:
        print("WARN", p, file=sys.stderr)
    by = {}
    for r in rows:
        by[(r["need"], r["kind"])] = by.get((r["need"], r["kind"]), 0) + 1
    for (need, kind), n in sorted(by.items()):
        print(f"  {need:10} {kind:16} {n:5}")
    print(f"wrote {len(rows)} resources to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
