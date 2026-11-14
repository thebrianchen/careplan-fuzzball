"""Chicago food pantry locations from Plentiful's public directory -> data/raw/plentiful_chicago.json.

directory.plentiful.org is a public pantry directory ("Data verified by food banks and community
organizations"; its robots.txt welcomes all crawlers). Detail pages carry schema.org JSON-LD with
name, address and coordinates. Hours live only in the Plentiful app, so pantries from here show
"hours not listed". One request per second with an identifying User-Agent.

    python -m pipeline.pantries
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone

import requests

from careplan import ROOT

CITY_URL = "https://directory.plentiful.org/illinois/chicago"
OUT = ROOT / "data" / "raw" / "plentiful_chicago.json"
UA = "careplan-check-hackathon/0.1 (research prototype; synthetic patients only)"
LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def parse_detail(page: str, url: str) -> dict | None:
    for block in LD_RE.findall(page):
        try:
            d = json.loads(block)
        except json.JSONDecodeError:
            continue
        types = d.get("@type") if isinstance(d.get("@type"), list) else [d.get("@type")]
        if not {"LocalBusiness", "FoodEstablishment"} & set(types) or "geo" not in d:
            continue
        a = d.get("address") or {}
        return {"name": d.get("name", ""), "description": d.get("description", ""), "url": url,
                "address": f"{a.get('streetAddress', '')}, {a.get('addressLocality', '')}, "
                           f"{a.get('addressRegion', '')} {a.get('postalCode', '')}".strip(", "),
                "lat": d["geo"]["latitude"], "lon": d["geo"]["longitude"], "modified": d.get("dateModified", "")}
    return None


def main() -> int:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    city = s.get(CITY_URL, timeout=30)
    city.raise_for_status()
    links = sorted(set(re.findall(rf'href="({re.escape(CITY_URL)}/[^"#?]+)"', city.text)))
    print(f"{len(links)} pantry pages")
    out, failed = [], []
    for i, url in enumerate(links, 1):
        try:
            r = s.get(url, timeout=30)
            rec = parse_detail(r.text, url) if r.ok else None
        except requests.RequestException:
            rec = None
        (out.append(rec) if rec else failed.append(url))
        if i % 25 == 0:
            print(f"  {i}/{len(links)}")
        time.sleep(1.0)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"source": CITY_URL, "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "pantries": out, "failed": failed}, indent=1)
    OUT.write_text(data)
    manifest = OUT.parent / "MANIFEST.json"
    m = json.loads(manifest.read_text()) if manifest.exists() else {}
    m[OUT.name] = {"source": "plentiful_chicago", "url": CITY_URL, "sha256": hashlib.sha256(data.encode()).hexdigest(),
                   "bytes": len(data), "downloaded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "what": "Plentiful public directory: Chicago food pantry locations (no hours)"}
    manifest.write_text(json.dumps(m, indent=2))
    print(f"wrote {len(out)} pantries ({len(failed)} failed) to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
