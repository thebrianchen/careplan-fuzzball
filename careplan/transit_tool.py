"""get_transit_trip: can the patient reach an appointment's facility by transit in time?

Runs on box 2 against its local copy of the CTA/Pace schedules (public data from the civic bundle),
so the patient's home and appointment never cross the boundary. The appointment must carry a
facility_ccn (CMS certification number); coordinates come from data/processed/dialysis_chicago.csv.
Evidence ref: "transit:<appt ref>", which supports the transport barrier when no departure from
the home tract arrives on time, or the on-time trip takes an hour or more or leaves before 5 AM.
"""
from __future__ import annotations

import csv
from functools import lru_cache

from . import ROOT

FACILITIES = ROOT / "data" / "processed" / "dialysis_chicago.csv"
LATEST_START_S = 3 * 3600


@lru_cache(maxsize=1)
def _router():
    from pipeline.transit import CACHE, Router
    return Router(CACHE) if CACHE.exists() else None


@lru_cache(maxsize=1)
def _facilities() -> dict[str, dict]:
    return {r["ccn"]: r for r in csv.DictReader(FACILITIES.open())} if FACILITIES.exists() else {}


@lru_cache(maxsize=256)
def trip(home_lat: float, home_lon: float, ccn: str, arrive_by: str) -> dict:
    from pipeline.transit import hhmm
    fac, r = _facilities().get(ccn), _router()
    if fac is None or r is None:
        return {"error": "transit data or facility location not available"}
    h, m = map(int, arrive_by.split(":"))
    last = r.latest_departure(home_lat, home_lon, float(fac["lat"]), float(fac["lon"]), h * 3600 + m * 60)
    first = r.earliest_arrival(home_lat, home_lon, float(fac["lat"]), float(fac["lon"]), LATEST_START_S)
    feasible = last.get("depart_s") is not None
    return {"facility": fac["name"], "arrive_by": arrive_by, "feasible": feasible,
            "latest_departure": hhmm(last.get("depart_s")) if feasible else None,
            "arrival": hhmm(last.get("arrival_s")) if feasible else None,
            "trip_minutes": round((last["arrival_s"] - last["depart_s"]) / 60) if feasible else None,
            "earliest_possible_arrival": hhmm(first.get("arrival_s")),
            "assumes": "weekday CTA+Pace schedules, walking up to 800 m each end, transfers within 250 m"}


@lru_cache(maxsize=256)
def rides(home_lat: float, home_lon: float, ccn: str, arrive_by: str) -> list[dict]:
    """The rides of the trip that `trip` reports, for drawing it: board and alight times and the stops each ride passes.
    Empty when there is no on-time trip or it is a walk."""
    from pipeline.transit import hhmm
    found = trip(home_lat, home_lon, ccn, arrive_by)
    if not found.get("feasible"):
        return []
    fac = _facilities()[ccn]
    h, m = map(int, found["latest_departure"].split(":"))
    journey = _router().journey(home_lat, home_lon, float(fac["lat"]), float(fac["lon"]), h * 3600 + m * 60)
    return [{"board": hhmm(r["board_s"]), "alight": hhmm(r["alight_s"]), "path": r["path"]} for r in (journey or {}).get("rides", [])]


def supports_transport(case, appt_ref: str, tract_row: dict | None) -> bool | None:
    """Scoring: True if the record + schedules show no on-time transit trip; None if unverifiable."""
    appt = case.refs.get(appt_ref)
    if not appt or not appt.get("facility_ccn") or not appt.get("time") or tract_row is None:
        return None
    res = trip(tract_row["intpt_lat"], tract_row["intpt_lon"], appt["facility_ccn"], appt["time"])
    if "error" in res:
        return None
    # No on-time trip, or one that means a ride over an hour or leaving before 5 AM, three days a week.
    return (not res["feasible"]) or res["trip_minutes"] >= 60 or res["latest_departure"] < "05:00"
