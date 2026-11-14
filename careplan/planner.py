"""The planner agent loop and the E1 conditions (C0-C4)."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml

from . import ROOT
from .cases import Case
from .data import ResourceTable, TractTable
from .llm import ChatClient, FakeClient, Reply, _first_json_object
from .sqltool import SCHEMA_DOC, build_db, run_sql
from .stream import ItemStreamer
from .tools import ALL_TOOLS, SQL_SPEC, SUBMIT_SPEC, TOOL_SPECS, ToolError, Toolbox, validate_assessment

PROMPT_PATH = ROOT / "prompts" / "planner_system.md"
PROGRAMS_PATH = ROOT / "rules" / "programs.yaml"
MAX_RESULT_CHARS = 8000

CONDITIONS = {
    "C0": {"desc": "transcript + plan only, no tools", "mode": "none", "default_profile": "super-noreason"},
    "C1": {"desc": "free-form SQL over the same data", "mode": "sql", "default_profile": "super-noreason"},
    "C2": {"desc": "Care Plan Check: 8 typed tools", "mode": "tools", "default_profile": "super-noreason"},
    "C3": {"desc": "8 typed tools, smaller planner", "mode": "tools", "default_profile": "nano"},
    "C4": {"desc": "8 typed tools, reference model", "mode": "tools", "default_profile": "claude"},
}

DATA_INSTRUCTIONS = {
    "none": "   In this setting you have no access to the patient's record or to neighborhood data. Judge from the transcript and plan alone.",
    "sql": "   Use run_sql to look up the patient's record, screener, notes, neighborhood (tract) data and community resources.\n" + "\n".join("   " + l for l in SCHEMA_DOC.splitlines()),
    "tools": "   Use the tools to check the patient's record (get_patient, get_problems, get_meds, get_appointments, get_screener), the neighborhood (get_tract_profile with the patient's home_tract) and community resources (find_resources). get_visit_transcript returns turns on a topic if you need to re-read them. For appointments with a facility on file, get_transit_trip checks whether the patient can arrive on time by bus or train; when you use it, cite its transit:<appt ref> and state the latest departure and ride length it returns.\n"
             "   Before suggesting community alternatives for an at_risk or blocked item, call find_resources for that need with the patient's home_tract, passing the days (and times, if known) the patient can actually go, e.g. not their treatment or work days. Prefer the returned records, mention their hours, and put their record_id in resource_record_id.\n"
             "   Call independent tools together in a single turn (for example get_patient, get_problems, get_meds, get_appointments and get_screener at once), then find_resources for each flagged need in one turn. Fewer turns makes the card faster.\n"
             "   Keep the card short: each reason under 20 words, each alternative under 25 words (name the place, its hours and how to start). Clinicians skim.",
}


FINAL_STEP = {
    "tool": "Call submit_assessment exactly once, with one entry per plan item.",
    "text": 'When you have what you need, reply with the final assessment as one JSON object and nothing else (no tool '
            'call), one entry per plan item, in this shape: {"items": [{"item_id": "P1", "status": "likely_ok"}, '
            '{"item_id": "P2", "status": "at_risk", "barriers": [{"barrier": "transport", "reason": "...", '
            '"evidence": ["turn:T04", "tract:no_vehicle_share"]}], "alternatives": [{"category": "...", '
            '"resource_record_id": null, "text": "..."}]}]}. Write the items in plan order, one at a time.',
}
SUBMIT_WORDING = {"tool": "call submit_assessment", "text": "reply with the final assessment JSON"}


def _wording(text: str, final: str) -> str:
    """Prompts are written for the tool-call ending; in text mode the final answer is streamed JSON content
    (the qwen3_coder parser releases tool arguments per parameter, so a tool-call card can't stream item by item)."""
    return text.replace("call submit_assessment", SUBMIT_WORDING[final]) if final == "text" else text


def render_programs() -> str:
    progs = yaml.safe_load(PROGRAMS_PATH.read_text())["programs"]
    lines = []
    for p in progs:
        lines.append(f"- {p['id']} (category: {p['category']}; for: {', '.join(p['needs'])}). "
                     f"Eligible when: {p['eligible_when']}. How to start: {p['how_to_start']}"
                     + (f" Caveats: {p['caveats']}" if p.get("caveats") else ""))
    return "\n".join(lines)


def system_prompt(mode: str, final: str = "tool") -> str:
    return PROMPT_PATH.read_text().format(data_instructions=_wording(DATA_INSTRUCTIONS[mode], final),
                                          programs=render_programs(), final_step=FINAL_STEP[final])


def user_prompt(case: Case, final: str = "tool") -> str:
    v, p = case.visit, case.patient
    lines = [f"Patient id: {p['patient_id']}. Visit id: {v['visit_id']} on {v.get('date', 'today')} ({v.get('setting', 'clinic visit')}).",
             "", "Visit transcript (cite turns by their ref):"]
    lines += [f"[{t['ref']}] {t['speaker']}: {t['text']}" for t in v["transcript"]]
    lines += ["", "Draft care plan:"]
    for item in case.plan:
        sched = f" [schedule: {item['schedule']}]" if item.get("schedule") else ""
        lines.append(f"- {item['item_id']} ({item['type']}): {item['text']}{sched}")
    lines += ["", _wording("Assess every plan item, then call submit_assessment.", final)]
    return "\n".join(lines)


@dataclass
class RunResult:
    case_id: str
    condition: str
    assessment: dict | None
    error: str | None
    steps: int
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    latency_ms: int = 0
    log: list[dict] = field(default_factory=list)


def _assistant_msg(reply: Reply) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": reply.content or ""}
    if reply.tool_calls:
        msg["tool_calls"] = [{"id": c.id, "type": "function",
                              "function": {"name": c.name, "arguments": c.raw_arguments or "{}"}} for c in reply.tool_calls]
    return msg


def _tool_msg(call_id: str, name: str, out: Any) -> dict:
    text = json.dumps(out, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "...(truncated)"
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": text}


def _call_key(name: str, args: Any) -> str:
    return name + json.dumps(args, sort_keys=True, default=str)


SIGNALS_NOTE = ("Some tool results carry a 'signal' note derived from fixed rules (a copay over the threshold, a no-show "
                "pattern, a positive screener answer, a neighborhood value above the county's 75th percentile). A signal is "
                "a reason to LOOK, not a reason to flag by itself. Match each signal to the plan items it bears on: a transport "
                "signal to items that require travel, a medication-cost signal to the item that prescribes that drug, a food "
                "signal to diet items. A signal that touches no item in this plan is not a conflict; leave those items "
                "likely_ok. Cite only evidence that is about this item.")

PREFETCH_TOOLS = ["get_patient", "get_problems", "get_meds", "get_appointments", "get_screener"]
PREFETCH_NOTE = ("The patient's record (get_patient, get_problems, get_meds, get_appointments, get_screener) and the "
                 "home-tract profile were already retrieved; their results follow, so do not call those tools again. "
                 "Read them as carefully as the transcript: copays and refill gaps in the meds, no-shows in the "
                 "appointments, the screener answers and the tract values can each be evidence on their own, even when "
                 "the visit never mentions the problem. Then call get_transit_trip for appointments at a facility and "
                 "find_resources for each need you flag, and then call submit_assessment.")


def prefetch_record(case: Case, toolbox: Toolbox, messages: list[dict], res: RunResult,
                    seen_calls: set[str], emit: Callable[[dict], None]) -> None:
    """Seed the conversation with the record reads every card makes anyway, as if the model had made them in
    one batched turn. Saves the model a full decode + re-prefill step on the box (H5 lever)."""
    pid, tract = case.patient["patient_id"], case.patient.get("tract_geoid")
    first = [(n, {"patient_id": pid}) for n in PREFETCH_TOOLS]
    if tract:
        first.append(("get_tract_profile", {"tract_id": tract}))
    calls, results = [], []
    for i, (name, args) in enumerate(first):
        cid = f"pre_{i}"
        try:
            out: Any = toolbox.call(name, args)
            ok = True
        except ToolError as e:
            out, ok = {"error": str(e)}, False
        calls.append({"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}})
        results.append(_tool_msg(cid, name, out))
        res.tool_calls.append({"tool": name, "args": args, "ok": ok, "prefetched": True})
        seen_calls.add(_call_key(name, args))
        emit({"type": "tool", "tool": name, "args": args, "n_results": len(out) if isinstance(out, list) else None,
              "prefetched": True})
    messages.append({"role": "assistant", "content": "", "tool_calls": calls})
    messages.extend(results)


def _final_from_text(content: str) -> dict | None:
    js = _first_json_object(content)
    if not js:
        return None
    obj = json.loads(js)
    return obj if isinstance(obj, dict) and "items" in obj else None


def _env_gateway() -> Callable[[dict], list[dict]] | None:
    """Batch runs cross to the public zone the same way the card server does when CAREPLAN_GATEWAY_URL is set
    (on the cluster: the gateway's Fuzzball endpoint, with an endpoint token in CAREPLAN_GATEWAY_TOKEN)."""
    url = os.environ.get("CAREPLAN_GATEWAY_URL")
    if not url:
        return None
    from .gateway import http_gateway
    return http_gateway(url, os.environ.get("CAREPLAN_GATEWAY_TOKEN"))


def run_case(case: Case, condition: str, client: ChatClient | FakeClient, tracts: TractTable,
             resources: ResourceTable, max_steps: int = 16, toolbox: Toolbox | None = None,
             on_event: Callable[[dict], None] | None = None, prefetch: bool = False, final: str = "tool",
             signals: bool = False) -> RunResult:
    """on_event, if given, receives progress events: {"type": "step"|"tool"|"item"|"done", ...}. Items stream
    out of the final answer as each one is written (careplan.stream.ItemStreamer).
    prefetch (tools mode only) runs the record reads before the first model call; see prefetch_record.
    final="tool": the card is a submit_assessment tool call (the E1 baseline). final="text": the card is a JSON
    object in the reply content, which streams item by item on vLLM; a submit_assessment call is still accepted."""
    cfg = CONDITIONS[condition]
    mode = cfg["mode"]
    toolbox = toolbox or Toolbox(case, tracts, resources, gateway=_env_gateway(), signals=signals)
    if signals:
        toolbox.signals = True
    tools = [SUBMIT_SPEC] if final == "tool" else []
    con = None
    if mode == "tools":
        tools = [TOOL_SPECS[n] for n in ALL_TOOLS] + tools
    elif mode == "sql":
        con = build_db(case, tracts, resources)
        tools = [SQL_SPEC] + tools
    prefetch = prefetch and mode == "tools"
    sys_p = system_prompt(mode, final)
    if signals:
        sys_p = sys_p + "\n\n" + SIGNALS_NOTE
    user_p = user_prompt(case, final) + ("\n\n" + _wording(PREFETCH_NOTE, final) if prefetch else "")
    messages: list[dict] = [{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}]
    res = RunResult(case.case_id, condition, None, None, 0)
    res.log.append({"prompt_sha256": hashlib.sha256((sys_p + user_p).encode()).hexdigest(), "prefetch": prefetch,
                    "final": final})
    plan_ids = [i["item_id"] for i in case.plan]
    nudges = 0
    seen_calls: set[str] = set()
    emit = on_event or (lambda e: None)
    if prefetch:
        prefetch_record(case, toolbox, messages, res, seen_calls, emit)

    def accept(obj: Any, step: int) -> list[str]:
        errs = validate_assessment(obj, plan_ids)
        if not errs and toolbox.signals:
            from .tools import evidence_kind_problems
            errs = evidence_kind_problems(obj)
        if not errs:
            res.assessment = obj
            emit({"type": "done", "assessment": obj, "steps": step})
        else:
            emit({"type": "rejected", "problems": errs[:10]})
        return errs

    for step in range(1, max_steps + 1):
        res.steps = step
        emit({"type": "step", "step": step})
        if on_event and hasattr(client, "chat_stream"):
            streamers: dict[int, ItemStreamer] = {}

            def on_delta(idx: int, name: str, frag: str) -> None:
                if name == "submit_assessment" or (final == "text" and name == ""):
                    for item in streamers.setdefault(idx, ItemStreamer()).feed(frag):
                        emit({"type": "item", "item": item})
            reply = client.chat_stream(messages, tools, on_delta)
        else:
            reply = client.chat(messages, tools)
        res.latency_ms += reply.latency_ms
        for k in ("prompt_tokens", "completion_tokens"):
            res.usage[k] += int(reply.usage.get(k, 0) or 0)
        res.log.append({"step": step, "latency_ms": reply.latency_ms, "usage": reply.usage,
                        "served_model": reply.raw.get("model"), "provider": reply.raw.get("provider"),
                        "content": (reply.content or "")[:2000],
                        "tool_calls": [{"name": c.name, "args": c.arguments, "parse_error": c.parse_error} for c in reply.tool_calls]})
        if not reply.tool_calls:
            messages.append({"role": "assistant", "content": reply.content or ""})
            obj = _final_from_text(reply.content) if final == "text" else None
            if obj is not None:
                errs = accept(obj, step)
                if not errs:
                    return res
                messages.append({"role": "user", "content": "Assessment rejected; fix these problems and reply again with "
                                                            "the complete JSON: " + json.dumps(errs[:10])})
                continue
            nudges += 1
            if nudges > 2:
                res.error = "model stopped without " + ("calling submit_assessment" if final == "tool" else "a final assessment")
                return res
            messages.append({"role": "user", "content": _wording("Please call submit_assessment now with one entry per plan item.", final)})
            continue
        messages.append(_assistant_msg(reply))
        for call in reply.tool_calls:
            key = _call_key(call.name, call.arguments)
            if call.parse_error:
                out: Any = {"error": call.parse_error}
            elif call.name != "submit_assessment" and key in seen_calls:
                # Exact repeats waste steps (and seconds on the box); answer from what's already in context.
                out = {"error": _wording("you already made this exact call; its result is above. "
                                         "Use what you have and call submit_assessment.", final)}
            elif call.name == "submit_assessment":
                errs = accept(call.arguments, step)
                if not errs:
                    return res
                out = {"error": _wording("assessment rejected; fix and call submit_assessment again", final), "problems": errs[:10]}
            elif call.name == "run_sql" and con is not None:
                out = run_sql(con, str(call.arguments.get("query", "")))
                res.tool_calls.append({"tool": "run_sql", "args": call.arguments, "ok": "error" not in out})
            else:
                try:
                    out = toolbox.call(call.name, call.arguments)
                    res.tool_calls.append({"tool": call.name, "args": call.arguments, "ok": True})
                    emit({"type": "tool", "tool": call.name, "args": call.arguments,
                          "n_results": len(out) if isinstance(out, list) else None})
                except ToolError as e:
                    out = {"error": str(e)}
                    res.tool_calls.append({"tool": call.name, "args": call.arguments, "ok": False})
            seen_calls.add(key)
            messages.append(_tool_msg(call.id, call.name, out))
        if max_steps - step == 3:
            # Seen on D005 (all-OK plan): one transcript call per barrier until the steps ran out.
            messages.append({"role": "user", "content": _wording("You have 3 steps left. Stop gathering and call "
                                                                 "submit_assessment with one entry per plan item.", final)})
    res.error = f"no valid assessment after {max_steps} steps"
    return res
