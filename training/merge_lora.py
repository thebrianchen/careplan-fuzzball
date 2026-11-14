"""Fallback if vLLM can't serve a LoRA on Nemotron's hybrid layers: merge the adapter into the base
weights and serve the merged model as an ordinary checkpoint.

    python training/merge_lora.py --base nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 --adapter runs/lora-nano --out runs/nano-merged
"""
from __future__ import annotations

import argparse


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    model = AutoModelForCausalLM.from_pretrained(a.base, dtype=torch.bfloat16, trust_remote_code=True)
    model = PeftModel.from_pretrained(model, a.adapter).merge_and_unload()
    model.save_pretrained(a.out, safe_serialization=True)
    AutoTokenizer.from_pretrained(a.base, trust_remote_code=True).save_pretrained(a.out)
    print(f"merged model at {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
