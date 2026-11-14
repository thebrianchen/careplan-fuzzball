"""Join the raw sources into one row per 2020 census tract: data/processed/tracts.csv.

Columns: tract_geoid, intpt_lat, intpt_lon, no_vehicle_share, households, transit_stops_800m,
early_service_stops, lila_1_10, lila_half_10, low_income, places_* (whatever PLACES has at tract
level), data_vintage.

    python -m pipeline.build_tracts
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from careplan import ROOT
from .sources import ACS_YEAR, COUNTY_FIPS, STATE_FIPS

RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed" / "tracts.csv"
EARLY_CUTOFF = "06:00:00"
RADIUS_KM = 0.8
PLACES_MEASURES = {          # PLACES measure id -> our column, when present at tract level
    "lacktrpt": "places_lacktrpt", "foodinsecu": "places_foodinsecu", "shututility": "places_shututility",
    "housinsecu": "places_housinsecu", "foodstamp": "places_foodstamp", "loneliness": "places_loneliness",
    "csmoking": "places_csmoking", "diabetes": "places_diabetes", "kidney": "places_kidney",
    "bphigh": "places_bphigh", "access2": "places_access2",
}


# ---------- parsers (each takes raw bytes/paths and returns a DataFrame keyed by tract_geoid)

def parse_gazetteer(path: Path, counties: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path, sep="\t", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df["tract_geoid"] = df["GEOID"].str.strip().str.zfill(11)
    df = df[df["tract_geoid"].str[:5].isin(counties)]
    return pd.DataFrame({"tract_geoid": df["tract_geoid"],
                         "intpt_lat": df["INTPTLAT"].astype(float),
                         "intpt_lon": df["INTPTLONG"].astype(float)})


def parse_acs(paths: list[Path]) -> pd.DataFrame:
    frames = []
    for p in paths:
        rows = json.loads(p.read_text())
        df = pd.DataFrame(rows[1:], columns=rows[0])
        df["tract_geoid"] = df["state"] + df["county"] + df["tract"]
        hh = pd.to_numeric(df["B08201_001E"], errors="coerce")
        nov = pd.to_numeric(df["B08201_002E"], errors="coerce")
        frames.append(pd.DataFrame({"tract_geoid": df["tract_geoid"], "households": hh,
                                    "no_vehicle_share": (nov / hh.where(hh > 0)).round(3)}))
    return pd.concat(frames, ignore_index=True)


def _read_fara_table(path: Path) -> pd.DataFrame:
    cols = ["CensusTract", "LILATracts_1And10", "LILATracts_halfAnd10", "LowIncomeTracts"]
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            csvs = [n for n in names if n.lower().endswith(".csv")]
            if csvs:
                return pd.read_csv(z.open(csvs[0]), dtype={"CensusTract": str}, usecols=lambda c: c in cols)
            xlsx = [n for n in names if n.lower().endswith(".xlsx")]
            if not xlsx:
                raise ValueError(f"no csv/xlsx in {path}: {names}")
            data = io.BytesIO(z.read(xlsx[0]))
    else:
        data = path
    sheets = pd.read_excel(data, sheet_name=None, dtype={"CensusTract": str})
    for df in sheets.values():
        if "CensusTract" in df.columns:
            return df[[c for c in cols if c in df.columns]]
    raise ValueError("no sheet with a CensusTract column")


def parse_fara(path: Path) -> pd.DataFrame:
    df = _read_fara_table(path)
    df["tract10"] = df["CensusTract"].astype(str).str.zfill(11)
    return df.rename(columns={"LILATracts_1And10": "lila_1_10", "LILATracts_halfAnd10": "lila_half_10",
                              "LowIncomeTracts": "low_income"})[["tract10", "lila_1_10", "lila_half_10", "low_income"]]


def parse_relationship(path: Path, counties: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path, sep="|", dtype=str)
    need = {"GEOID_TRACT_20", "GEOID_TRACT_10", "AREALAND_PART"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"relationship file missing columns {missing}; got {list(df.columns)[:8]}...")
    df = df[df["GEOID_TRACT_20"].str[:5].isin(counties)]
    df["w"] = pd.to_numeric(df["AREALAND_PART"], errors="coerce").fillna(0)
    return df[["GEOID_TRACT_20", "GEOID_TRACT_10", "w"]].rename(
        columns={"GEOID_TRACT_20": "tract_geoid", "GEOID_TRACT_10": "tract10"})


def crosswalk_fara(fara: pd.DataFrame, rel: pd.DataFrame) -> pd.DataFrame:
    """2010-tract flags onto 2020 tracts: area-weighted share of flagged land; flag when > 0.5."""
    m = rel.merge(fara, on="tract10", how="left")
    out = []
    for geoid, g in m.groupby("tract_geoid"):
        row = {"tract_geoid": geoid}
        tot = g["w"].sum()
        for col in ("lila_1_10", "lila_half_10", "low_income"):
            vals = pd.to_numeric(g[col], errors="coerce")
            ok = vals.notna()
            if not ok.any() or tot <= 0:
                row[col] = None
                continue
            share = (vals[ok] * g["w"][ok]).sum() / g["w"][ok].sum()
            row[col] = int(share > 0.5)
        out.append(row)
    return pd.DataFrame(out)


def parse_places(path: Path, counties: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.lower() for c in df.columns]
    df["tract_geoid"] = df["tractfips"].str.zfill(11)
    df = df[df["tract_geoid"].str[:5].isin(counties)]
    out = pd.DataFrame({"tract_geoid": df["tract_geoid"]})
    for mid, col in PLACES_MEASURES.items():
        src = f"{mid}_crudeprev"
        if src in df.columns:
            out[col] = pd.to_numeric(df[src], errors="coerce")
    return out


def _gtfs_stops(zip_path: Path) -> pd.DataFrame:
    """Stops with a flag for weekday departures before EARLY_CUTOFF."""
    with zipfile.ZipFile(zip_path) as z:
        stops = pd.read_csv(z.open("stops.txt"), dtype=str)
        trips = pd.read_csv(z.open("trips.txt"), dtype=str, usecols=["trip_id", "service_id"])
        weekday_services = set()
        if "calendar.txt" in z.namelist():
            cal = pd.read_csv(z.open("calendar.txt"), dtype=str)
            wk = cal[[d for d in ("monday", "tuesday", "wednesday", "thursday", "friday")]].astype(int).sum(axis=1) >= 3
            weekday_services = set(cal.loc[wk, "service_id"])
        else:  # calendar_dates-only feeds: treat every service as possible
            weekday_services = set(trips["service_id"])
        st = pd.read_csv(z.open("stop_times.txt"), dtype=str, usecols=["trip_id", "stop_id", "departure_time"])
    st = st.merge(trips, on="trip_id")
    st = st[st["service_id"].isin(weekday_services)]
    dep = st["departure_time"].str.strip().str.zfill(8)
    early = set(st.loc[(dep < EARLY_CUTOFF) & (dep >= "04:00:00"), "stop_id"])
    stops = stops[stops.get("location_type", pd.Series(["0"] * len(stops))).fillna("0").isin(["0", ""])]
    return pd.DataFrame({"stop_id": stops["stop_id"], "lat": stops["stop_lat"].astype(float),
                         "lon": stops["stop_lon"].astype(float), "early": stops["stop_id"].isin(early)})


def transit_counts(centroids: pd.DataFrame, stops: pd.DataFrame, radius_km: float = RADIUS_KM) -> pd.DataFrame:
    lat1 = np.radians(centroids["intpt_lat"].to_numpy())[:, None]
    lon1 = np.radians(centroids["intpt_lon"].to_numpy())[:, None]
    lat2 = np.radians(stops["lat"].to_numpy())[None, :]
    lon2 = np.radians(stops["lon"].to_numpy())[None, :]
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    within = 2 * 6371.0 * np.arcsin(np.sqrt(a)) <= radius_km
    early = stops["early"].to_numpy()[None, :]
    return pd.DataFrame({"tract_geoid": centroids["tract_geoid"].to_numpy(),
                         "transit_stops_800m": within.sum(axis=1),
                         "early_service_stops": (within & early).sum(axis=1)})


# ---------- main

def build(raw: Path = RAW, counties: list[str] = COUNTY_FIPS) -> pd.DataFrame:
    base = parse_gazetteer(raw / f"2020_gaz_tracts_{STATE_FIPS}.txt", counties)
    acs = parse_acs([raw / f"acs_b08201_{c}.json" for c in counties])
    fara = crosswalk_fara(parse_fara(raw / "fara_2019.zip"), parse_relationship(raw / "tab20_tract20_tract10_natl.txt", counties))
    places = parse_places(raw / "places_tract_il.csv", counties)
    stops = pd.concat([_gtfs_stops(raw / f) for f in ("gtfs_cta.zip", "gtfs_pace.zip") if (raw / f).exists()], ignore_index=True)
    transit = transit_counts(base, stops)
    df = base.merge(acs, on="tract_geoid", how="left").merge(transit, on="tract_geoid", how="left") \
             .merge(fara, on="tract_geoid", how="left").merge(places, on="tract_geoid", how="left")
    df["data_vintage"] = (f"ACS {ACS_YEAR} 5-yr; USDA FARA 2019 (2010 tracts, area-crosswalked); "
                          f"CDC PLACES 2025; GTFS CTA+Pace; Gazetteer 2020")
    return df


def main() -> int:
    try:
        df = build()
    except FileNotFoundError as e:
        print(f"missing raw file: {e.filename}\nrun `python -m pipeline.download` first", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"wrote {len(df)} tracts to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
