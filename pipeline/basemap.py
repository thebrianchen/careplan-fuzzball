"""The map under the clinic page: census tract outlines, streets, rail and water for the area around the panel's clinic.

Fetched once from public sources and kept in the repo, so the page draws a real map with no tile server and no network:
    python -m pipeline.basemap                      # -> data/processed/basemap_westside.json
    python -m pipeline.basemap --wide               # -> data/processed/basemap_chicago.json: the city around it, in less detail
Sources: U.S. Census Bureau TIGERweb (tract outlines, roads, railroads, water); City of Chicago Data Portal (the 77 community areas).
Points are [lat, lon], rounded to 5 places (about a metre).
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

from careplan import ROOT

OUT = ROOT / "data" / "processed" / "basemap_westside.json"
BOX = (41.847, -87.778, 41.921, -87.682)          # south, west, north, east: the panel's neighborhoods plus a margin
WIDE = ROOT / "data" / "processed" / "basemap_chicago.json"
WIDE_BOX = (41.64, -87.96, 42.03, -87.50)         # Chicago and the lakefront
WIDE_LAYERS = {"tracts": ("Tracts_Blocks/MapServer/0", "GEOID"), "highways": ("Transportation/MapServer/2", "NAME"),
               "arterials": ("Transportation/MapServer/6", "NAME"), "rail": ("Transportation/MapServer/9", "NAME"),
               "water": ("Hydro/MapServer/1", "NAME")}
COARSE = 0.0003                                   # about 30 m: invisible at city scale
AREAS = "https://data.cityofchicago.org/resource/igwz-8jzy.geojson"     # Chicago's 77 community areas: the neighborhoods people name
TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
LAYERS = {"tracts": ("Tracts_Blocks/MapServer/0", "GEOID"),
          "highways": ("Transportation/MapServer/2", "NAME"),
          "arterials": ("Transportation/MapServer/6", "NAME"),
          "streets": ("Transportation/MapServer/8", "NAME"),
          "rail": ("Transportation/MapServer/9", "NAME"),
          "water": ("Hydro/MapServer/1", "NAME")}


def _line(coords: list) -> list:
    return [[round(lat, 5), round(lon, 5)] for lon, lat, *_ in coords]


def _lines(geometry: dict | None) -> list[list]:
    """Every ring or line of a GeoJSON geometry as a list of [lat, lon] points."""
    if not geometry:
        return []
    kind, c = geometry["type"], geometry["coordinates"]
    parts = {"LineString": [c], "MultiLineString": c, "Polygon": c, "MultiPolygon": [ring for poly in c for ring in poly]}[kind]
    return [_line(p) for p in parts if len(p) >= 2]


def tiger(layer: str, field: str, box=BOX, coarse: float = 0) -> list[dict]:
    """`coarse` is how far, in degrees, a line may be moved to drop points: 0 keeps every point."""
    s, w, n, e = box
    out, offset = [], 0
    while True:
        r = requests.get(f"{TIGER}/{layer}/query", timeout=180, params={
            "geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            "outFields": field, "outSR": 4326, "geometryPrecision": 4 if coarse else 5, **({"maxAllowableOffset": coarse} if coarse else {}),
            "f": "geojson", "resultOffset": offset, "resultRecordCount": 2000})
        r.raise_for_status()
        feats = r.json().get("features", [])
        out += feats
        if len(feats) < 2000:
            return out
        offset += len(feats)


def build() -> dict:
    got = {name: tiger(layer, field) for name, (layer, field) in LAYERS.items()}
    tracts = {f["properties"]["GEOID"]: _lines(f["geometry"]) for f in got["tracts"]}
    roads = {name: [{"name": f["properties"].get("NAME") or "", "line": line} for f in got[name] for line in _lines(f["geometry"])]
             for name in ("highways", "arterials", "streets", "rail")}
    water = [{"name": f["properties"].get("NAME") or "", "rings": _lines(f["geometry"])} for f in got["water"] if f["geometry"]]
    return {"built": time.strftime("%Y-%m-%d"), "box": {"south": BOX[0], "west": BOX[1], "north": BOX[2], "east": BOX[3]},
            "source": "U.S. Census Bureau, TIGERweb: census tracts, roads, railroads and water (tigerweb.geo.census.gov)",
            "tracts": tracts, **roads, "water": water}


def _thin(line: list, tol: float) -> list:
    """Douglas-Peucker: drop the points of a line that lie within `tol` degrees of the line through their neighbors."""
    last = len(line) - 1
    if line[0] == line[last]:                      # a ring: its ends are one point, so split it at the point farthest from them
        mid = max(range(last), key=lambda i: (line[i][0] - line[0][0]) ** 2 + (line[i][1] - line[0][1]) ** 2)
        keep, stack = {0, mid, last}, [(0, mid), (mid, last)]
    else:
        keep, stack = {0, last}, [(0, last)]
    while stack:
        a, b = stack.pop()
        (ay, ax), (by, bx) = line[a], line[b]
        dx, dy, far, at = bx - ax, by - ay, 0.0, None
        for i in range(a + 1, b):
            py, px = line[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / ((dx * dx + dy * dy) ** 0.5 or 1)
            if d > far:
                far, at = d, i
        if at is not None and far > tol:
            keep.add(at)
            stack += [(a, at), (at, b)]
    return [line[i] for i in sorted(keep)]


def areas() -> list[dict]:
    feats = requests.get(AREAS, timeout=180, params={"$limit": 200}).json()["features"]
    out = [{"name": f["properties"]["community"].title().replace("Mckinley", "McKinley").replace("Ohare", "O'Hare"),
            "number": int(f["properties"]["area_numbe"]),
            "rings": [[[round(lat, 4), round(lon, 4)] for lat, lon in _thin(ring, COARSE)] for ring in _lines(f["geometry"]) if len(ring) > 30]}
           for f in feats]
    return sorted(out, key=lambda a: a["number"])


def build_wide() -> dict:
    got = {name: tiger(layer, field, WIDE_BOX, COARSE) for name, (layer, field) in WIDE_LAYERS.items()}
    lines = lambda name: [{"name": f["properties"].get("NAME") or "", "line": line} for f in got[name] for line in _lines(f["geometry"])]
    return {"built": time.strftime("%Y-%m-%d"), "box": dict(zip(("south", "west", "north", "east"), WIDE_BOX)),
            "source": "U.S. Census Bureau, TIGERweb: census tracts, roads, railroads and water (tigerweb.geo.census.gov). "
                      "City of Chicago: community area boundaries (data.cityofchicago.org)",
            "areas": areas(),
            "tracts": {f["properties"]["GEOID"]: _lines(f["geometry"]) for f in got["tracts"]},
            "highways": lines("highways"), "arterials": lines("arterials"), "rail": lines("rail"),
            "water": [{"name": f["properties"].get("NAME") or "", "rings": _lines(f["geometry"])} for f in got["water"] if f["geometry"]]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path)
    ap.add_argument("--wide", action="store_true", help="the city around the clinic's area, in less detail")
    a = ap.parse_args(argv)
    d, out = (build_wide(), a.out or WIDE) if a.wide else (build(), a.out or OUT)
    out.write_text(json.dumps(d, separators=(",", ":")))
    print(f"{len(d['tracts'])} tracts, {len(d['highways'])} highway, {len(d['arterials'])} arterial, {len(d.get('streets', []))} street and {len(d['rail'])} rail lines, "
          f"{len(d['water'])} water areas -> {out} ({out.stat().st_size / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
