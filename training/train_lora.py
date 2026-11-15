"""LoRA fine-tune of Nemotron 3 Nano 30B-A3B on the synthetic planner trajectories, on one GB10.

Loss is computed only on the planner's own turns (tool calls and the final submit_assessment): the
Nemotron chat template doesn't mark assistant turns, so masks are built by rendering each
conversation up to every assistant turn (prefix) and including it (prefix + turn).

Pre-flight on a laptop (CPU, tiny model) catches data and template bugs before paying for GPU hours:
    .venv-train/bin/python training/check_template.py --model nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
    .venv-train/bin/python training/train_lora.py --model Qwen/Qwen2.5-0.5B-Instruct --limit 2 --max-steps 1 \
        --targets q_proj,v_proj --out /tmp/lora-smoke --cpu
On a GB10 (fuzzball/event/train.fz.yaml runs this on the cluster):
    python training/train_lora.py --list-modules --model nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
    python training/train_lora.py --model nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 --targets <from above> --out runs/lora-nano
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re


def prepare(ex: dict) -> dict:
    """HF chat templates expect tool-call arguments as objects, not JSON strings."""
    ex = copy.deepcopy(ex)
    for m in ex["messages"]:
        for c in m.get("tool_calls") or []:
            if isinstance(c["function"]["arguments"], str):
                c["function"]["arguments"] = json.loads(c["function"]["arguments"])
    return {"case_id": ex.get("case_id", ""), "messages": ex["messages"], "tools": ex["tools"]}


def load_streaming(path: str, dtype, device: str, remote_code: bool = False):
    """Build the model on the meta device, then load one safetensors shard at a time straight to `device`,
    evicting each file from the page cache afterwards. On a GB10 (unified memory) the default loader can hold
    the mmapped checkpoint in page cache on top of the loaded weights and get OOM-killed in a container."""
    import gc
    import glob
    import torch
    from accelerate import init_empty_weights
    from accelerate.utils import set_module_tensor_to_device
    from safetensors.torch import load as st_load
    from transformers import AutoConfig, AutoModelForCausalLM

    cfg = AutoConfig.from_pretrained(path, trust_remote_code=remote_code)
    with init_empty_weights():
        model = AutoModelForCausalLM.from_config(cfg, trust_remote_code=remote_code, dtype=dtype)
    params = dict(model.named_parameters())             # buffers (e.g. rotary inv_freq) are computed at init, not stored
    prefix = getattr(model, "base_model_prefix", "model")
    expert_re = re.compile(r"^(.*\.experts)\.(\d+)\.(\w+)\.weight$")
    pending: dict[str, dict[int, "torch.Tensor"]] = {}  # fused expert param -> {expert index: tensor}
    loaded = set()

    def resolve(name: str) -> str | None:
        if name in params:
            return name
        head, _, rest = name.partition(".")                # e.g. Nemotron checkpoints say "backbone.", transformers "model."
        alt = f"{prefix}.{rest}"
        return alt if alt in params else None

    for shard in sorted(glob.glob(os.path.join(path, "*.safetensors"))):
        with open(shard, "rb") as f:
            blob = f.read()
        tensors = st_load(blob)
        del blob
        for name, t in tensors.items():
            t = t.to(dtype) if t.is_floating_point() else t
            m = expert_re.match(name)
            if m:                                          # per-expert weights -> one stacked [n_experts, ...] param
                fused = resolve(f"{m.group(1)}.{m.group(3)}")
                if fused is None:
                    continue
                pending.setdefault(fused, {})[int(m.group(2))] = t.to(device)
                if len(pending[fused]) == params[fused].shape[0]:
                    parts = pending.pop(fused)
                    set_module_tensor_to_device(model, fused, device, value=torch.stack([parts[i] for i in range(len(parts))]))
                    loaded.add(fused)
                continue
            key = resolve(name)
            if key is None:
                continue
            set_module_tensor_to_device(model, key, device, value=t)
            loaded.add(key)
        del tensors
        if hasattr(os, "posix_fadvise"):                 # Linux only (the box); a no-op concern on macOS
            fd = os.open(shard, os.O_RDONLY)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.close(fd)
        gc.collect()
    for name, buf in list(model.named_buffers()):         # computed buffers: just move them
        set_module_tensor_to_device(model, name, device, value=buf)
    if hasattr(model, "tie_weights"):
        model.tie_weights()                              # e.g. lm_head tied to the embeddings
    still_meta = [k for k, v in model.named_parameters() if v.device.type == "meta"]
    if still_meta:
        raise RuntimeError(f"parameters not found in checkpoint: {still_meta[:5]}")
    print(f"streamed {len(loaded)} tensors to {device}")
    return model


def encode(tok, ex: dict, max_len: int) -> dict | None:
    """input_ids + labels with everything except assistant turns masked to -100."""
    msgs, tools = ex["messages"], ex["tools"]
    full = tok.apply_chat_template(msgs, tools=tools, tokenize=False)
    ids = tok(full, add_special_tokens=False)["input_ids"]
    labels = [-100] * len(ids)
    for i, m in enumerate(msgs):
        if m["role"] != "assistant":
            continue
        before = tok.apply_chat_template(msgs[:i], tools=tools, tokenize=False, add_generation_prompt=True)
        upto = tok.apply_chat_template(msgs[: i + 1], tools=tools, tokenize=False)
        if not full.startswith(upto):
            return None                                   # template isn't prefix-stable; skip rather than mis-mask
        # The generation prompt may differ from the rendered turn (Nemotron opens "<think>" vs a rendered
        # "<think></think>"), so the span starts where they diverge.
        start = os.path.commonprefix([before, upto])
        a = len(tok(start, add_special_tokens=False)["input_ids"])
        b = len(tok(upto, add_special_tokens=False)["input_ids"])
        labels[a:b] = ids[a:b]
    if len(ids) > max_len:
        return None            # drop, don't truncate: a cut tail is the card (submit_assessment), and a cut card teaches broken JSON
    return {"input_ids": ids, "labels": labels} if any(x != -100 for x in labels) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--train", default="data/synth/sft/train.jsonl")
    ap.add_argument("--val", default="data/synth/sft/val.jsonl")
    ap.add_argument("--out", default="runs/lora-nano")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--max-steps", type=int, default=-1)
    ap.add_argument("--limit", type=int, default=0, help="use only the first N train/val examples (smoke test)")
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--max-len", type=int, default=12288)
    ap.add_argument("--targets", default="q_proj,k_proj,v_proj,o_proj", help="check with --list-modules first")
    ap.add_argument("--cpu", action="store_true")
    ap.add_argument("--list-modules", action="store_true")
    ap.add_argument("--stream-load", action="store_true", help="load shard by shard (GB10 containers; see load_streaming)")
    ap.add_argument("--remote-code", action="store_true", help="use the repo's modeling code (needs mamba-ssm); default: native transformers class")
    a = ap.parse_args()

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments

    if a.list_modules:
        from accelerate import init_empty_weights
        from transformers import AutoConfig
        with init_empty_weights():
            m = AutoModelForCausalLM.from_config(AutoConfig.from_pretrained(a.model, trust_remote_code=a.remote_code), trust_remote_code=a.remote_code)
        print("Linear module names:", sorted({n.rsplit(".", 1)[-1] for n, mod in m.named_modules() if isinstance(mod, torch.nn.Linear)}))
        return 0

    from peft import LoraConfig, get_peft_model
    tok = AutoTokenizer.from_pretrained(a.model, trust_remote_code=True)
    pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id

    def load(path):
        rows = [prepare(json.loads(l)) for l in open(path)][: a.limit or None]
        enc = [encode(tok, r, a.max_len) for r in rows]
        print(f"{path}: dropped {sum(e is None for e in enc)} example(s) over max_len={a.max_len} or unmaskable")
        kept = [e for e in enc if e]
        print(f"{path}: {len(kept)}/{len(rows)} examples encoded; "
              f"assistant tokens {sum(sum(x != -100 for x in e['labels']) for e in kept):,} of {sum(len(e['input_ids']) for e in kept):,}")
        return kept

    def collate(batch):
        n = max(len(b["input_ids"]) for b in batch)
        ids = torch.tensor([b["input_ids"] + [pad] * (n - len(b["input_ids"])) for b in batch])
        lab = torch.tensor([b["labels"] + [-100] * (n - len(b["labels"])) for b in batch])
        return {"input_ids": ids, "labels": lab, "attention_mask": (torch.arange(n)[None, :] < torch.tensor([len(b["input_ids"]) for b in batch])[:, None]).long()}

    train, val = load(a.train), None
    dtype = torch.float32 if a.cpu else torch.bfloat16
    if a.stream_load:
        model = load_streaming(a.model, dtype, "cpu" if a.cpu else "cuda", a.remote_code)
    else:
        model = AutoModelForCausalLM.from_pretrained(a.model, dtype=dtype, trust_remote_code=a.remote_code,
                                                     device_map=None if a.cpu else "cuda")
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(r=a.rank, lora_alpha=2 * a.rank, lora_dropout=0.05,
                                             target_modules=a.targets.split(","), task_type="CAUSAL_LM"))
    model.print_trainable_parameters()
    args = TrainingArguments(output_dir=a.out, num_train_epochs=a.epochs, max_steps=a.max_steps, learning_rate=a.lr,
                             per_device_train_batch_size=1, gradient_accumulation_steps=1 if a.limit else 8,
                             bf16=not a.cpu, logging_steps=1 if a.limit else 10, report_to=[], use_cpu=a.cpu,
                             eval_strategy="no", per_device_eval_batch_size=1,   # in-training eval OOM'd on GB10 (default eval batch 8); scored separately
                             save_strategy="no" if a.limit else "steps",
                             save_steps=20, save_total_limit=2,                    # resume after interruptions
                             remove_unused_columns=False, lr_scheduler_type="cosine", warmup_steps=0 if a.limit else 10)
    import glob as _glob
    resume = bool(_glob.glob(os.path.join(a.out, "checkpoint-*"))) and not a.limit
    if resume:
        print(f"resuming from the latest checkpoint in {a.out}")
    Trainer(model=model, args=args, train_dataset=train, eval_dataset=val, data_collator=collate).train(resume_from_checkpoint=resume or None)
    model.save_pretrained(a.out)
    tok.save_pretrained(a.out)
    print(f"saved adapter to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
