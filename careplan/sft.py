"""Turn synthetic cases into supervised fine-tuning trajectories (OpenAI chat + tool-call format).

Each trajectory is what an ideal planner does, executed against the real tools:
  turn 1: read the record and the home tract in one batched call;
  turn 2: find_resources for every flagged need, in one batched call;
  turn 3: submit_assessment with the generator's answer key, citing the evidence refs and real
          resource records that came back.
Every gold assessment is checked: it must validate and score supported recall 1.0 with no false
flags, or the example is dropped. Training data never includes Lynna's test cases.

    python -m careplan.sft --cases data/synth/cases.jsonl --out data/synth/sft
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import ROOT
from .cases import Case, build_ref_index
from .data import ResourceTable, TractTable
from .planner import MAX_RESULT_CHARS, PREFETCH_NOTE, RunResult, prefetch_record, system_prompt, user_prompt
from .scoring import score_assessment
from .tools import ALL_TOOLS, SUBMIT_SPEC, TOOL_SPECS, Toolbox, validate_assessment

NEED = {"transport": "transport", "food_access": "food", "utilities": "utilities", "med_cost": "med_cost",
        "housing": "housing", "caregiving": "caregiving", "language_literacy": "language", "digital_access": "digital"}
REASON = {"transport": "No reliable way to get there as scheduled.", "food_access": "Limited access to the foods this plan needs.",
          "med_cost": "Out-of-pocket cost makes this medicine hard to keep filling.",
          "utilities": "Power or refrigeration at home is at risk.", "housing": "No stable place to store medicine or receive mail.",
          "caregiving": "Caregiving duties conflict with the schedule.", "work_schedule": "Work hours conflict with the schedule.",
          "language_literacy": "Written English instructions may not be usable.", "digital_access": "No smartphone or internet for this item."}


def _call(cid: str, name: str, args: dict) -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _result(cid: str, name: str, out) -> dict:
    text = json.dumps(out, ensure_ascii=False, default=str)
    return {"role": "tool", "tool_call_id": cid, "name": name,
            "content": text[:MAX_RESULT_CHARS] + ("...(truncated)" if len(text) > MAX_RESULT_CHARS else "")}


def gold_assessment(case: Case, found: dict[str, list]) -> dict:
    """The generator's answer key as a card: one entry per plan item, evidence refs from the planted conflicts,
    first alternative pointing at a real resource record when the trajectory found one for that need."""
    by_item: dict[str, list] = {}
    for c in case.data["planted_conflicts"]:
        by_item.setdefault(c["item_id"], []).append(c)
    items = []
    for it in case.plan:
        cs = by_item.get(it["item_id"])
        if not cs:
            items.append({"item_id": it["item_id"], "status": "likely_ok"})
            continue
        barriers, alts = [], []
        for c in cs:
            barriers.append({"barrier": c["barrier"], "reason": REASON[c["barrier"]], "evidence": [e["ref"] for e in c["evidence"]]})
            recs = found.get(NEED.get(c["barrier"], ""), [])
            for k, cat in enumerate(c["acceptable_alternative_categories"][:2]):
                rec = recs[0] if (k == 0 and recs) else None
                text = (f"{rec['name']}: {rec['hours']}, {rec['distance_miles']} mi away." if rec
                        else f"Offer {cat.replace('_', ' ')}.")
                alts.append({"category": cat, "resource_record_id": rec["record_id"] if rec else None, "text": text})
        items.append({"item_id": it["item_id"], "status": max((c["expected_status"] for c in cs), key=["at_risk", "blocked"].index),
                      "barriers": barriers, "alternatives": alts[:3]})
    return {"items": items}


def trajectory(case: Case, tracts: TractTable, resources: ResourceTable, shape: str = "steps") -> dict | None:
    """shape="steps": the model reads the record (turn 1), checks transit if cited, looks up resources, submits.
    shape="prefetch": the record and tract are pre-loaded exactly as careplan.planner does with prefetch=True, and the
    model makes ONE gathering turn (transit + resources together) then submits: the one-step card (finetune-plan e3).
    Serve a prefetch-shaped model with `careplan run --prefetch` / CAREPLAN_PREFETCH=1."""
    tb = Toolbox(case, tracts, resources)
    pid, tract = case.patient["patient_id"], case.patient["tract_geoid"]
    conflicts = case.data["planted_conflicts"]
    transit_refs = sorted({e["ref"].split(":", 1)[1] for c in conflicts for e in c["evidence"] if e["ref"].startswith("transit:")})
    needs = sorted({NEED[c["barrier"]] for c in conflicts if c["barrier"] in NEED})
    found: dict[str, list] = {}
    if shape == "prefetch":
        msgs = [{"role": "system", "content": system_prompt("tools")},
                {"role": "user", "content": user_prompt(case) + "\n\n" + PREFETCH_NOTE}]
        prefetch_record(case, tb, msgs, RunResult(case.case_id, "sft", None, None, 0), set(), lambda e: None)
        gather = [("get_transit_trip", {"appt_ref": r}) for r in transit_refs] + [("find_resources", {"tract_id": tract, "need": n}) for n in needs]
        if gather:
            calls = [_call(f"c1_{i}", n, a) for i, (n, a) in enumerate(gather)]
            msgs.append({"role": "assistant", "content": "", "tool_calls": calls})
            for c, (n, a) in zip(calls, gather):
                try:
                    out = tb.call(n, a)
                except Exception:
                    return None
                if n == "find_resources":
                    found[a["need"]] = out
                msgs.append(_result(c["id"], n, out))
    else:
        msgs = [{"role": "system", "content": system_prompt("tools")}, {"role": "user", "content": user_prompt(case)}]
        first = [("get_patient", {"patient_id": pid}), ("get_problems", {"patient_id": pid}), ("get_meds", {"patient_id": pid}),
                 ("get_appointments", {"patient_id": pid}), ("get_screener", {"patient_id": pid}), ("get_tract_profile", {"tract_id": tract})]
        calls = [_call(f"c1_{i}", n, a) for i, (n, a) in enumerate(first)]
        msgs.append({"role": "assistant", "content": "", "tool_calls": calls})
        msgs += [_result(c["id"], n, tb.call(n, a)) for c, (n, a) in zip(calls, first)]
        if transit_refs:                                   # v2 cases: the key cites a transit check, so the trajectory makes it
            calls = [_call(f"c1t_{i}", "get_transit_trip", {"appt_ref": r}) for i, r in enumerate(transit_refs)]
            msgs.append({"role": "assistant", "content": "", "tool_calls": calls})
            for c, r in zip(calls, transit_refs):
                try:
                    msgs.append(_result(c["id"], "get_transit_trip", tb.call("get_transit_trip", {"appt_ref": r})))
                except Exception:                          # no facility on file: the key shouldn't have cited it
                    return None
        if needs:
            calls = [_call(f"c2_{i}", "find_resources", {"tract_id": tract, "need": n}) for i, n in enumerate(needs)]
            msgs.append({"role": "assistant", "content": "", "tool_calls": calls})
            for c, n in zip(calls, needs):
                found[n] = tb.call("find_resources", {"tract_id": tract, "need": n})
                msgs.append(_result(c["id"], "find_resources", found[n]))
    gold = gold_assessment(case, found)
    if validate_assessment(gold, [i["item_id"] for i in case.plan]):
        return None
    s = score_assessment(case, gold, "gold", 1, tracts, resources)
    if s.evidence_ok != s.planted or s.fp:
        return None
    msgs.append({"role": "assistant", "content": "", "tool_calls": [_call("c3_0", "submit_assessment", gold)]})
    return {"case_id": case.case_id, "messages": msgs, "tools": [TOOL_SPECS[n] for n in ALL_TOOLS] + [SUBMIT_SPEC]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, default=ROOT / "data" / "synth" / "cases.jsonl")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "synth" / "sft")
    ap.add_argument("--val-every", type=int, default=10, help="every Nth case goes to the validation split")
    ap.add_argument("--shape", choices=["steps", "prefetch"], default="steps", help="see trajectory()")
    a = ap.parse_args(argv)
    tracts, resources = TractTable.load(), ResourceTable.load()
    a.out.mkdir(parents=True, exist_ok=True)
    kept = dropped = 0
    with (a.out / "train.jsonl").open("w") as tr, (a.out / "val.jsonl").open("w") as va:
        for i, line in enumerate(a.cases.read_text().splitlines()):
            data = json.loads(line)
            ex = trajectory(Case(data=data, path=a.cases, refs=build_ref_index(data)), tracts, resources, a.shape)
            if ex is None:
                dropped += 1
                continue
            (va if i % a.val_every == 0 else tr).write(json.dumps(ex) + "\n")
            kept += 1
    print(f"kept {kept} trajectories, dropped {dropped} (gold failed its own check) -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
