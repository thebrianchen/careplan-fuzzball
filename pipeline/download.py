"""Download every source into data/raw/ and record checksums. Run on a machine with internet.

    python -m pipeline.download            # everything missing
    python -m pipeline.download --only gtfs_cta --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from careplan import ROOT
from .sources import ACS_YEAR, COUNTY_FIPS, SOURCES, STATE_FIPS

RAW = ROOT / "data" / "raw"
MANIFEST = RAW / "MANIFEST.json"


def targets():
    for sid, s in SOURCES.items():
        if s.get("per_county"):
            for c in COUNTY_FIPS:
                yield sid, s["url"].format(year=ACS_YEAR, state=STATE_FIPS, county=c[2:]), s["file"].format(county=c), s
        else:
            yield sid, s["url"].format(state=STATE_FIPS), s["file"].format(state=STATE_FIPS), s


def fetch(url: str, dest: Path) -> str:
    h = hashlib.sha256()
    tmp = dest.with_suffix(dest.suffix + ".part")
    if "api.census.gov" in url:
        key = os.environ.get("CENSUS_API_KEY")
        if not key:
            raise RuntimeError("the Census API needs a key: set CENSUS_API_KEY (free: https://api.census.gov/data/key_signup.html)")
        url += f"&key={key}"
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "careplan-check-hackathon/0.1"}) as r:
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", ""):
            raise RuntimeError(f"got an HTML page instead of data (HTTP {r.status_code}); the URL or key is wrong")
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                h.update(chunk)
    tmp.replace(dest)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    RAW.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    failed = []
    for sid, url, fname, s in targets():
        if a.only and sid not in a.only:
            continue
        dest = RAW / fname
        if dest.exists() and not a.force:
            print(f"skip  {fname} (exists)")
            continue
        print(f"get   {fname} <- {url}")
        try:
            digest = fetch(url, dest)
        except Exception as e:
            failed.append(sid)
            hint = "" if s.get("verified") else f"  (URL not verified; find the current link on {s['page']})"
            print(f"FAIL  {sid}: {e}{hint}", file=sys.stderr)
            continue
        manifest[fname] = {"source": sid, "url": url, "sha256": digest, "bytes": dest.stat().st_size,
                           "downloaded": datetime.now(timezone.utc).isoformat(timespec="seconds"), "what": s["what"]}
        MANIFEST.write_text(json.dumps(manifest, indent=2))
    if failed:
        print(f"\n{len(failed)} failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
