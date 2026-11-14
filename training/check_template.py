"""Pre-flight (no GPU): render SFT examples through the target model's chat template and check that
tools, tool calls, tool results and the final submit_assessment all survive, and whether the template
marks assistant turns for assistant-only loss. Downloads only tokenizer files.

    .venv-train/bin/python training/check_template.py --model nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_lora import prepare  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--data", default="data/synth/sft/val.jsonl")
    ap.add_argument("--n", type=int, default=20)
    a = ap.parse_args()
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.model, trust_remote_code=True)
    rows = [prepare(json.loads(l)) for l in Path(a.data).read_text().splitlines()[: a.n]]
    lengths, problems = [], []
    for r in rows:
        text = tok.apply_chat_template(r["messages"], tools=r["tools"], tokenize=False)
        lengths.append(len(tok(text)["input_ids"]))
        final = json.loads(r["messages"][-1]["tool_calls"][0]["function"]["arguments"]) if isinstance(
            r["messages"][-1]["tool_calls"][0]["function"]["arguments"], str) else r["messages"][-1]["tool_calls"][0]["function"]["arguments"]
        for needle in ("submit_assessment", "find_resources", "get_patient", final["items"][0]["item_id"]):
            if needle not in text:
                problems.append(f"{r['case_id']}: '{needle}' missing from rendered text")
    has_generation = "{% generation %}" in (tok.chat_template or "")
    print(json.dumps({"model": a.model, "examples": len(rows), "tokens_min": min(lengths), "tokens_max": max(lengths),
                      "tokens_mean": round(sum(lengths) / len(lengths)), "template_marks_assistant_turns": has_generation,
                      "problems": problems[:5]}, indent=2))
    print("\n--- tail of one rendered example ---\n" + tok.apply_chat_template(rows[0]["messages"], tools=rows[0]["tools"], tokenize=False)[-900:])
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
