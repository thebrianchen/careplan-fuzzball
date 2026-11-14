"""Sign the public-data bundle (the last job in fuzzball/nightly-civic.fz.yaml). A thin wrapper over pipeline.bundle:
builds bundle/ from data/processed, signs the manifest with the Ed25519 key, then verifies its own output.

    python -m pipeline.sign_bundle [--key keys/bundle.key] [--out bundle]      # key path also from BUNDLE_KEY
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from careplan import ROOT
from pipeline import bundle


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", type=Path, default=Path(os.environ.get("BUNDLE_KEY", ROOT / "keys" / "bundle.key")))
    ap.add_argument("--out", type=Path, default=ROOT / "bundle")
    a = ap.parse_args(argv)
    if not a.key.exists():
        print(f"no signing key at {a.key}; create one with: python -m pipeline.bundle keygen")
        return 2
    out = bundle.build(a.key, a.out)
    problems = bundle.verify(a.key.with_suffix(".pub"), out)
    print(f"signed {out} ({len(list(out.iterdir()))} files); verify: {'ok' if not problems else problems}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
