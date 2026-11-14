"""Which barriers does an address's census tract support? For choosing case addresses.

    python -m pipeline.lookup "4200 W Madison St, Chicago, IL 60624"
"""
from __future__ import annotations

import sys

from careplan.data import TractTable
from careplan.scoring import RULES, _cmp
from .geocode import geocode_address


def tract_barriers(row: dict) -> list[tuple[str, object, str, bool, list[str]]]:
    """(field, value, rule, fires, barriers) for every rule-backed tract field."""
    return [(f, row.get(f), r["rule"], _cmp(row.get(f), r["rule"]), r["supports"])
            for f, r in RULES["tract_fields"].items() if f in row]


def main(argv=None) -> int:
    address = " ".join(argv or sys.argv[1:])
    if not address:
        print(__doc__)
        return 1
    g = geocode_address(address)
    if not g.get("tract_geoid"):
        print(f"no match for {address!r}")
        return 1
    row = TractTable.load().get(g["tract_geoid"])
    print(f"{g['matched_address']}  ->  tract {g['tract_geoid']}")
    if row is None:
        print("tract not in data/processed/tracts.csv (outside Cook County?)")
        return 1
    for field, value, rule, fires, barriers in tract_barriers(row):
        print(f"  {'SUPPORTS ' + '/'.join(barriers) if fires else '':28} {field:22} {value!s:>8}  (rule {rule})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
