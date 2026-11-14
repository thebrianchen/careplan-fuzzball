"""The planner's tools: 7 local (private zone) + find_resources (the one public-zone crossing),
plus submit_assessment for the final answer.

Every function validates its arguments and returns JSON-able data carrying `ref` strings the
planner can cite. Scoring metadata (topic tags, planted conflicts, decoys) is never returned.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import jsonschema

from . import BARRIERS, DAYS, NEEDS, ROOT
from .cases import Case, strip_hidden
from .data import ResourceTable, TractTable

ASSESSMENT_SCHEMA = json.loads((ROOT / "schemas" / "assessment.schema.json").read_text())
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

# Topic keywords for get_visit_transcript. Deliberately simple and independent of the
# case's hidden topic tags, so the tool doesn't leak the answer key.
TOPIC_WORDS = {
    "transport": r"\b(car|drive|driv|bus|buses|train|ride|rides|uber|lyft|cab|taxi|walk|transport|trip|parking)\w*",
    "food_access": r"\b(food|grocer|store|eat|ate|meal|lunch|dinner|breakfast|hungry|pantry|snap|cook)\w*",
    "med_cost": r"\b(copay|co-pay|afford|price|cost|expensive|pharmacy|refill|pills?)\w*",
    "other_cost": r"\b(afford|money|pay|bill|bills|rent|insurance|expensive|cost)\w*",
    "utilities": r"\b(electric|power|gas|heat|lights|utility|utilities|shut ?off|fridge|refrigerat)\w*",
    "housing": r"\b(apartment|landlord|evict|shelter|rent|housing|stairs|move|moving|staying with)\w*",
    "caregiving": r"\b(kids?|child|children|daughter|son|mother|mom|father|dad|grand\w+|care for|caregiv|babysit)\w*",
    "language_literacy": r"\b(read|reading|english|spanish|language|interpreter|understand|forms?)\w*",
    "digital_access": r"\b(phone|smartphone|app|internet|wifi|computer|email|video|online|portal)\w*",
    "work_schedule": r"\b(work|job|shift|boss|hours|off work|paid time|overtime)\w*",
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required,
                       "additionalProperties": False}}}


PATIENT_ID = {"type": "string", "description": "The patient id given in the task."}
TOOL_SPECS: dict[str, dict] = {
    "get_patient": _tool("get_patient", "Demographics, coverage, PCP and home census tract for the patient.",
                         {"patient_id": PATIENT_ID}, ["patient_id"]),
    "get_problems": _tool("get_problems", "Active problem list (ICD-10-CM).", {"patient_id": PATIENT_ID}, ["patient_id"]),
    "get_meds": _tool("get_meds", "Medications with dose, frequency, cost tier, monthly copay and refill gaps.",
                      {"patient_id": PATIENT_ID}, ["patient_id"]),
    "get_appointments": _tool("get_appointments", "Past and scheduled appointments with kept/no-show status.", {
        "patient_id": PATIENT_ID,
        "dept": {"type": "string", "description": "Optional department filter, e.g. dialysis."},
        "window_days": {"type": "integer", "minimum": 1, "maximum": 730, "description": "Look-back window from the visit date. Default 90."},
    }, ["patient_id"]),
    "get_visit_transcript": _tool("get_visit_transcript", "Transcript turns from today's visit about one topic. The full transcript is already in your prompt, so you rarely need this; don't call it once per barrier.", {
        "visit_id": {"type": "string"},
        "topic": {"enum": list(BARRIERS) + ["any"]},
    }, ["visit_id", "topic"]),
    "get_screener": _tool("get_screener", "Most recent social-needs screener answers (CMS AHC questions) with the date taken.",
                          {"patient_id": PATIENT_ID}, ["patient_id"]),
    "get_tract_profile": _tool("get_tract_profile", "Neighborhood data for a census tract: share of households with no vehicle, transit stops nearby and early-morning service, USDA low-income/low-access flags, CDC PLACES measures.",
                               {"tract_id": {"type": "string", "pattern": "^[0-9]{11}$"}}, ["tract_id"]),
    "find_resources": _tool("find_resources", "Community resources near a census tract for one need, optionally open on given days and times. This is the only call that leaves the private zone, so it accepts only a tract, a need and a time window.", {
        "tract_id": {"type": "string", "pattern": "^[0-9]{11}$"},
        "need": {"enum": list(NEEDS)},
        "days": {"type": "array", "items": {"enum": list(DAYS)}, "maxItems": 7},
        "start": {"type": "string", "pattern": HHMM},
        "end": {"type": "string", "pattern": HHMM},
    }, ["tract_id", "need"]),
}
SUBMIT_SPEC = {"type": "function", "function": {
    "name": "submit_assessment",
    "description": "Submit the final assessment: one entry per draft plan item. Call exactly once.",
    "parameters": ASSESSMENT_SCHEMA}}
SQL_SPEC = _tool("run_sql", "Run one read-only SQLite SELECT query over the patient record, transcript, tract and resource tables. Returns up to 50 rows.",
                 {"query": {"type": "string"}}, ["query"])

TOOL_SPECS["get_transit_trip"] = _tool(
    "get_transit_trip", "Can the patient reach an appointment's facility by bus/train in time? Uses weekday CTA and Pace "
    "schedules from the home tract, with walking and transfers. Returns the latest departure that arrives on time (or "
    "feasible=false) and the trip length. Call it once per facility and time: recurring appointments at the same "
    "place share one answer. Cite as transit:<appt ref>.",
    {"appt_ref": {"type": "string", "pattern": "^appt:[0-9]+$"},
     "arrive_by": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$", "description": "HH:MM; defaults to the appointment time"}},
    ["appt_ref"])

LOCAL_TOOLS = ["get_patient", "get_problems", "get_meds", "get_appointments", "get_visit_transcript",
               "get_screener", "get_tract_profile", "get_transit_trip"]
ALL_TOOLS = LOCAL_TOOLS + ["find_resources"]


class ToolError(Exception):
    pass


class Toolbox:
    """Binds the tools to one case plus the neighborhood tables."""

    def __init__(self, case: Case, tracts: TractTable, resources: ResourceTable,
                 gateway: Callable[[dict], list[dict]] | None = None, signals: bool = False):
        self.case = case
        self.tracts = tracts
        self.resources = resources
        # signals=True: tool results carry the barrier rules' own verdicts ("copay $47 >= $30: supports med_cost") so the
        # model doesn't have to know the thresholds. The rules are ours (rules/barrier_rules.yaml); the model still decides
        # whether a signal affects a given plan item. Off by default so the baseline numbers stay reproducible.
        self.signals = signals
        # In the split design, find_resources goes through a gateway to the public zone.
        self.gateway = gateway
        self.calls: list[dict] = []

    # ---- helpers
    def _check_patient(self, patient_id: str):
        if patient_id != self.case.patient["patient_id"]:
            raise ToolError(f"unknown patient_id {patient_id!r}")

    def _visit_date(self) -> date:
        return date.fromisoformat(self.case.visit.get("date", date.today().isoformat()))

    # ---- tools
    def get_patient(self, patient_id: str) -> dict:
        self._check_patient(patient_id)
        p = self.case.patient
        dob = date.fromisoformat(p["dob"])
        vd = self._visit_date()
        age = vd.year - dob.year - ((vd.month, vd.day) < (dob.month, dob.day))
        return {"patient_id": p["patient_id"], "age": age, "sex": p.get("sex"), "language": p.get("language", "en"),
                "coverage": p["coverage"], "pcp": p.get("pcp"), "home_tract": p.get("tract_geoid")}

    def get_problems(self, patient_id: str) -> list[dict]:
        self._check_patient(patient_id)
        return strip_hidden(self.case.patient.get("problems", []))

    def get_meds(self, patient_id: str) -> list[dict]:
        self._check_patient(patient_id)
        meds = strip_hidden(self.case.patient.get("meds", []))
        if self.signals:
            for m in meds:
                sig = []
                copay, tier = m.get("monthly_copay_usd") or 0, m.get("cost_tier") or 0
                if copay >= 30 or tier >= 4:
                    sig.append(f"high cost: ${copay:g}/month, tier {tier} (threshold $30 or tier 4): supports med_cost on items that need this drug")
                if (m.get("refill_gap_days") or 0) >= 7:
                    sig.append(f"refill gap {m['refill_gap_days']} days: weak signal for med_cost or transport")
                if m.get("needs_refrigeration"):
                    sig.append("needs refrigeration: supporting signal for utilities/housing if power or a fridge is in doubt")
                if sig:
                    m["signals"] = sig
        return meds

    def get_appointments(self, patient_id: str, dept: str | None = None, window_days: int = 90) -> list[dict]:
        self._check_patient(patient_id)
        cutoff = self._visit_date() - timedelta(days=window_days)
        out = []
        for a in self.case.patient.get("appointments", []):
            if dept and a["dept"].lower() != dept.lower():
                continue
            if a["status"] != "scheduled" and date.fromisoformat(a["date"]) < cutoff:
                continue
            a = strip_hidden(a)
            if self.signals and a.get("status") == "no_show":
                a["signal"] = "no-show: weak signal for transport, caregiving or work_schedule; check get_transit_trip for this appointment"
            out.append(a)
        if self.signals:
            n = sum(1 for a in out if a.get("status") == "no_show")
            if n >= 2:
                out.append({"summary": f"{n} no-shows in the window: a pattern, not a one-off"})
        return out

    def get_visit_transcript(self, visit_id: str, topic: str) -> list[dict]:
        if visit_id != self.case.visit["visit_id"]:
            raise ToolError(f"unknown visit_id {visit_id!r}")
        turns = strip_hidden(self.case.visit["transcript"])
        if topic == "any":
            return turns
        pat = re.compile(TOPIC_WORDS[topic], re.I)
        return [t for t in turns if pat.search(t["text"])]

    def get_screener(self, patient_id: str) -> dict:
        self._check_patient(patient_id)
        s = self.case.patient.get("screener") or {}
        answers = [{"ref": f"screener:{q}", "question": q, "answer": a} for q, a in (s.get("answers") or {}).items()]
        if self.signals:
            from .scoring import RULES
            for row in answers:
                rule = RULES["screener"].get(row["question"])
                if not rule:
                    continue
                pos = (row["answer"] in rule.get("when_any", [])) or ("when_none_of" in rule and row["answer"] not in rule["when_none_of"])
                if pos:
                    row["signal"] = f"positive: supports {', '.join(rule['supports'])}"
        return {"date": s.get("date"), "answers": answers}

    def get_tract_profile(self, tract_id: str) -> dict:
        row = self.tracts.get(tract_id)
        if row is None:
            raise ToolError(f"no data for tract {tract_id}")
        fields = {k: v for k, v in row.items() if k not in ("tract_geoid", "intpt_lat", "intpt_lon") and v is not None}
        out = [{"ref": f"tract:{k}", "field": k, "value": v} for k, v in fields.items()]
        if self.signals:
            from .scoring import RULES, _cmp
            for f in out:
                rule = RULES["tract_fields"].get(f["field"])
                if rule and _cmp(f["value"], rule["rule"]):
                    f["signal"] = f"above the Cook County 75th percentile ({rule['rule']}): supports {', '.join(rule['supports'])}"
        return {"tract_id": row["tract_geoid"], "fields": out}

    def get_transit_trip(self, appt_ref: str, arrive_by: str | None = None) -> dict:
        from .transit_tool import trip
        appt = self.case.refs.get(appt_ref)
        if not appt or not appt_ref.startswith("appt:"):
            raise ToolError(f"unknown appointment {appt_ref!r}")
        row = self.tracts.get(self.case.patient.get("tract_geoid"))
        if not appt.get("facility_ccn") or row is None or row.get("intpt_lat") is None:
            raise ToolError("no facility location on file for this appointment")
        arrive_by = arrive_by or appt.get("time")
        if not arrive_by:
            raise ToolError("appointment has no time; pass arrive_by")
        key = (appt["facility_ccn"], arrive_by)
        seen = getattr(self, "_transit_seen", {})
        self._transit_seen = seen
        if key in seen:                                   # recurring visits: same answer, say so instead of recomputing
            return {"ref": f"transit:{appt_ref}", "same_as": seen[key],
                    "note": "same facility and time as an earlier call; that result applies here too"}
        seen[key] = f"transit:{appt_ref}"
        return {"ref": f"transit:{appt_ref}", **trip(row["intpt_lat"], row["intpt_lon"], appt["facility_ccn"], arrive_by)}

    def find_resources(self, tract_id: str, need: str, days: list[str] | None = None,
                       start: str | None = None, end: str | None = None) -> list[dict]:
        request = {"tract_id": tract_id, "need": need, "days": days, "start": start, "end": end}
        if self.gateway is not None:
            return self.gateway(request)
        row = self.tracts.get(tract_id)
        if row is None or row.get("intpt_lat") is None:
            raise ToolError(f"no location for tract {tract_id}")
        return self.resources.search(row["intpt_lat"], row["intpt_lon"], need, days, start, end)

    # ---- dispatch
    def call(self, name: str, args: dict) -> Any:
        spec = TOOL_SPECS.get(name)
        if spec is None or not hasattr(self, name):
            raise ToolError(f"unknown tool {name!r}")
        try:
            jsonschema.validate(args, spec["function"]["parameters"])
        except jsonschema.ValidationError as e:
            raise ToolError(f"invalid arguments for {name}: {e.message}") from None
        result = getattr(self, name)(**args)
        self.calls.append({"tool": name, "args": args})
        return result


KIND_SUPPORTS = {"turn": None, "note": None, "transit": ["transport"], "appt": ["transport", "caregiving", "work_schedule"],
                 "med": ["med_cost", "transport", "utilities"], "prob": []}


def evidence_kind_problems(obj: dict) -> list[str]:
    """Each flagged barrier needs at least one cited ref of a kind that can support it (a turn or note always can; a
    screener question or tract field only the barriers the rules say; appt -> transport/caregiving/work_schedule;
    med -> med_cost/transport/utilities; transit -> transport). Catches 'transport flagged, only the tract cited' cards."""
    from .scoring import RULES
    probs = []
    for it in obj.get("items", []):
        for b in it.get("barriers", []) or []:
            ok = False
            for ref in b.get("evidence", []):
                kind, _, key = str(ref).partition(":")
                if kind in ("turn", "note"):
                    ok = True
                elif kind == "screener":
                    ok = b["barrier"] in (RULES["screener"].get(key, {}).get("supports") or [])
                elif kind == "tract":
                    ok = b["barrier"] in (RULES["tract_fields"].get(key, {}).get("supports") or [])
                elif kind in KIND_SUPPORTS and KIND_SUPPORTS[kind] is not None:
                    ok = b["barrier"] in KIND_SUPPORTS[kind]
                if ok:
                    break
            if not ok and b.get("evidence"):
                probs.append(f"{it.get('item_id')}: evidence {b['evidence']} cannot support '{b['barrier']}'; cite a transcript turn, chart note, or a record/tract ref that bears on that barrier")
    return probs


def validate_assessment(obj: dict, plan_item_ids: list[str]) -> list[str]:
    errs = [e.message for e in jsonschema.Draft202012Validator(ASSESSMENT_SCHEMA).iter_errors(obj)]
    if errs:
        return errs
    got = [i["item_id"] for i in obj["items"]]
    missing = sorted(set(plan_item_ids) - set(got))
    extra = sorted(set(got) - set(plan_item_ids))
    if missing:
        errs.append(f"missing plan items: {missing}")
    if extra:
        errs.append(f"unknown plan items: {extra}")
    if len(got) != len(set(got)):
        errs.append("duplicate plan items")
    for i in obj["items"]:
        if i["status"] != "likely_ok" and not i.get("barriers"):
            errs.append(f"{i['item_id']} is {i['status']} but lists no barriers")
    return errs
