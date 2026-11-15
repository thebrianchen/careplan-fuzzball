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
  the problem or why it is hard; the patient knows.
- For clinician_edit alternatives, preserve every requested action. If the clinician says to explain a reason to an
  insurer, include that instruction. Keep any suitability checks, approval checks, and instructions to continue current
  treatment while waiting for a new plan."""


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


SAID = """Each line below is something said in a clinic visit, with its reference and the kind of barrier it was cited for.
For each one, write what it tells the doctor about the patient's situation, as one short plain statement in the third person,
eight words or fewer. Say the situation, not the anecdote: "Patient takes two buses to dialysis", "Patient no longer has a
car", "No reliable support at home". Use only what the line says. Do not add causes, advice or names.
Return ONLY JSON, keyed by the reference exactly as given: {{"lines": {{"<reference>": "<statement>"}}}}

{quotes}
"""


def said_lines(case: Case, quotes: dict[str, str], client: ChatClient) -> dict[str, str]:
    """One short statement of fact for each cited line of the visit, written by the planner. The page shows the statement
    and opens the line itself underneath. `quotes` maps an evidence ref to the line, with what it was cited for."""
    reply = client.chat([{"role": "user", "content": SAID.format(quotes="\n".join(f"[{r}] {q}" for r, q in quotes.items()))}], [])
    js = _first_json_object(reply.content or "")
    if not js:
        raise ValueError("the planner returned no JSON")
    short = {r.split(":")[-1]: r for r in quotes}          # the planner often answers "T02" for "turn:T02"
    lines = {}
    for r, v in (json.loads(js).get("lines") or {}).items():
        r = r if r in quotes else short.get(str(r).strip("[]").split(":")[-1])
        if r and str(v).strip():
            lines[r] = " ".join(str(v).split()).rstrip(".")
    if set(lines) != set(quotes) or any(len(v) > 90 for v in lines.values()):
        raise ValueError("the planner did not write a short statement for every line")
    for canary in case.canaries():
        if canary and canary in json.dumps(lines):
            raise ValueError("a statement contained a patient identifier; refused")
    return lines
