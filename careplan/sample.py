"""A stand-in for a planner run, for working on the pages without a planner (CAREPLAN_SAMPLE=1).

Built from the visit's answer key: the flagged items, their barrier and the evidence are the key's; the places come from the
resource table; the program wording comes from rules/programs.yaml. The reasons are stock sentences, one per kind of
barrier. It has the shape of a saved run, so the pages show it the same way, and every page labels it as a sample.
"""
from __future__ import annotations

import functools

import yaml

from .cases import Case
from .data import ResourceTable
from .planner import PROGRAMS_PATH

REASON = {
    "transport": "Getting there depends on a ride this patient may not have",
    "work_schedule": "The time conflicts with the hours this patient works",
    "food_access": "The diet depends on food this patient cannot easily get",
    "other_cost": "The plan carries a cost this patient may not be able to cover",
    "language_literacy": "The instructions may not be understood as they are written",
    "housing": "The plan assumes a stable place to live",
    "caregiving": "The plan depends on help at home that may not be there",
    "utilities": "The plan depends on power at home, and the power is at risk",
    "med_cost": "The cost of the medication may keep this patient from filling it",
    "digital_access": "The plan depends on a device or a connection this patient may not have",
}
PLACES = {"food_access": ("food", "food_pantry"), "utilities": ("utilities", "utility_assistance"), "med_cost": ("med_cost", "med_assistance_program")}
WORSE = {"likely_ok": 0, "at_risk": 1, "blocked": 2}
STEP_S = 0.35                                    # seconds between the steps of a sample review, so the page can be watched


@functools.cache
def _programs() -> dict[str, str]:
    return {p["category"]: p["how_to_start"] for p in reversed(yaml.safe_load(PROGRAMS_PATH.read_text())["programs"])}


def run(case: Case, tract: dict | None, resources: ResourceTable) -> dict:
    items, lookups = [], []
    for plan in case.plan:
        conflicts = [c for c in case.data["planted_conflicts"] if c["item_id"] == plan["item_id"]]
        alternatives, seen = [], set()
        for c in conflicts:
            need, category = PLACES.get(c["barrier"], (None, None))
            if need and tract:
                lookups.append(need)
                for hit in resources.search(tract["intpt_lat"], tract["intpt_lon"], need, limit=2):
                    alternatives.append({"category": category, "resource_record_id": hit["record_id"], "text": f"{hit['name']}, {hit['distance_miles']} miles from home"})
            for cat in c.get("acceptable_alternative_categories", []):
                if cat not in seen and cat != category and cat != "other":
                    seen.add(cat)
                    alternatives.append({"category": cat, "resource_record_id": None, "text": _programs().get(cat, f"Offer {cat.replace('_', ' ')}")})
        items.append({"item_id": plan["item_id"], "status": max((c["expected_status"] for c in conflicts), key=WORSE.get, default="likely_ok"),
                      "barriers": [{"barrier": c["barrier"], "reason": REASON.get(c["barrier"], c["barrier"].replace("_", " ")),
                                    "evidence": [e["ref"] for e in c["evidence"]]} for c in conflicts],
                      "alternatives": alternatives[:4]})
    calls = [{"tool": t, "args": {}, "ok": True} for t in ("get_patient", "get_visit_transcript", "get_tract_profile")]
    calls += [{"tool": "find_resources", "args": {"tract_id": case.tract_geoid, "need": need, "days": []}, "ok": True} for need in dict.fromkeys(lookups)]
    events = [{"type": "tool", **{k: c[k] for k in ("tool", "args")}} for c in calls] + [{"type": "item", "item": i} for i in items]
    timed = [[round((n + 1) * STEP_S, 2), ev] for n, ev in enumerate(events)]
    return {"saved": None, "profile": "sample", "events": timed,
            "rec": {"case_id": case.case_id, "profile": "sample", "assessment": {"items": items}, "tool_calls": calls, "usage": {},
                    "steps": 0, "wall_s": timed[-1][0], "error": None}}
