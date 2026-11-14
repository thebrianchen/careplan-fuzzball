"""Neighborhood tables: tract profiles and community resources (the public-zone data)."""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import DAYS, ROOT

DEFAULT_TRACTS = ROOT / "data" / "processed" / "tracts.csv"
DEFAULT_RESOURCES = ROOT / "data" / "processed" / "resources.csv"
FIXTURE_TRACTS = ROOT / "tests" / "fixtures" / "tracts_fixture.csv"
FIXTURE_RESOURCES = ROOT / "tests" / "fixtures" / "resources_fixture.csv"

KNOWN_HOURS_BOOST_KM = 3.0
TRACT_NUMERIC = ("no_vehicle_share", "transit_stops_800m", "early_service_stops", "lila_1_10",
                 "lila_half_10", "low_income", "intpt_lat", "intpt_lon")


def _num(v: str):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except ValueError:
        return v
    return int(f) if f.is_integer() and "." not in v else f


class TractTable:
    def __init__(self, rows: dict[str, dict], source: Path):
        self.rows = rows
        self.source = source

    @classmethod
    def load(cls, path: Path | None = None) -> "TractTable":
        path = path or (DEFAULT_TRACTS if DEFAULT_TRACTS.exists() else FIXTURE_TRACTS)
        rows = {}
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                geoid = r["tract_geoid"].zfill(11)
                rows[geoid] = {k: (_num(v) if k != "tract_geoid" else geoid) for k, v in r.items()}
        return cls(rows, path)

    def get(self, geoid: str | None) -> dict | None:
        if not geoid:
            return None
        return self.rows.get(str(geoid).zfill(11))

    @property
    def is_fixture(self) -> bool:
        return self.source == FIXTURE_TRACTS


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def read_schedule(text: str | None) -> dict[str, tuple[str, str]]:
    """'Mon 10:00-18:00;Tue 12:00-20:00' -> {'Mon': ('10:00', '18:00'), ...}"""
    out = {}
    for part in (text or "").split(";"):
        m = re.fullmatch(r"\s*(\w{3}) (\d\d:\d\d)-(\d\d:\d\d)\s*", part)
        if m and m.group(1) in DAYS:
            out[m.group(1)] = (m.group(2), m.group(3))
    return out


@dataclass
class Resource:
    record_id: str
    name: str
    need: str
    address: str
    lat: float
    lon: float
    days: list[str]
    open: str
    close: str
    eligibility: str
    phone: str
    source_url: str
    source_hash: str
    untrusted_notes: str
    kind: str = ""
    schedule: dict = field(default_factory=dict)   # {day: (open, close)} when hours differ by day

    @property
    def hours_known(self) -> bool:
        return bool(self.schedule or self.days)

    def open_during(self, days: list[str] | None, start: str | None, end: str | None) -> bool:
        """Unknown hours never filter a resource out; the record says the hours aren't listed."""
        if self.schedule:
            windows = [self.schedule[d] for d in (days or self.schedule) if d in self.schedule]
            if not windows:
                return False
            return not (start and end) or any(
                _minutes(o) <= _minutes(end) and _minutes(c) >= _minutes(start) for o, c in windows)
        if days and self.days and not set(days) & set(self.days):
            return False
        if start and end and self.open and self.close:
            return _minutes(self.open) <= _minutes(end) and _minutes(self.close) >= _minutes(start)
        return True

    def hours_text(self) -> str:
        if self.schedule:
            return "; ".join(f"{d} {o}-{c}" for d, (o, c) in self.schedule.items())
        if self.days:
            return f"{','.join(self.days)} {self.open}-{self.close}".strip()
        return "hours not listed; call ahead"


class ResourceTable:
    def __init__(self, resources: list[Resource], source: Path):
        self.resources = resources
        self.by_id = {r.record_id: r for r in resources}
        self.source = source

    @classmethod
    def load(cls, path: Path | None = None) -> "ResourceTable":
        path = path or (DEFAULT_RESOURCES if DEFAULT_RESOURCES.exists() else FIXTURE_RESOURCES)
        out = []
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                days = [d for d in (r.get("days") or "").split(",") if d in DAYS]
                out.append(Resource(
                    record_id=r["record_id"], name=r["name"], need=r["need"], address=r.get("address", ""),
                    lat=float(r["lat"]), lon=float(r["lon"]), days=days, open=r.get("open", ""),
                    close=r.get("close", ""), eligibility=r.get("eligibility", ""), phone=r.get("phone", ""),
                    source_url=r.get("source_url", ""), source_hash=r.get("source_hash", ""),
                    untrusted_notes=r.get("untrusted_notes", ""), kind=r.get("kind", ""),
                    schedule=read_schedule(r.get("schedule"))))
        return cls(out, path)

    def search(self, lat: float, lon: float, need: str, days: list[str] | None = None,
               start: str | None = None, end: str | None = None, limit: int = 5) -> list[dict]:
        hits = []
        for r in self.resources:
            if r.need != need or not r.open_during(days, start, end):
                continue
            hits.append((haversine_km(lat, lon, r.lat, r.lon), r))
        # With a day/time filter, nearby places confirmed open come before places whose hours aren't
        # listed; beyond KNOWN_HOURS_BOOST_KM, distance alone decides.
        filtered = bool(days or (start and end))
        hits.sort(key=lambda t: (not (filtered and t[1].hours_known and t[0] <= KNOWN_HOURS_BOOST_KM), t[0]))
        return [{
            "ref": f"resource:{r.record_id}", "record_id": r.record_id, "name": r.name, "need": r.need,
            "kind": r.kind, "address": r.address, "distance_miles": round(d * 0.621371, 2), "hours": r.hours_text(),
            "eligibility": r.eligibility, "phone": r.phone, "source_url": r.source_url,
            "untrusted_description": r.untrusted_notes,
        } for d, r in hits[:limit]]
