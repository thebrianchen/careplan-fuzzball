"""Can the patient get there by transit in time? Earliest arrival over CTA + Pace weekday schedules.

Connection Scan Algorithm over one weekday's trips (default Wednesday), with walking at both ends
(<= 800 m at 1.2 m/s) and transfers between stops within 250 m. This is the evidence the hero's
transport flag really needs ("no early bus that reaches the center"), and on the event hardware it
is box 1's batch job: a tract x facility arrival matrix computed nightly and shipped to box 2.

    python -m pipeline.transit build                       # -> data/processed/transit_wed.npz (cached)
    python -m pipeline.transit trip --tract 17031260300 --to-lat 41.86 --to-lon -87.69 --arrive-by 06:00
"""
from __future__ import annotations

import argparse
import io
import sys
import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from careplan import ROOT

RAW = ROOT / "data" / "raw"
CACHE = ROOT / "data" / "processed" / "transit_wed.npz"
WALK_MPS, MAX_WALK_M, TRANSFER_M = 1.2, 800.0, 250.0
WINDOW = (3 * 3600, 23 * 3600)          # 03:00-23:00: early, afternoon and evening trips


def _secs(s: pd.Series) -> np.ndarray:
    hms = s.str.strip().str.split(":", expand=True).astype(int)
    return (hms[0] * 3600 + hms[1] * 60 + hms[2]).to_numpy()


def _feed(path: Path, prefix: str, weekday: str):
    with zipfile.ZipFile(path) as z:
        cal = pd.read_csv(z.open("calendar.txt"), dtype=str)
        stops = pd.read_csv(z.open("stops.txt"), dtype=str, usecols=["stop_id", "stop_lat", "stop_lon"])
        trips = pd.read_csv(z.open("trips.txt"), dtype=str, usecols=["trip_id", "service_id"])
        today = date.today().strftime("%Y%m%d")
        live = cal[(cal[weekday] == "1") & (cal["start_date"] <= today) & (cal["end_date"] >= today)]
        if live.empty:                                   # feed not current: use any service on that weekday
            live = cal[cal[weekday] == "1"]
        trip_ids = set(trips.loc[trips["service_id"].isin(set(live["service_id"])), "trip_id"])
        parts = []
        for chunk in pd.read_csv(io.TextIOWrapper(z.open("stop_times.txt")), dtype=str, chunksize=1_000_000,
                                 usecols=["trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"]):
            chunk = chunk[chunk["trip_id"].isin(trip_ids)]
            parts.append(chunk)
    st = pd.concat(parts)
    st["seq"] = st["stop_sequence"].astype(int)
    st["arr"], st["dep"] = _secs(st["arrival_time"]), _secs(st["departure_time"])
    st = st.sort_values(["trip_id", "seq"])
    nxt = st.groupby("trip_id").shift(-1)
    conn = pd.DataFrame({"trip": prefix + st["trip_id"], "from": prefix + st["stop_id"], "to": prefix + nxt["stop_id"],
                         "dep": st["dep"], "arr": nxt["arr"]}).dropna()
    conn = conn[(conn["dep"] >= WINDOW[0]) & (conn["dep"] <= WINDOW[1])]
    stops["stop_id"] = prefix + stops["stop_id"]
    return conn, stops


def build(weekday: str = "wednesday") -> Path:
    conns, stops = zip(*[_feed(RAW / f, p, weekday) for f, p in (("gtfs_cta.zip", "C:"), ("gtfs_pace.zip", "P:"))])
    conn, stops = pd.concat(conns), pd.concat(stops).drop_duplicates("stop_id")
    ids = {s: i for i, s in enumerate(stops["stop_id"])}
    conn = conn[conn["from"].isin(ids) & conn["to"].isin(ids)].sort_values("dep")
    trips = {t: i for i, t in enumerate(conn["trip"].unique())}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(CACHE, frm=conn["from"].map(ids).to_numpy(np.int32), to=conn["to"].map(ids).to_numpy(np.int32),
                        dep=conn["dep"].to_numpy(np.int32), arr=conn["arr"].astype(int).to_numpy(np.int32),
                        trip=conn["trip"].map(trips).to_numpy(np.int32),
                        lat=stops["stop_lat"].astype(float).to_numpy(), lon=stops["stop_lon"].astype(float).to_numpy())
    print(f"{len(conn):,} connections, {len(ids):,} stops, {len(trips):,} trips ({weekday}) -> {CACHE}")
    return CACHE


def _dist_m(lat, lon, lats, lons):
    p1, p2 = np.radians(lat), np.radians(lats)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lons - lon) / 2) ** 2
    return 2 * 6371000 * np.arcsin(np.sqrt(a))


class Router:
    def __init__(self, path: Path = CACHE):
        d = np.load(path)
        self.frm, self.to, self.dep, self.arr, self.trip = d["frm"], d["to"], d["dep"], d["arr"], d["trip"]
        self.lat, self.lon = d["lat"], d["lon"]
        self._xfer: dict[int, tuple] = {}

    def _transfers(self, s: int):
        if s not in self._xfer:
            dm = _dist_m(self.lat[s], self.lon[s], self.lat, self.lon)
            near = np.nonzero(dm <= TRANSFER_M)[0]
            self._xfer[s] = (near, (dm[near] / WALK_MPS).astype(int))
        return self._xfer[s]

    def earliest_arrival(self, olat, olon, dlat, dlon, depart_s: int) -> dict:
        """Earliest arrival (seconds after midnight) at the destination leaving the origin at depart_s."""
        best = np.full(len(self.lat), np.iinfo(np.int32).max, dtype=np.int64)
        on_trip = np.zeros(self.trip.max() + 1, dtype=bool)
        do = _dist_m(olat, olon, self.lat, self.lon)
        for s in np.nonzero(do <= MAX_WALK_M)[0]:
            best[s] = depart_s + int(do[s] / WALK_MPS)
        dd = _dist_m(dlat, dlon, self.lat, self.lon)
        targets = np.nonzero(dd <= MAX_WALK_M)[0]
        walk_only = depart_s + int(_dist_m(olat, olon, np.array([dlat]), np.array([dlon]))[0] / WALK_MPS)
        target_best = walk_only if walk_only - depart_s <= MAX_WALK_M * 2 / WALK_MPS else np.iinfo(np.int32).max
        start = np.searchsorted(self.dep, depart_s)
        for i in range(start, len(self.dep)):
            if self.dep[i] >= target_best:
                break
            t = self.trip[i]
            if on_trip[t] or best[self.frm[i]] <= self.dep[i]:
                on_trip[t] = True
                a, s = self.arr[i], self.to[i]
                if a < best[s]:
                    best[s] = a
                    near, secs = self._transfers(s)
                    np.minimum.at(best, near, a + secs)
        if len(targets):
            target_best = min(target_best, int(np.min(best[targets] + (dd[targets] / WALK_MPS).astype(int))))
        ok = target_best < np.iinfo(np.int32).max
        return {"arrival_s": int(target_best) if ok else None, "minutes": round((target_best - depart_s) / 60) if ok else None,
                "origin_stops_in_walk": int((do <= MAX_WALK_M).sum()), "dest_stops_in_walk": int(len(targets))}

    def one_to_all(self, olat, olon, depart_s: int, until_s: int) -> np.ndarray:
        """Earliest arrival at every stop leaving the origin at depart_s (scan stops at until_s)."""
        best = np.full(len(self.lat), np.iinfo(np.int32).max, dtype=np.int64)
        on_trip = np.zeros(self.trip.max() + 1, dtype=bool)
        do = _dist_m(olat, olon, self.lat, self.lon)
        for s in np.nonzero(do <= MAX_WALK_M)[0]:
            best[s] = depart_s + int(do[s] / WALK_MPS)
        for i in range(np.searchsorted(self.dep, depart_s), len(self.dep)):
            if self.dep[i] > until_s:
                break
            t = self.trip[i]
            if on_trip[t] or best[self.frm[i]] <= self.dep[i]:
                on_trip[t] = True
                a, s = self.arr[i], self.to[i]
                if a < best[s]:
                    best[s] = a
                    near, secs = self._transfers(s)
                    np.minimum.at(best, near, a + secs)
        return best

    def arrival_at(self, best: np.ndarray, dlat, dlon) -> int | None:
        dd = _dist_m(dlat, dlon, self.lat, self.lon)
        near = np.nonzero(dd <= MAX_WALK_M)[0]
        if not len(near):
            return None
        v = int(np.min(best[near] + (dd[near] / WALK_MPS).astype(int)))
        return v if v < np.iinfo(np.int32).max else None

    def latest_departure(self, olat, olon, dlat, dlon, arrive_by_s: int, step_s: int = 300) -> dict:
        """Latest departure (on a 5-minute grid from 03:00) that still arrives by arrive_by_s. Arrival time
        is non-decreasing in departure time, so binary search: ~8 route searches instead of up to 144."""
        grid = list(range(WINDOW[0], arrive_by_s, step_s))
        ok = lambda dep: (r := self.earliest_arrival(olat, olon, dlat, dlon, dep))["arrival_s"] is not None \
            and r["arrival_s"] <= arrive_by_s and r
        lo, hi, found = 0, len(grid) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            r = ok(grid[mid])
            if r:
                found, lo = {"depart_s": grid[mid], **r}, mid + 1
            else:
                hi = mid - 1
        return found or {"depart_s": None, "arrival_s": None}

    def journey(self, olat, olon, dlat, dlon, depart_s: int) -> dict | None:
        """The rides of the earliest-arrival trip that leaves the origin at depart_s: for each ride its board and alight
        times and the stops it passes, in order. The same scan as earliest_arrival, remembering how each stop was reached.
        None when no ride reaches the destination."""
        inf = np.iinfo(np.int32).max
        best = np.full(len(self.lat), inf, dtype=np.int64)
        reached: dict[int, tuple] = {}            # stop -> ("ride", trip, its last connection) or ("walk", from stop)
        boarded: dict[int, int] = {}              # trip -> the connection where it was boarded
        do = _dist_m(olat, olon, self.lat, self.lon)
        for s in np.nonzero(do <= MAX_WALK_M)[0]:
            best[s] = depart_s + int(do[s] / WALK_MPS)
        dd = _dist_m(dlat, dlon, self.lat, self.lon)
        targets = np.nonzero(dd <= MAX_WALK_M)[0]
        if not len(targets):
            return None
        last_walk = (dd[targets] / WALK_MPS).astype(int)
        arrival = inf
        for i in range(np.searchsorted(self.dep, depart_s), len(self.dep)):
            if self.dep[i] >= arrival:
                break
            t = int(self.trip[i])
            if t in boarded or best[self.frm[i]] <= self.dep[i]:
                boarded.setdefault(t, i)
                a, s = int(self.arr[i]), int(self.to[i])
                if a < best[s]:
                    best[s], reached[s] = a, ("ride", t, i)
                    near, secs = self._transfers(s)
                    for n, sec in zip(near.tolist(), secs.tolist()):
                        if a + sec < best[n]:
                            best[n], reached[n] = a + sec, ("walk", s)
                    arrival = int(np.min(best[targets] + last_walk))
        s = int(targets[np.argmin(best[targets] + last_walk)])
        rides = []
        while s in reached:
            how = reached[s]
            if how[0] == "walk":
                s = how[1]
                continue
            _, t, last = how
            first = boarded[t]
            conns = first + np.nonzero(self.trip[first:last + 1] == t)[0]
            stops = [int(self.frm[conns[0]]), *self.to[conns].tolist()]
            rides.append({"board_s": int(self.dep[first]), "alight_s": int(self.arr[last]),
                          "path": [[round(float(self.lat[k]), 5), round(float(self.lon[k]), 5)] for k in stops]})
            s = int(self.frm[first])
        return {"arrival_s": arrival, "rides": rides[::-1]} if rides else None


def hhmm(s: int | None) -> str:
    return "none" if s is None else f"{s // 3600:02d}:{s % 3600 // 60:02d}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--weekday", default="wednesday")
    t = sub.add_parser("trip")
    t.add_argument("--tract")
    t.add_argument("--from-lat", type=float)
    t.add_argument("--from-lon", type=float)
    t.add_argument("--to-lat", type=float, required=True)
    t.add_argument("--to-lon", type=float, required=True)
    t.add_argument("--arrive-by", default="06:00")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        build(a.weekday)
        return 0
    if a.tract:
        from careplan.data import TractTable
        row = TractTable.load().get(a.tract)
        a.from_lat, a.from_lon = row["intpt_lat"], row["intpt_lon"]
    h, m = map(int, a.arrive_by.split(":"))
    r = Router()
    first = r.earliest_arrival(a.from_lat, a.from_lon, a.to_lat, a.to_lon, WINDOW[0])
    last = r.latest_departure(a.from_lat, a.from_lon, a.to_lat, a.to_lon, h * 3600 + m * 60)
    print(f"earliest possible arrival (leaving 03:00): {hhmm(first['arrival_s'])}")
    print(f"latest departure arriving by {a.arrive_by}: {hhmm(last['depart_s'])} -> {hhmm(last['arrival_s'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
