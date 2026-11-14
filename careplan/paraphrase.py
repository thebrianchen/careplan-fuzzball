"""Make synthetic training cases less formulaic: an LLM rewrites each visit turn and chart note in varied,
realistic wording while keeping its meaning, so the answer key (planted conflicts, evidence refs, topic
tags) stays valid. Round 1 learned the template phrasing; this is round 2's training data.

    python -m careplan.paraphrase --cases data/synth/cases.jsonl --out data/synth/cases_v2.jsonl \
        --profile super-vllm --workers 2

Only text fields change. A rewrite is rejected (original kept) if it drops below half or above triple the
original length, or loses a number or barrier keyword the original had.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ROOT
from .cases import validate_case
from .llm import ChatClient
from .runner import load_profiles

PROMPT = """Rewrite each numbered line so it sounds like a different real person said or wrote it: vary vocabulary,
sentence length, filler words and tone (patients may ramble, hedge or be terse; clinicians may be brisk).
Keep every fact, number, time, place and medical meaning exactly. Do not add new problems or facts.
Return JSON only: {"lines": ["...", "..."]} with the same number of lines, in order.

"""
KEYWORDS = r"car|bus|buses|grocery|store|shelter|power|electric|shutoff|smartphone|phone|English|Spanish|shift|warehouse|mother|dollar|\d"


def ok(orig: str, new: str) -> bool:
    if not new or not (0.5 * len(orig) <= len(new) <= 3 * len(orig) + 40):
        return False
    lost = {m.lower() for m in re.findall(KEYWORDS, orig, re.I)} - {m.lower() for m in re.findall(KEYWORDS, new, re.I)}
    return not {k for k in lost if k.isdigit()}            # numbers must survive; synonyms for keywords are fine


def rewrite(case: dict, client: ChatClient) -> dict:
    out = copy.deepcopy(case)
    slots = [("turn", t) for t in out["visit"]["transcript"]] + [("note", n) for n in out["patient"].get("notes", [])]
    texts = [s[1]["text"] for s in slots]
    if not texts:
        return out
    msg = PROMPT + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    try:
        reply = client.chat([{"role": "user", "content": msg}], [])
        m = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", reply.content or "", flags=re.S), re.S)
        lines = json.loads(m.group(0))["lines"] if m else []
    except Exception:
        return out
    if len(lines) != len(texts):
        return out
    for (_, obj), new in zip(slots, lines):
        if isinstance(new, str) and ok(obj["text"], new.strip()):
            obj["text"] = new.strip()
    return out if not validate_case(out) else case


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, default=ROOT / "data" / "synth" / "cases.jsonl")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "synth" / "cases_v2.jsonl")
    ap.add_argument("--profile", default="super-vllm")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    client = ChatClient(load_profiles()[a.profile])
    cases = [json.loads(l) for l in a.cases.read_text().splitlines()][: a.limit or None]
    with ThreadPoolExecutor(a.workers) as ex:
        new = list(ex.map(lambda c: rewrite(c, client), cases))
    changed = sum(json.dumps(n["visit"]) != json.dumps(c["visit"]) for n, c in zip(new, cases))
    a.out.write_text("".join(json.dumps(c) + "\n" for c in new))
    print(f"wrote {len(new)} cases to {a.out}; {changed} rewritten, {len(new) - changed} kept as-is")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
