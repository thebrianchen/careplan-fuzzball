"""Patient-facing after-visit summary: the checked card rewritten in plain language, in the patient's language.

One extra model call after the card, no tools. Input is the card the clinician saw (statuses, reasons, alternatives
with real places and hours) plus the plan text; output is a short set of steps per plan item. Rules in the prompt
keep it to what the patient already knows: no diagnoses they weren't told, no record numbers, no new facts.
Runs on box 2 like everything else. The same JSON renders the printable sheet (careplan/web/templates/summary.html).
"""
from __future__ import annotations

import json

from .cases import Case
from .llm import ChatClient, _first_json_object

LANG = {"en": "English", "es": "Spanish", "pl": "Polish", "zh": "Chinese", "ar": "Arabic"}

PROMPT = """You write after-visit instructions for patients. A doctor's care plan was checked against what this patient can
realistically do. Turn the result into a short handout the patient can follow.

Rules:
- Write in {language}, at a 6th-grade reading level, second person ("you"). Short sentences.
- One section per plan item, in order, with a short heading in {language} too. For items marked likely_ok: one sentence saying to keep doing it, with the schedule.
{flagged}
- Use only facts in the input. Do not add medical advice, diagnoses, or anything the input doesn't say. Never include
  the patient's name, date of birth, or record numbers.
- Return ONLY JSON: {{"items": [{{"item_id": "P1", "heading": "...", "steps": ["...", "..."]}}], "closing": "one sentence"}}

Plan items:
{plan}

Checked card (JSON):
{card}
"""


WITH_WHY = """- For items at_risk or blocked: say plainly what makes it hard (from the reason), then 1-3 concrete steps from the
  alternatives: name the place, its hours, the phone number or how to start, the bus to leave by, exactly as given."""
STEPS_ONLY = """- For items at_risk or blocked: write one short step for each alternative in the input, as an instruction to the
  patient ("Call ...", "Go to ..."). Keep every name, address, day, hour and phone number exactly as given. Do not restate
  the problem or why it is hard; the patient knows."""


def build(case: Case, rec: dict, client: ChatClient, items: list[str] | None = None, why: bool = True) -> dict:
    """`items`, when given, are the plan items the handout covers (the ones the clinician was shown). `why=False` leaves
    out the sentence on what makes an item hard and keeps the steps."""
    lang = LANG.get((case.patient.get("language") or "en").lower(), "English")
    plan = "\n".join(f"- {i['item_id']}: {i['text']}" + (f" [{i['schedule']}]" if i.get("schedule") else "")
                     for i in case.plan if items is None or i["item_id"] in items)
    card = json.dumps(rec["assessment"], ensure_ascii=False)
    reply = client.chat([{"role": "user", "content": PROMPT.format(language=lang, plan=plan, card=card, flagged=WITH_WHY if why else STEPS_ONLY)}], [])
    js = _first_json_object(reply.content or "")
    if not js:
        raise ValueError("summary model returned no JSON")
    out = json.loads(js)
    known = {i["item_id"] for i in case.plan if items is None or i["item_id"] in items}
    out["items"] = [it for it in out.get("items", []) if it.get("item_id") in known and isinstance(it.get("steps"), list)]
    out["language"] = lang
    out["profile"] = client.p.name
    for canary in case.canaries():                     # belt and braces: the handout must not carry identifiers
        if canary and canary in json.dumps(out):
            raise ValueError("summary contained a patient identifier; refused")
    return out
