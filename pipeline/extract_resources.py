"""One shard of the nightly resource extraction (the task-array job in fuzzball/nightly-civic.fz.yaml).

Takes the source URLs of the curated resource list, keeps every Nth one for this shard, and runs pipeline.extract on
each. Shards write separate files so tasks never share an output.

    python -m pipeline.extract_resources --shard 3 --of 16 [--profile nano-noreason] [--list]
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import time
from pathlib import Path

from careplan import ROOT


def shard_urls(src: Path, shard: int, of: int) -> list[str]:
    """Unique source URLs, in file order; shard is 1-based (Fuzzball task ids start at 1)."""
    seen, urls = set(), []
    with src.open(newline="") as f:
        for row in csv.DictReader(f):
            u = (row.get("source_url") or row.get("url") or "").strip()
            if u.startswith("http") and u not in seen:
                seen.add(u)
                urls.append(u)
    return [u for i, u in enumerate(urls) if i % of == (shard - 1) % of]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, required=True)
    ap.add_argument("--of", type=int, default=16)
    ap.add_argument("--src", type=Path, default=ROOT / "data" / "curated" / "resources.csv")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data" / "processed" / "extracted")
    ap.add_argument("--profile", default="nano-noreason")
    ap.add_argument("--list", action="store_true", help="print this shard's URLs and exit (no model, no network)")
    a = ap.parse_args(argv)
    urls = shard_urls(a.src, a.shard, a.of)
    if a.list:
        print("\n".join(urls))
        return 0
    import requests
    from careplan.llm import ChatClient
    from careplan.runner import load_profiles
    from pipeline.extract import UA, extract
    client = ChatClient(dataclasses.replace(load_profiles()[a.profile], temperature=0.2))
    s = requests.Session()
    s.headers["User-Agent"] = UA
    a.out_dir.mkdir(parents=True, exist_ok=True)
    out = a.out_dir / f"shard_{a.shard:02d}_of_{a.of:02d}.jsonl"
    n = failed = 0
    with out.open("w") as f:
        for url in urls:
            try:
                rec = extract(url, client, s)
            except Exception as e:                      # one dead page must not stop the shard
                rec, failed = {"source_url": url, "error": str(e)[:200]}, failed + 1
            f.write(json.dumps(rec) + "\n")
            n += 1
            time.sleep(1.0)
    print(json.dumps({"shard": a.shard, "of": a.of, "urls": n, "failed": failed, "out": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
