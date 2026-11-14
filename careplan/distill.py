"""Distill the big planner's own good cards into fine-tuning trajectories (the overnight loop on box 2).

By day Super writes cards. The scorer decides, with no human, which cards were *clean*: every planted conflict
flagged, every flag supported by its cited evidence, no false flags. This turns those runs into SFT trajectories
by replaying the model's own tool calls against the real tools (results come from the tools, not from the log),
so Nano learns Super's realistic behaviour instead of the synthetic template.

    python -m careplan.distill runs/<super run on synthetic cases> [more runs...] --cases data/synth/cases.jsonl \
        --out data/synth/sft_distill [--merge data/synth/sft/train.jsonl]

Refuses dev/test cases (D*, T*): those are what the tuned model is scored on. Output matches careplan.sft:
{case_id, messages, tools} per line, plus a summary JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from . import ROOT
from .cases import Case, build_ref_index, load_cases
from .data import ResourceTable, TractTable
from .planner import MAX_RESULT_CHARS, PREFETCH_NOTE, PREFETCH_TOOLS, RunResult, prefetch_record, system_prompt, user_prompt
from .scoring import score_assessment
from .sft import gold_assessment
from .tools import ALL_TOOLS, SUBMIT_SPEC, TOOL_SPECS, Toolbox, ToolError, validate_assessment

TOOLS = [TOOL_SPECS[n] for n in ALL_TOOLS] + [SUBMIT_SPEC]


def load_case_pool(path: Path) -> dict[str, Case]:
    if path.is_file() and path.suffix == ".jsonl":
        out = {}
        for line in path.read_text().splitlines():
            d = json.loads(line)
            out[d["case_id"]] = Case(data=d, path=path, refs=build_ref_index(d))
        return out
    return {c.case_id: c for c in load_cases(path)}


def load_feedback(path: Path) -> dict[str, dict[str, dict]]:
    """feedback.jsonl -> {assessment_sha256: {item_id: last decision row}}. Keyed by the card's hash so a decision
    applies to exactly the card the clinician saw."""
    out: dict[str, dict[str, dict]] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("assessment_sha256"):
            out.setdefault(r["assessment_sha256"], {})[r["item_id"]] = r
    return out


def clinician_clean(assessment: dict | None, feedback: dict[str, dict[str, dict]]) -> tuple[bool, str]:
    """Production filter: every flagged item was accepted (or edited) and nothing was dismissed. No answer key needed.
    Edited items keep the model's card for now; the note is logged for a later version that rewrites the item."""
    if not assessment:
        return False, "no card"
    key = hashlib.sha256(json.dumps(assessment, sort_keys=True).encode()).hexdigest()
    decisions = feedback.get(key)
    if not decisions:
        return False, "no clinician decisions for this card"
    flagged = [i["item_id"] for i in assessment["items"] if i["status"] != "likely_ok"]
    if any(d.get("decision") == "dismiss" for d in decisions.values()):
        return False, "a flag was dismissed"
    missing = [i for i in flagged if i not in decisions]
    if missing:
        return False, f"{len(missing)} flagged item(s) not reviewed"
    return True, "accepted by clinician"


def is_clean(case: Case, assessment: dict | None, tracts: TractTable, resources: ResourceTable) -> tuple[bool, str]:
    if not assessment:
        return False, "no card"
    s = score_assessment(case, assessment, "distill", 1, tracts, resources)
    if s.fp:
        return False, f"{s.fp} false flag(s)"
    if s.tp != s.planted:
        return False, f"recall {s.tp}/{s.planted}"
    if s.evidence_ok != s.planted:
        return False, f"evidence {s.evidence_ok}/{s.planted}"
    return True, "clean"


def replay(rec: dict, case: Case, tracts: TractTable, resources: ResourceTable, keep_repeats: bool = False,
           answers: str = "model", errors: str = "strip", shape: str = "steps") -> dict | None:
    """Rebuild the conversation: the model's tool calls per step, real tool results, final submit.
    answers="model": the card Super wrote (use with the clean filter: the at-event mode).
    answers="gold": the generator's answer key, for synthetic cases that have one (pre-event training data: Super's
    realistic tool trajectory, the key's correct card)."""
    tb = Toolbox(case, tracts, resources)
    found: dict[str, list] = {}
    if shape == "prefetch":
        return _replay_prefetch(rec, case, tb, tracts, resources, found, answers, errors)
    msgs = [{"role": "system", "content": system_prompt("tools")}, {"role": "user", "content": user_prompt(case)}]
    seen: set[str] = set()
    n = 0
    for e in rec["log"]:
        if "step" not in e:
            continue
        calls = [c for c in e["tool_calls"] if c["name"] != "submit_assessment" and not c.get("parse_error")]
        if not calls:
            continue                                   # nudges and empty turns don't teach anything
        kept = []
        for c in calls:
            key = c["name"] + json.dumps(c["args"], sort_keys=True, default=str)
            if key in seen and not keep_repeats:
                continue                               # the planner answered repeats with an error; don't teach them
            seen.add(key)
            kept.append(c)
        if not kept:
            continue
        results = []
        for c in kept:
            try:
                out = tb.call(c["name"], c["args"])
                if c["name"] == "find_resources" and isinstance(out, list):
                    found.setdefault(c["args"].get("need", ""), out)
                results.append((c, out))
            except ToolError as ex:
                if errors == "drop":
                    return None            # the teacher fumbled a call (e.g. a wrong tract id); don't teach the fumble
                if errors == "keep":
                    results.append((c, {"error": str(ex)}))
                # errors == "strip": the fumbled call and its error simply never happened
        if not results:
            continue
        tc = [{"id": f"d{n + i}", "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
              for i, (c, _) in enumerate(results)]
        msgs.append({"role": "assistant", "content": "", "tool_calls": tc})    # content dropped: reasoning text was 'none' anyway
        for t, (c, out) in zip(tc, results):
            text = json.dumps(out, ensure_ascii=False, default=str)
            msgs.append({"role": "tool", "tool_call_id": t["id"], "name": c["name"],
                         "content": text[:MAX_RESULT_CHARS] + ("...(truncated)" if len(text) > MAX_RESULT_CHARS else "")})
        n += len(results)
    if n == 0:
        return None
    if answers == "gold":
        missing = [c for c in _gold_required_calls(case, case.patient["tract_geoid"])
                   if c["name"] + json.dumps(c["args"], sort_keys=True) not in seen]
        if missing:
            tc = [{"id": f"m{i}", "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}} for i, c in enumerate(missing)]
            msgs.append({"role": "assistant", "content": "", "tool_calls": tc})
            for t, c in zip(tc, missing):
                try:
                    out = tb.call(c["name"], c["args"])
                except ToolError:
                    return None                       # the key cites something the tools can't produce: not a training example
                if c["name"] == "find_resources" and isinstance(out, list):
                    found.setdefault(c["args"]["need"], out)
                text = json.dumps(out, ensure_ascii=False, default=str)
                msgs.append({"role": "tool", "tool_call_id": t["id"], "name": c["name"],
                             "content": text[:MAX_RESULT_CHARS] + ("...(truncated)" if len(text) > MAX_RESULT_CHARS else "")})
    return _finish(msgs, rec, case, tracts, resources, found, answers, "steps")


def _gold_required_calls(case: Case, tract: str) -> list[dict]:
    """The calls the answer key's card presupposes: a transit check for every cited transit ref, a resource lookup for
    every flagged need. Added when the teacher didn't make them, so the student never learns to cite what it didn't fetch."""
    from .sft import NEED
    conflicts = case.data.get("planted_conflicts", [])
    calls = [{"name": "get_transit_trip", "args": {"appt_ref": r}} for r in
             sorted({e["ref"].split(":", 1)[1] for c in conflicts for e in c["evidence"] if e["ref"].startswith("transit:")})]
    calls += [{"name": "find_resources", "args": {"tract_id": tract, "need": n}} for n in
              sorted({NEED[c["barrier"]] for c in conflicts if c["barrier"] in NEED})]
    return calls


def _final_card(rec: dict, case: Case, tracts, resources, found: dict, answers: str) -> dict | None:
    card = rec["assessment"]
    if answers == "gold":
        if "planted_conflicts" not in case.data:
            return None
        card = gold_assessment(case, found)
        if validate_assessment(card, [i["item_id"] for i in case.plan]):
            return None
        s = score_assessment(case, card, "gold", 1, tracts, resources)
        if s.fp or s.evidence_ok != s.planted:
            return None
    return card


def _finish(msgs, rec, case, tracts, resources, found, answers, shape):
    card = _final_card(rec, case, tracts, resources, found, answers)
    if card is None:
        return None
    msgs.append({"role": "assistant", "content": "",
                 "tool_calls": [{"id": "dsubmit", "type": "function", "function": {"name": "submit_assessment", "arguments": json.dumps(card)}}]})
    return {"case_id": case.case_id, "messages": msgs, "tools": TOOLS, "source": f"distilled-{answers}-{shape}"}


def _replay_prefetch(rec, case, tb, tracts, resources, found, answers, errors):
    """e5: the teacher's trajectory in the one-step shape. The record and tract are pre-loaded exactly as the planner
    does with prefetch=True; every call the teacher made beyond those (transit checks, resource lookups, transcript or
    note reads) is collapsed into ONE gathering turn, deduplicated and in the teacher's order; then the card."""
    msgs = [{"role": "system", "content": system_prompt("tools")},
            {"role": "user", "content": user_prompt(case) + "\n\n" + PREFETCH_NOTE}]
    prefetch_record(case, tb, msgs, RunResult(case.case_id, "sft", None, None, 0), set(), lambda e: None)
    pre = set(PREFETCH_TOOLS) | {"get_tract_profile"}
    gather, seen = [], set()
    teacher_calls = [c for e in rec["log"] if "step" in e for c in e.get("tool_calls", [])]
    required = _gold_required_calls(case, case.patient["tract_geoid"]) if answers == "gold" else []
    for c in teacher_calls + required:
        if c["name"] in pre or c["name"] == "submit_assessment" or c.get("parse_error"):
            continue
        key = c["name"] + json.dumps(c["args"], sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            gather.append(c)
    results = []
    for c in gather:
        try:
            out = tb.call(c["name"], c["args"])
            if c["name"] == "find_resources" and isinstance(out, list):
                found.setdefault(c["args"].get("need", ""), out)
            results.append((c, out))
        except ToolError as ex:
            if errors == "drop":
                return None
            if errors == "keep":
                results.append((c, {"error": str(ex)}))
    if results:
        tc = [{"id": f"g{i}", "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}} for i, (c, _) in enumerate(results)]
        msgs.append({"role": "assistant", "content": "", "tool_calls": tc})
        for t, (c, out) in zip(tc, results):
            text = json.dumps(out, ensure_ascii=False, default=str)
            msgs.append({"role": "tool", "tool_call_id": t["id"], "name": c["name"],
                         "content": text[:MAX_RESULT_CHARS] + ("...(truncated)" if len(text) > MAX_RESULT_CHARS else "")})
    return _finish(msgs, rec, case, tracts, resources, found, answers, "prefetch")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--cases", type=Path, default=ROOT / "data" / "synth" / "cases.jsonl")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "synth" / "sft_distill")
    ap.add_argument("--merge", type=Path, help="existing train.jsonl to append (e.g. the synthetic gold set)")
    ap.add_argument("--conditions", default="C2", help="which conditions' results to use")
    ap.add_argument("--answers", choices=["model", "gold"], default="model",
                    help="model: keep only clean cards Super wrote (at-event mode); gold: Super's tool trajectory + the answer key")
    ap.add_argument("--shape", choices=["steps", "prefetch"], default="steps", help="steps: the teacher's turns; prefetch: one-step shape (e5)")
    ap.add_argument("--errors", choices=["strip", "drop", "keep"], default="strip",
                    help="teacher tool calls that error (wrong tract id etc.; ~40%% of Super's runs have one): strip the call (default), drop the trajectory, or keep the error")
    ap.add_argument("--feedback", type=Path, help="runs/feedback.jsonl from careplan.serve: accept clinician-approved cards "
                    "instead of (or in addition to) scorer-clean ones; this is the production signal")
    a = ap.parse_args(argv)
    pool = load_case_pool(a.cases)
    tracts, resources = TractTable.load(), ResourceTable.load()
    a.out.mkdir(parents=True, exist_ok=True)
    conds = set(a.conditions.split(","))
    fb = load_feedback(a.feedback) if a.feedback else {}
    kept, why, seen_case = [], {}, set()
    for run in a.runs:
        for f in sorted((run / "results").glob("*.json")):
            rec = json.loads(f.read_text())
            if rec.get("condition") not in conds:
                continue
            cid = rec["case_id"]
            if cid[0] in "DT":
                why["refused dev/test case"] = why.get("refused dev/test case", 0) + 1
                continue
            case = pool.get(cid)
            if case is None:
                why["case not in pool"] = why.get("case not in pool", 0) + 1
                continue
            if a.answers == "gold":
                ok, reason = True, "gold"
            else:
                ok, reason = clinician_clean(rec["assessment"], fb) if fb else (False, "")
                if not ok:                                  # fall back to the scorer when the case has an answer key
                    ok, reason = is_clean(case, rec["assessment"], tracts, resources) if "planted_conflicts" in case.data else (False, reason or "no answer key and no clinician decisions")
            if a.answers == "gold" and not rec["assessment"]:
                ok, reason = False, "no card"                      # a run that never finished has no usable trajectory
            if not ok:
                why[reason.split(" ")[0] if reason.startswith(("recall", "evidence")) else reason] = why.get(reason.split(" ")[0] if reason.startswith(("recall", "evidence")) else reason, 0) + 1
                continue
            if cid in seen_case:
                why["duplicate case (another rep)"] = why.get("duplicate case (another rep)", 0) + 1
                continue
            ex = replay(rec, case, tracts, resources, answers=a.answers, errors=a.errors, shape=a.shape)
            if ex is None:
                why["no usable tool calls" + (" (or a tool error, --errors drop)" if a.errors == "drop" else "")] = why.get("no usable tool calls" + (" (or a tool error, --errors drop)" if a.errors == "drop" else ""), 0) + 1
                continue
            seen_case.add(cid)
            kept.append(ex)
    with (a.out / "train.jsonl").open("w") as fh:
        if a.merge and a.merge.exists():
            fh.write(a.merge.read_text().rstrip("\n") + "\n")
        for ex in kept:
            fh.write(json.dumps(ex) + "\n")
    summary = {"distilled": len(kept), "answers": a.answers, "feedback_cards": len(fb), "dropped": why, "merged_from": str(a.merge) if a.merge else None,
               "runs": [str(r) for r in a.runs], "out": str(a.out / "train.jsonl")}
    (a.out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
