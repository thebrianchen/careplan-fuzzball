"""Data for the demo front end (careplan/web/demo): one JSON document per case, with every citation already resolved to
text, so the page needs no knowledge of the case files. Built from a saved real run (recordings/stream_<case>.json) or
from a live result; the shape is the same either way.

    python -m careplan.demo D001        # writes careplan/web/demo/data/D001.json from the saved run
"""
from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

from . import ROOT
from .card import STATUS, _evidence
from .cases import Case
from .data import ResourceTable, TractTable, haversine_km

RECORDINGS = ROOT / "recordings"
OUT = ROOT / "careplan" / "web" / "demo" / "data"
CHART_TOOLS = {"get_patient", "get_problems", "get_meds", "get_appointments", "get_screener", "get_notes"}
STEP = {"get_visit_transcript": ("visit", "Read the visit"), "get_tract_profile": ("neighborhood", "Looked up the neighborhood"),
        "get_transit_trip": ("transit", "Checked the bus trip to the appointment")}


@functools.cache
def _profiles() -> dict:
    from .runner import load_profiles
    return load_profiles()


def model_name(profile: str | None) -> str | None:
    """The model a config profile runs, as a reader would name it: 'super-vllm-raw' -> 'Nemotron-3-Super-120B-A12B-NVFP4'."""
    p = _profiles().get(profile)
    return p.model.split("/")[-1].removeprefix("NVIDIA-") if p else profile


def step_for(t: float, ev: dict) -> dict:
    """One tool call as a plain step. A 'crossing' step is the only kind that leaves the private box."""
    if ev["tool"] == "find_resources":
        a = ev.get("args") or {}
        return {"t": t, "kind": "crossing", "label": f"Asked the public side for {a.get('need', '?')} help near home",
                "crossed": {"tract_id": a.get("tract_id"), "need": a.get("need"), "days": a.get("days") or []}, "results": ev.get("n_results")}
    kind, label = ("chart", "Read the patient chart") if ev["tool"] in CHART_TOOLS else STEP.get(ev["tool"], ("other", ev["tool"]))
    return {"t": t, "kind": kind, "label": label}


def _steps(events: list) -> list[dict]:
    out: list[dict] = []
    for t, ev in events:
        if ev.get("type") != "tool":
            continue
        step = step_for(t, ev)
        if not out or (out[-1]["kind"], out[-1]["label"]) != (step["kind"], step["label"]):
            out.append(step)
    return out


def resolve_item(case: Case, it: dict, t: float | None, tract: dict | None, resources: ResourceTable) -> dict:
    """One assessed plan item with every citation turned into text and every cited resource expanded."""
    plan = {i["item_id"]: i for i in case.plan}
    barriers = []
    for b in it.get("barriers", []) or []:
        ev = [{"ref": r, "source": s, "text": txt, "said_in_visit": r.startswith("turn:")}
              for r in b.get("evidence", []) for s, txt in [_evidence(case, r, tract)] if s != "Unresolved"]
        barriers.append({"barrier": b["barrier"], "label": b["barrier"].replace("_", " "), "reason": b.get("reason", ""), "evidence": ev})
    alts = []
    for alt in it.get("alternatives", []) or []:
        r = resources.by_id.get(alt.get("resource_record_id") or "")
        res = r and {"name": r.name, "address": r.address, "hours": r.hours_text(), "phone": r.phone, "lat": r.lat, "lon": r.lon,
                     "miles_from_home": round(haversine_km(tract["intpt_lat"], tract["intpt_lon"], r.lat, r.lon) * 0.621371, 1) if tract else None}
        alts.append({"category": alt["category"], "label": alt["category"].replace("_", " "), "text": alt["text"], "resource": res or None})
    return {"item_id": it["item_id"], "plan_text": plan.get(it["item_id"], {}).get("text", it["item_id"]), "t": t,
            "status": it["status"], "status_label": STATUS.get(it["status"], (it["status"],))[0], "barriers": barriers, "alternatives": alts}


def build(case: Case, rec: dict, events: list | None, tracts: TractTable, resources: ResourceTable, source: str, saved: str | None = None,
          where: str | None = None) -> dict:
    tract = tracts.get(case.tract_geoid)
    item_t = {ev["item"]["item_id"]: t for t, ev in (events or []) if ev.get("type") == "item"}
    items = [resolve_item(case, it, item_t.get(it["item_id"]), tract, resources) for it in (rec.get("assessment") or {}).get("items", [])]
    p, visit = case.patient, case.visit
    age = int(visit["date"][:4]) - int(p["dob"][:4]) - (visit["date"][5:] < p["dob"][5:]) if p.get("dob") and visit.get("date") else None
    return {
        "meta": {"case_id": case.case_id, "title": case.data.get("title", ""), "made_up_patient": True, "source": source, "saved": saved, "where": where,
                 "profile": rec.get("profile"), "model": model_name(rec.get("profile")), "seconds": rec.get("wall_s") or (events[-1][0] if events else None)},
        "patient": {"name": p.get("name"), "age": age, "sex": p.get("sex"), "coverage": (p.get("coverage") or {}).get("plan"),
                    "problems": [x.get("display") for x in p.get("problems", [])][:5]},
        "visit": {"date": visit.get("date"), "setting": visit.get("setting"),
                  "turns": [{"ref": t["ref"], "speaker": t["speaker"], "text": t["text"]} for t in visit["transcript"]]},
        "plan": [{"item_id": i["item_id"], "type": i.get("type"), "text": i["text"]} for i in case.plan],
        "steps": _steps(events or []),
        "items": items,
        "crossings": [{"tract_id": c["args"].get("tract_id"), "need": c["args"].get("need"), "days": c["args"].get("days") or []}
                      for c in rec.get("tool_calls", []) if c["tool"] == "find_resources"],
    }


def from_recording(case: Case, tracts: TractTable, resources: ResourceTable) -> dict | None:
    f = RECORDINGS / f"stream_{case.case_id}.json"
    if not f.is_file():
        return None
    d = json.loads(f.read_text())
    return build(case, d["rec"], d["events"], tracts, resources, "recorded", d.get("saved"), d.get("where"))


def main(argv=None) -> int:
    from .cases import load_cases
    from .runner import apply_case_tracts
    cases = {c.case_id: c for c in load_cases(ROOT / "cases")}
    apply_case_tracts(list(cases.values()))
    tracts, resources = TractTable.load(), ResourceTable.load()
    OUT.mkdir(parents=True, exist_ok=True)
    for cid in (argv or sys.argv[1:]) or ["D001"]:
        doc = from_recording(cases[cid], tracts, resources)
        if doc is None:
            print(f"{cid}: no saved run in recordings/"); return 1
        (OUT / f"{cid}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
        ev = [e for i in doc["items"] for b in i["barriers"] for e in b["evidence"]]
        print(f"{cid}: {len(doc['items'])} items, {len(doc['steps'])} steps, {len(ev)} pieces of evidence "
              f"({sum(not e['said_in_visit'] for e in ev)} not said in the visit), {len(doc['crossings'])} crossings -> {OUT / (cid + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
