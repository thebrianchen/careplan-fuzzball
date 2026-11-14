"""Apply Lynna's dispute decisions (reports/reviews.jsonl from scripts/review_server.py) to the case files.

Rules (latest decision per item wins; notes from any submission are kept):
  dispute:<item>:<barrier> = system  -> add a planted conflict: the system was right, our key missed a barrier.
                                        Evidence = the refs the system cited (from the run dirs) that exist in the case;
                                        if none survive, the conflict is listed as a follow-up instead of added.
  missed:<conflict_id>     = system  -> remove that conflict: not really a barrier.
  ... = key                          -> no change (the key stands; the system's flag stays a false flag / the miss stays a miss).
  general notes                      -> cases/REVIEW_NOTES.md (free text she wrote; some are new barriers to add by hand).
Every touched case gets reviewed_by. Then: validate, re-freeze test cases, print a summary.

    python scripts/apply_reviews.py --reviews reports/reviews.jsonl --runs runs/h1_dev_raw_merged runs/h1_test_raw_merged
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from careplan.cases import iter_case_paths, validate_case  # noqa: E402
from careplan.synth import ALTS  # noqa: E402

REVIEWER = "Lynna Yang (dispute review, 2026-11-10)"


def latest_answers(path: Path) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """case -> {key: decision}, case -> [notes]. Latest decision per key; notes accumulate."""
    decisions: dict[str, dict] = defaultdict(dict)
    notes: dict[str, list] = defaultdict(list)
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        cid, a = r.get("case_id"), r.get("answers") or {}
        if not cid or cid in ("SMOKE", "TEST"):
            continue
        for k, v in a.items():
            if k == "general":
                if v and v not in notes[cid]:
                    notes[cid].append(v)
            elif isinstance(v, dict):
                if v.get("decision"):
                    decisions[cid][k] = v["decision"]
                if v.get("note") and v["note"] not in notes[cid]:
                    notes[cid].append(f"{k}: {v['note']}")
    return decisions, notes


def system_evidence(case_id: str, item: str, barrier: str, run_dirs: list[Path]) -> list[str]:
    refs: set[str] = set()
    for rd in run_dirs:
        for f in glob.glob(str(rd / "results" / f"{case_id}_C2_*.json")):
            r = json.loads(Path(f).read_text())
            for it in (r.get("assessment") or {}).get("items", []):
                if it["item_id"] == item:
                    for b in it.get("barriers", []) or []:
                        if b["barrier"] == barrier:
                            refs.update(b.get("evidence", []))
    return sorted(refs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviews", type=Path, default=Path("reports/reviews.jsonl"))
    ap.add_argument("--runs", type=Path, nargs="*", default=[])
    ap.add_argument("--cases", type=Path, nargs="*", default=[Path("cases/dev"), Path("cases/test")])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    decisions, notes = latest_answers(a.reviews)
    paths = {p.stem.split("-")[0]: p for root in a.cases for p in iter_case_paths(root)}
    added, removed, followups, touched = [], [], [], set()
    for cid, dec in sorted(decisions.items()):
        path = paths.get(cid)
        if not path:
            print(f"{cid}: no case file"); continue
        data = yaml.safe_load(path.read_text())
        known = {(c["item_id"], c["barrier"]): c for c in data["planted_conflicts"]}
        refs_in_case = set()
        for section in ("transcript",):
            refs_in_case.update(t["ref"] for t in data["visit"]["transcript"])
        p = data["patient"]
        for lst in ("meds", "appointments", "notes", "problems"):
            refs_in_case.update(x["ref"] for x in p.get(lst, []))
        refs_in_case.update(f"screener:{q}" for q in (p.get("screener") or {}).get("answers", {}))
        for key, d in dec.items():
            if key.startswith("dispute:") and d == "system":
                _, item, barrier = key.split(":", 2)
                if (item, barrier) in known:
                    continue
                ev = [r for r in system_evidence(cid, item, barrier, a.runs)
                      if r in refs_in_case or r.startswith(("tract:", "transit:"))]
                if not ev:
                    followups.append(f"{cid} {item}/{barrier}: Lynna says it's a real barrier; the system cited no evidence that exists in the case. Add by hand.")
                    continue
                for turn in data["visit"]["transcript"]:          # a clinician confirmed the barrier: tag the cited turns so the scorer counts them
                    if turn["ref"] in ev and barrier not in turn.get("topics", []):
                        turn.setdefault("topics", []).append(barrier)
                for note in data["patient"].get("notes", []):
                    if note["ref"] in ev and barrier not in note.get("topics", []):
                        note.setdefault("topics", []).append(barrier)
                n = len(data["planted_conflicts"]) + 1
                data["planted_conflicts"].append({"conflict_id": f"C{n}", "item_id": item, "barrier": barrier, "expected_status": "at_risk",
                                                  "evidence": [{"source": ("transcript" if r.startswith("turn:") else "tract" if r.startswith("tract:") else "record"), "ref": r} for r in ev],
                                                  "acceptable_alternative_categories": ALTS.get(barrier, ["community_health_worker"])[:2],
                                                  "note": "Added from Lynna's dispute review: the system raised it, the key had missed it."})
                data["expected_ok_items"] = [i for i in data.get("expected_ok_items", []) if i != item]
                data["decoys"] = [x for x in data.get("decoys", []) if x.get("barrier") != barrier]
                added.append(f"{cid} {item}/{barrier} ({len(ev)} refs)")
            elif key.startswith("missed:") and d == "system":
                conf_id = key.split(":", 1)[1]
                before = len(data["planted_conflicts"])
                gone = [c for c in data["planted_conflicts"] if c["conflict_id"] == conf_id]
                data["planted_conflicts"] = [c for c in data["planted_conflicts"] if c["conflict_id"] != conf_id]
                if gone:
                    item = gone[0]["item_id"]
                    if not any(c["item_id"] == item for c in data["planted_conflicts"]) and item not in data.get("expected_ok_items", []):
                        data.setdefault("expected_ok_items", []).append(item)
                    removed.append(f"{cid} {conf_id} ({item}/{gone[0]['barrier']})")
        data["reviewed_by"] = REVIEWER
        errs = validate_case(data)
        if errs:
            print(f"{cid}: validation failed after apply: {errs[:3]}"); continue
        touched.add(cid)
        if not a.dry_run:
            path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120))
    if not a.dry_run:
        lines = ["# Lynna's review notes (verbatim, 2026-11-10)", ""]
        for cid in sorted(notes):
            lines.append(f"## {cid}")
            lines += [f"- {n}" for n in notes[cid]]
            lines.append("")
        if followups:
            lines += ["## Follow-ups (barriers she confirmed that need hand-written evidence)", *[f"- {f}" for f in followups], ""]
        Path("cases/REVIEW_NOTES.md").write_text("\n".join(lines))
    print(f"reviewed cases: {len(touched)} | conflicts added: {len(added)} | removed: {len(removed)} | follow-ups: {len(followups)}")
    for x in added: print("  +", x)
    for x in removed: print("  -", x)
    for x in followups: print("  ?", x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
