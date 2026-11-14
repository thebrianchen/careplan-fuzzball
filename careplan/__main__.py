"""CLI: python -m careplan <command>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .cases import iter_case_paths, load_cases, new_mrn, validate_case_file


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="careplan")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate case files")
    v.add_argument("paths", nargs="*", default=["cases"])

    sub.add_parser("new-mrn", help="print a fresh canary MRN")

    fz = sub.add_parser("freeze", help="record hashes of test cases so later edits are caught")
    fz.add_argument("path", nargs="?", default="cases/test")
    fz.add_argument("--update", action="store_true", help="overwrite hashes of cases that changed (needs --reason; logged)")
    fz.add_argument("--reason", default="")

    r = sub.add_parser("run", help="run E1 conditions")
    r.add_argument("--cases", default="cases/dev")
    r.add_argument("--conditions", default="C0,C2")
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--profile", action="append", default=[], metavar="COND=PROFILE",
                   help="override a condition's model profile, e.g. C2=super-vllm")
    r.add_argument("--tag", default="")
    r.add_argument("--tracts")
    r.add_argument("--resources")
    r.add_argument("--allow-unfrozen", action="store_true")
    r.add_argument("--signals", action="store_true", help="tool results carry the barrier rules' verdicts; submissions must cite evidence kinds that can support each barrier")
    r.add_argument("--prefetch", action="store_true",
                   help="run the record reads before the first model call (fewer steps; H5 lever)")
    r.add_argument("--final", choices=["tool", "text"], default="tool",
                   help="how the card is returned: a submit_assessment tool call (baseline) or streamed JSON text")

    s = sub.add_parser("score", help="score a run directory")
    s.add_argument("run_dir")
    s.add_argument("--cases", default="cases")
    s.add_argument("--tracts")
    s.add_argument("--resources")

    lat = sub.add_parser("latency", help="project GB10 card latency from a run's token counts")
    lat.add_argument("run_dir")
    lat.add_argument("--decode-tps", type=float, default=16.0)
    lat.add_argument("--prefill-tps", type=float, default=1000.0)

    sub.add_parser("card", help="render a result JSON as the clinician card (HTML)", add_help=False)

    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "card":
        from .card import main as card_main
        return card_main(argv[1:])
    a = ap.parse_args(argv)
    if a.cmd == "validate":
        bad = 0
        for root in a.paths:
            for p in iter_case_paths(Path(root)):
                errs = validate_case_file(p)
                if errs:
                    bad += 1
                    print(f"FAIL {p}")
                    for e in errs:
                        print(f"   - {e}")
                else:
                    print(f"ok   {p}")
        return 1 if bad else 0
    if a.cmd == "new-mrn":
        print(new_mrn())
        return 0
    if a.cmd == "freeze":
        from .runner import freeze
        if a.update and not a.reason.strip():
            print("--update needs --reason")
            return 2
        frozen = freeze(load_cases(Path(a.path)), update=a.update, reason=a.reason.strip())
        print(f"{len(frozen)} test cases frozen in cases/FROZEN.json")
        return 0
    if a.cmd == "run":
        from .runner import run
        overrides = dict(x.split("=", 1) for x in a.profile)
        conds = [c.strip() for c in a.conditions.split(",") if c.strip()]
        d = run(Path(a.cases), conds, a.reps, overrides, a.tag,
                Path(a.tracts) if a.tracts else None, Path(a.resources) if a.resources else None, a.allow_unfrozen,
                prefetch=a.prefetch, final=a.final, signals=a.signals)
        print(f"run written to {d}")
        return 0
    if a.cmd == "score":
        from .runner import score_run
        summary = score_run(Path(a.run_dir), Path(a.cases), Path(a.tracts) if a.tracts else None,
                            Path(a.resources) if a.resources else None)
        print(json.dumps(summary["summary"], indent=2))
        if summary["tract_table_is_fixture"]:
            print("WARNING: scored against the FIXTURE tract table; tract evidence is not real.", file=sys.stderr)
        return 0
    if a.cmd == "latency":
        from .runner import project_latency
        print(json.dumps(project_latency(Path(a.run_dir), a.decode_tps, a.prefill_tps), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
