"""Procedural synthetic cases with known answers, for fine-tuning (never for evaluation).

Every case is generated from templates: a condition with a draft plan, real Cook County tracts,
and barriers whose evidence is placed deliberately in the visit (spoken), the chart (record) or
the neighborhood data (tract). Because the answer key comes from the generator rather than from
a model, training on these is not circular. Lynna's hand-written test cases stay the held-out exam.

    python -m careplan.synth --n 500 --seed 1 --out data/synth/cases.jsonl

v2 (2026-11-01): appointment histories at real facilities with transit evidence, varied dialogue, multi-source
conflicts, fewer tract-only cases, more all-OK plans. See docs/finetune-plan.md (e2).
v3 (2026-11-03): label policy aligned with the dev/T sets: a tract value alone is a decoy, never a conflict; tract-backed
conflicts carry a chart note (+ screener) too. v1's tract-only rule taught tuned Nano to over-flag.
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from . import ROOT
from .cases import B32, validate_case
from .data import TractTable
from .scoring import RULES, _cmp

FIRST = ["Alma", "Bernard", "Carmen", "Dwayne", "Elena", "Frank", "Gloria", "Hassan", "Irene", "Jamal", "Keisha",
         "Luis", "Marta", "Nathan", "Olga", "Pedro", "Queen", "Rashid", "Sonia", "Terrence", "Ursula", "Victor",
         "Wanda", "Xavier", "Yolanda", "Zeke"]
LAST = ["Abara", "Bello", "Cruz", "Dawkins", "Esposito", "Fofanah", "Grant", "Haddad", "Ibarra", "Jansen", "Kowal",
        "Lindqvist", "Mensah", "Nguyen", "Okonkwo", "Pruitt", "Quintero", "Rios", "Sutton", "Tran", "Underwood",
        "Vance", "Whitfield", "Yilmaz", "Zamora"]

# Each condition: problems, meds, and plan items. Plan item "needs" say which barriers can break it.
CONDITIONS = {
    "dialysis": {
        "problems": [("N18.6", "End stage renal disease"), ("Z99.2", "Dependence on renal dialysis")],
        "meds": [{"name": "sevelamer carbonate", "dose": "800 mg", "frequency": "three times daily with meals"}],
        "plan": [("appointment", "Continue in-center hemodialysis Monday/Wednesday/Friday at 6:00 AM", "MWF 06:00",
                  ["transport", "work_schedule", "caregiving"]),
                 ("diet", "Renal diet: low potassium and phosphorus, fresh produce from the low-potassium list", None, ["food_access"]),
                 ("medication", "Sevelamer 800 mg three times daily with meals", None, ["med_cost"]),
                 ("monitoring", "Daily weight and blood pressure log on paper", None, [])]},
    "insulin": {
        "problems": [("E11.65", "Type 2 diabetes mellitus with hyperglycemia")],
        "meds": [{"name": "insulin glargine pen", "dose": "24 units", "frequency": "nightly", "needs_refrigeration": True}],
        "plan": [("medication", "Insulin glargine 24 units nightly; keep unopened pens refrigerated", None, ["utilities", "housing", "med_cost"]),
                 ("education", "Diabetes self-management class, Tuesdays 10:00 AM for 4 weeks", "Tue 10:00 x4",
                  ["work_schedule", "caregiving", "transport", "language_literacy"]),
                 ("monitoring", "Continuous glucose monitor with smartphone app", None, ["digital_access"]),
                 ("diet", "Carbohydrate-consistent diet with vegetables and lean protein", None, ["food_access"])]},
    "chf": {
        "problems": [("I50.22", "Chronic systolic heart failure")],
        "meds": [{"name": "empagliflozin", "dose": "10 mg", "frequency": "daily"}],
        "plan": [("referral", "Cardiac rehabilitation at the hospital, Monday/Wednesday/Friday 8:00 AM for 12 weeks", "MWF 08:00",
                  ["transport", "work_schedule", "caregiving"]),
                 ("diet", "2 g sodium diet with fresh vegetables; limit canned and processed foods", None, ["food_access"]),
                 ("medication", "Continue empagliflozin 10 mg daily", None, ["med_cost"]),
                 ("monitoring", "Daily morning weight on a home scale", None, [])]},
    "asthma": {
        "problems": [("J45.41", "Moderate persistent asthma with acute exacerbation")],
        "meds": [{"name": "albuterol nebulizer solution", "dose": "2.5 mg", "frequency": "every 4 hours as needed"}],
        "plan": [("medication", "Albuterol by home nebulizer during flares", None, ["utilities"]),
                 ("appointment", "Pulmonary function testing at the hospital lab, weekday 8:00 AM", "weekday 08:00",
                  ["work_schedule", "transport", "caregiving"]),
                 ("education", "Written asthma action plan", None, ["language_literacy"]),
                 ("medication", "Budesonide-formoterol 2 puffs twice daily", None, ["med_cost"])]},
    "afib": {
        "problems": [("I48.91", "Unspecified atrial fibrillation")],
        "meds": [{"name": "warfarin", "dose": "5 mg", "frequency": "daily"}],
        "plan": [("monitoring", "Weekly INR checks at the hospital anticoagulation clinic for 6 weeks", "weekly x6",
                  ["caregiving", "transport", "work_schedule"]),
                 ("medication", "Warfarin 5 mg daily", None, []),
                 ("activity", "Daily walking and balance exercises", None, []),
                 ("monitoring", "Video visit in 2 weeks to review INR results", None, ["digital_access"])]},
}

# How each barrier shows up in each evidence location. Placeholders are filled per case.
# v2 (2026-11-01): several wordings per barrier plus a doctor question per barrier, so the visit reads like the dev cases
# rather than one fixed sentence; appointment histories at real facilities give record-only transport evidence
# (no-shows + an infeasible transit trip), the pattern the dev set has and v1 lacked.
SPOKEN = {
    "transport": ["I don't have a car anymore. It's two buses to get anywhere.",
                  "I sold the car in August. Getting there means a bus and a long walk.",
                  "My neighbor drove me a couple of times, but he works days now."],
    "food_access": ["There's no real grocery near me. Mostly the corner store.",
                    "Fresh vegetables? The nearest store with produce is a bus ride away.",
                    "I eat what the corner store has. Chips, lunch meat, that kind of thing."],
    "med_cost": ["That new medicine was almost fifty dollars. I couldn't fill it this month.",
                 "The pharmacy wanted sixty for it. I split the pills to make them last.",
                 "I skipped the refill. Rent came first."],
    "utilities": ["We got a shutoff notice from the electric company.",
                  "The power's been off twice this month. The fridge is a problem.",
                  "ComEd sent a disconnect letter. I'm behind two months."],
    "housing": ["I've been staying at a shelter since the summer.",
                "I'm between places right now, sleeping at my cousin's some nights.",
                "The landlord gave us thirty days. I don't know where we'll be."],
    "caregiving": ["I take care of my mother. She can't be left alone.",
                   "My grandkids are with me after school every day. Nobody else can watch them.",
                   "My husband has dementia. If I'm gone more than an hour it's a problem."],
    "work_schedule": ["I work days at the warehouse. If I miss a shift, I lose the hours.",
                      "I'm on the line from six to three. They don't give time off for appointments.",
                      "I drive for a delivery company. Mornings are when I make my money."],
    "language_literacy": ["Can you explain it again? I don't read English well.",
                          "The papers they gave me last time, I couldn't follow them.",
                          "My daughter usually translates for me, but she's at work."],
    "digital_access": ["I don't have a smartphone, just a flip phone.",
                       "There's no internet at the apartment. I use the library's.",
                       "I have a phone but it's prepaid and the data runs out."],
}
ASK = {
    "transport": "How do you get to your appointments?", "food_access": "Tell me about what you're eating most days.",
    "med_cost": "Any trouble paying for your medicines?", "utilities": "Is everything okay with the power and heat at home?",
    "housing": "Where are you staying these days?", "caregiving": "Is anyone depending on you at home?",
    "work_schedule": "What are your work hours like right now?", "language_literacy": "Were the written instructions clear?",
    "digital_access": "Do you have a phone that can run an app?",
}
HERRING = [("doctor", "Is anyone able to come with you to visits?"), ("patient", "My daughter comes when she can. She's got two kids."),
           ("doctor", "How's your sleep?"), ("patient", "Fine. I'm up early anyway."),
           ("patient", "I walk to church on Sundays. It's about six blocks."),
           ("doctor", "Any questions about the plan so far?"), ("patient", "Not yet. Let me think on it.")]
APPT_TEMPLATE = {   # condition -> (dept, appointment time, how many past visits, location label)
    "dialysis": ("dialysis", "06:00", 6, "dialysis unit"), "chf": ("cardiac_rehab", "08:00", 4, "hospital cardiac rehab"),
    "asthma": ("pulmonology", "08:00", 2, "hospital pulmonary lab"), "afib": ("anticoagulation", "09:00", 4, "anticoagulation clinic"),
    "insulin": ("diabetes_education", "10:00", 3, "diabetes education center"),
}


def _facilities() -> list[dict]:
    from .transit_tool import _facilities as fac
    return list(fac().values())


def _transit_blocks(tract: dict, ccn: str, when: str) -> bool | None:
    """Same rule as scoring (transit_tool.supports_transport): no on-time trip, >= 60 min, or leaving before 5 AM."""
    from .transit_tool import trip
    if tract.get("intpt_lat") is None:
        return None
    res = trip(tract["intpt_lat"], tract["intpt_lon"], ccn, when)
    if "error" in res:
        return None
    return (not res["feasible"]) or res["trip_minutes"] >= 60 or res["latest_departure"] < "05:00"


NOTES = {
    "transport": ("social_work", "Patient reports no vehicle; relies on buses."),
    "food_access": ("social_work", "Patient reports limited access to groceries; shops at a convenience store."),
    "utilities": ("social_work", "Patient received an electric disconnection notice; balance past due."),
    "housing": ("community_health_worker", "Staying at a shelter; no personal refrigerator; no reliable mailing address."),
    "caregiving": ("social_work", "Primary caregiver for a parent with dementia who cannot be left alone."),
    "work_schedule": ("social_work", "Works weekday day shifts, hourly, no paid sick time."),
    "language_literacy": ("nursing", "Prefers Spanish; limited English reading; interpreter needed."),
    "digital_access": ("nursing", "No smartphone or home internet."),
}
SCREENER_SIGNAL = {"transport": ("transportation", "yes"), "food_access": ("food_worry", "often_true"),
                   "utilities": ("utilities_shutoff", "yes"), "housing": ("living_situation", "no_steady_place")}
TRACT_FIELDS = {"transport": ["no_vehicle_share", "places_lacktrpt"], "food_access": ["places_foodinsecu"],
                "utilities": ["places_shututility"], "housing": ["places_housinsecu"]}
ALTS = {"transport": ["nemt", "paratransit", "schedule_change"], "food_access": ["food_pantry", "snap", "medically_tailored_meals"],
        "med_cost": ["med_assistance_program", "generic_or_formulary_switch"], "utilities": ["utility_assistance", "medical_certificate_utilities"],
        "housing": ["housing_assistance", "community_health_worker"], "caregiving": ["caregiver_support", "schedule_change"],
        "work_schedule": ["flexible_scheduling", "telehealth"], "language_literacy": ["interpreter", "plain_language_materials"],
        "digital_access": ["phone_followup", "telehealth"]}
FILLER = [("doctor", "How have you been feeling since the last visit?"), ("patient", "About the same. Some days are better."),
          ("doctor", "Any new symptoms I should know about?"), ("patient", "No, nothing new."),
          ("doctor", "Are you taking your medicines as prescribed?"), ("patient", "I try to. Most days.")]


@dataclass
class Planted:
    item_id: str
    barrier: str
    where: str            # spoken | record | tract
    refs: list = field(default_factory=list)


def _tract_supports(row: dict, barrier: str) -> list[str]:
    out = []
    for f in TRACT_FIELDS.get(barrier, []):
        rule = RULES["tract_fields"].get(f)
        if rule and row.get(f) is not None and _cmp(row[f], rule["rule"]):
            out.append(f)
    return out


def generate(rng: random.Random, idx: int, tracts: TractTable) -> dict:
    cond_name = rng.choice(sorted(CONDITIONS))
    cond = CONDITIONS[cond_name]
    rows = list(tracts.rows.values())
    high_need = [r for r in rows if _tract_supports(r, "transport") and _tract_supports(r, "food_access")]
    tract = rng.choice(high_need if high_need and rng.random() < 0.3 else rows)   # tract-only evidence occurs, but doesn't dominate
    cid, pid, vid = f"S{idx:05d}", f"P-S{idx:05d}", f"V-S{idx:05d}"
    plan = [{"item_id": f"P{i}", "type": t, "text": txt, **({"schedule": s} if s else {})}
            for i, (t, txt, s, _) in enumerate(cond["plan"], start=1)]
    breakable = [(f"P{i}", b) for i, (_, _, _, bs) in enumerate(cond["plan"], start=1) for b in bs]
    rng.shuffle(breakable)
    n_conf = rng.choice([0, 1, 1, 2, 2, 2, 3])
    if n_conf == 0:          # an all-OK plan: pick a tract that supports none of this plan's barriers, so the key stays empty
        quiet = [r for r in rows if not any(_tract_supports(r, b) for (_, _, _, bs) in cond["plan"] for b in bs)]
        tract = rng.choice(quiet) if quiet else tract
    appt_dept, appt_time, n_past, appt_label = APPT_TEMPLATE[cond_name]
    facs = _facilities()
    fac = rng.choice(facs) if facs else None
    planted: list[Planted] = []
    used_barriers, used_items = set(), set()
    for item, barrier in breakable:
        if len(planted) >= n_conf:
            break
        if barrier in used_barriers or item in used_items:
            continue
        # v3 policy (2026-11-03): a neighborhood value alone never makes a conflict; it needs the patient to corroborate it
        # (screener answer or chart note). Matches the dev set, the T-set and the clinician pre-review; v1's tract-only
        # conflicts taught tuned Nano "tract above threshold => flag" and 1.0-1.3 false flags per case on dev.
        places = ["spoken", "record"] + (["tract+record"] if _tract_supports(tract, barrier) and barrier in NOTES else [])
        if barrier == "med_cost":
            places = ["spoken", "record"]
        if barrier == "transport" and fac is not None and _transit_blocks(tract, fac["ccn"], appt_time):
            places += ["appointments", "appointments"]          # the hero pattern: missed visits + a trip the schedules can't make
        planted.append(Planted(item, barrier, rng.choice(places)))
        used_barriers.add(barrier)
        used_items.add(item)
    # Tract signals that nothing corroborates are decoys, not conflicts (v3 policy; v1 planted them all as tract-only).
    tract_decoys = []
    for i, (_, _, _, bs) in enumerate(cond["plan"], start=1):
        for b in bs:
            if b in TRACT_FIELDS and _tract_supports(tract, b) and b not in used_barriers:
                tract_decoys.append(b)
                used_barriers.add(b)                 # so no spoken decoy duplicates it
    meds = [dict(m, ref=f"med:{i}", cost_tier=1, monthly_copay_usd=2.0, refill_gap_days=0)
            for i, m in enumerate(cond["meds"], start=1)]
    # Appointment history at a real facility (dates before the 2026-10-20 visit). Transport planted "in the appointments"
    # turns some of them into no-shows; the transit check at the plan's time is then the strong evidence.
    appts = []
    miss = {p.item_id for p in planted if p.where == "appointments"}
    for k in range(n_past):
        day = 20 - 3 * (n_past - k)
        appts.append({"ref": f"appt:{k + 1}", "date": f"2026-{10 if day > 0 else 9}-{(day if day > 0 else day + 30):02d}", "dept": appt_dept,
                      "time": appt_time, "location": f"{fac['name']}, {fac['address'].split(',')[0]}" if fac else appt_label,
                      **({"facility_ccn": fac["ccn"]} if fac else {}),
                      "status": "no_show" if (miss and k >= n_past - 3 and rng.random() < 0.8) else "kept"})
    appts.append({"ref": f"appt:{n_past + 1}", "date": "2026-10-01", "dept": "primary_care", "time": "09:00", "location": "Clinic", "status": "kept"})
    notes, turns, screener = [], [], {"living_situation": "steady_place", "housing_problems": "none", "food_worry": "never_true",
                                      "food_ran_out": "never_true", "transportation": "no", "utilities_shutoff": "no"}
    filler = list(FILLER) + [h for h in HERRING if rng.random() < 0.5]
    rng.shuffle(filler)
    for spk, txt in filler[:rng.choice([3, 4, 5])]:
        turns.append({"speaker": spk, "text": txt})
    for p in planted:
        if p.where == "spoken":
            turns.append({"speaker": "doctor", "text": ASK[p.barrier]})
            turns.append({"speaker": "patient", "text": rng.choice(SPOKEN[p.barrier]), "topics": [p.barrier]})
            p.refs.append(("transcript", len(turns)))
            if p.barrier in NOTES and rng.random() < 0.4:        # multi-source, like the dev cases: the chart says it too
                role, text = NOTES[p.barrier]
                notes.append({"ref": f"note:{len(notes) + 1}", "date": "2026-09-15", "role": role, "topics": [p.barrier], "text": text})
                p.refs.append(("record", notes[-1]["ref"]))
        elif p.where == "appointments":
            missed = [a["ref"] for a in appts if a["status"] == "no_show"]
            p.refs += [("record", r) for r in missed[:3]]
            p.refs.append(("record", f"transit:{appts[0]['ref']}"))
            if rng.random() < 0.5:                                # sometimes the visit hints at it without saying it
                turns.append({"speaker": "doctor", "text": "The unit says you've missed some sessions. What's been going on?"})
                turns.append({"speaker": "patient", "text": rng.choice(["It's a long way. Some mornings I just can't make it.",
                                                                        "The early start is the problem. Nothing runs that early where I am."]), "topics": ["transport"]})
                p.refs.append(("transcript", len(turns)))
        elif p.where == "record":
            if p.barrier == "med_cost":
                meds[0].update(cost_tier=rng.choice([3, 4]), monthly_copay_usd=rng.choice([38.0, 45.0, 60.0, 120.0]), refill_gap_days=rng.choice([0, 14, 21]))
                p.refs.append(("record", "med:1"))
            else:
                role, text = NOTES[p.barrier]
                notes.append({"ref": f"note:{len(notes) + 1}", "date": "2026-09-15", "role": role, "topics": [p.barrier], "text": text})
                p.refs.append(("record", notes[-1]["ref"]))
                if p.barrier in SCREENER_SIGNAL:
                    q, a = SCREENER_SIGNAL[p.barrier]
                    screener[q] = a
                    p.refs.append(("record", f"screener:{q}"))
        else:                                                 # tract+record: the neighborhood value, corroborated by the chart
            role, text = NOTES[p.barrier]
            notes.append({"ref": f"note:{len(notes) + 1}", "date": "2026-09-15", "role": role, "topics": [p.barrier], "text": text})
            p.refs.append(("record", notes[-1]["ref"]))
            if p.barrier in SCREENER_SIGNAL:
                q, a = SCREENER_SIGNAL[p.barrier]
                screener[q] = a
                p.refs.append(("record", f"screener:{q}"))
            for f in _tract_supports(tract, p.barrier):
                p.refs.append(("tract", f"tract:{f}"))
    # One decoy: a barrier that's present but affects no plan item.
    decoys = []
    free = [b for b in SPOKEN if b not in used_barriers and not any(b in bs for (_, _, _, bs) in cond["plan"])]
    if free and rng.random() < 0.7:
        b = rng.choice(free)
        turns.append({"speaker": "doctor", "text": ASK[b]})
        turns.append({"speaker": "patient", "text": rng.choice(SPOKEN[b]), "topics": [b]})
        decoys.append({"barrier": b, "ref": f"turn:T{len(turns):02d}", "why_irrelevant": "Present, but no plan item depends on it."})
    for b in sorted(set(tract_decoys)):
        f = _tract_supports(tract, b)[0]
        decoys.append({"barrier": b, "ref": f"tract:{f}",
                       "why_irrelevant": "Neighborhood value alone; nothing in the visit, screener or chart says this patient has the problem."})
    for i, t in enumerate(turns, start=1):
        t["ref"] = f"turn:T{i:02d}"
    conflicts = []
    for k, p in enumerate(planted, start=1):
        ev = [{"source": "transcript", "ref": f"turn:T{r:02d}"} if s == "transcript" else {"source": s, "ref": r}
              for s, r in p.refs]
        conflicts.append({"conflict_id": f"C{k}", "item_id": p.item_id, "barrier": p.barrier,
                          "expected_status": "blocked" if p.where == "appointments" and rng.random() < 0.5 else "at_risk", "evidence": ev,
                          "acceptable_alternative_categories": ALTS[p.barrier]})
    lang = "es" if any(p.barrier == "language_literacy" for p in planted) else "en"
    case = {
        "case_id": cid, "split": "dev", "title": f"Synthetic {cond_name} case", "author": "synth", "reviewed_by": None,
        "written_at": "2026-11-01", "frozen_sha256": None,
        "patient": {
            "patient_id": pid, "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "dob": f"{rng.randint(1945, 1995)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            "sex": rng.choice(["F", "M"]), "language": lang, "mrn": "MRN-" + "".join(rng.choice(B32) for _ in range(10)),
            "address": "Synthetic address, Chicago, IL", "tract_geoid": tract["tract_geoid"],
            "coverage": rng.choice([{"type": "medicaid", "plan": "Fictional Medicaid MCO", "transport_benefit": "yes"},
                                    {"type": "medicare", "plan": "Original Medicare", "transport_benefit": "no"},
                                    {"type": "dual", "plan": "Fictional dual plan", "transport_benefit": "yes"},
                                    {"type": "commercial", "plan": "Fictional employer PPO", "transport_benefit": "no"}]),
            "pcp": "Dr. Synthetic (fictional)",
            "problems": [{"ref": f"prob:{i}", "code": c, "system": "ICD-10-CM", "display": d}
                         for i, (c, d) in enumerate(cond["problems"], start=1)],
            "meds": meds,
            "appointments": appts,
            "screener": {"date": "2026-10-01", "answers": screener},
            "notes": notes,
        },
        "visit": {"visit_id": vid, "date": "2026-10-20", "setting": "clinic visit", "transcript": turns},
        "draft_plan": plan,
        "planted_conflicts": conflicts,
        "decoys": decoys,
        "expected_ok_items": [i["item_id"] for i in plan if i["item_id"] not in used_items],
    }
    return case


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "synth" / "cases.jsonl")
    a = ap.parse_args(argv)
    rng, tracts = random.Random(a.seed), TractTable.load()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    bad = 0
    with a.out.open("w") as f:
        for i in range(1, a.n + 1):
            case = generate(rng, i, tracts)
            errs = validate_case(case)
            if errs:
                bad += 1
                continue
            f.write(json.dumps(case) + "\n")
    print(f"wrote {a.n - bad} cases to {a.out} ({bad} failed validation)")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
