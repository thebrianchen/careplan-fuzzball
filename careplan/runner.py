"""Run E1 conditions over cases, write run logs, score, and project latency."""
from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import ROOT
from .cases import Case, content_hash, load_cases
from .data import ResourceTable, TractTable
from .llm import ChatClient, ModelProfile, make_client
from .planner import CONDITIONS, run_case
from .scoring import as_dict, score_assessment, summarize

CASE_TRACTS = ROOT / "data" / "processed" / "case_tracts.csv"
FROZEN = ROOT / "cases" / "FROZEN.json"


def load_profiles(path: Path | None = None) -> dict[str, ModelProfile]:
    raw = yaml.safe_load((path or ROOT / "config" / "models.yaml").read_text())["profiles"]
    profiles = {name: ModelProfile(name=name, **cfg) for name, cfg in raw.items()}
    for p in profiles.values():
        if p.base_url_env and os.environ.get(p.base_url_env):
            p.base_url = os.environ[p.base_url_env]
    return profiles


def apply_case_tracts(cases: list[Case], path: Path = CASE_TRACTS) -> None:
    """Fill patient.tract_geoid from the geocoder output when the case file leaves it null."""
    if not path.exists():
        return
    with path.open(newline="") as f:
        m = {r["case_id"]: r["tract_geoid"] for r in csv.DictReader(f) if r.get("tract_geoid")}
    for c in cases:
        if not c.patient.get("tract_geoid") and c.case_id in m:
            c.patient["tract_geoid"] = m[c.case_id].zfill(11)


def check_frozen(cases: list[Case]) -> list[str]:
    frozen = json.loads(FROZEN.read_text()) if FROZEN.exists() else {}
    problems = []
    for c in cases:
        if c.data["split"] != "test":
            continue
        h = frozen.get(c.case_id)
        if h is None:
            problems.append(f"{c.case_id} is a test case but not frozen")
        elif h != content_hash(c.data):
            problems.append(f"{c.case_id} changed after freezing")
    return problems


def freeze(cases: list[Case], update: bool = False, reason: str = "") -> dict:
    """Record test-case hashes. Existing hashes are kept unless update=True, which needs a reason and is logged in
    cases/FREEZE_LOG.md (a re-freeze is a deliberate act, e.g. after a physician's review changed the keys)."""
    frozen = json.loads(FROZEN.read_text()) if FROZEN.exists() else {}
    changed = []
    for c in cases:
        if c.data["split"] != "test":
            continue
        h = content_hash(c.data)
        if update and frozen.get(c.case_id) not in (None, h):
            changed.append(c.case_id)
            frozen[c.case_id] = h
        else:
            frozen.setdefault(c.case_id, h)
    FROZEN.write_text(json.dumps(frozen, indent=2, sort_keys=True) + "\n")
    if changed:
        with (FROZEN.parent / "FREEZE_LOG.md").open("a") as f:
            f.write(f"- {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}: re-froze {len(changed)} case(s) ({', '.join(changed)}). Reason: {reason}\n")
    return frozen


def _git_rev() -> str | None:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (rev + ("-dirty" if dirty else "")) or None
    except Exception:
        return None


def run(cases_path: Path, conditions: list[str], reps: int, profile_overrides: dict[str, str],
        tag: str = "", tracts_path: Path | None = None, resources_path: Path | None = None,
        allow_unfrozen: bool = False, client_factory=None, out_root: Path | None = None,
        prefetch: bool = False, final: str = "tool", signals: bool = False) -> Path:
    cases = load_cases(cases_path)
    problems = check_frozen(cases)          # hash the file as frozen, before the geocoder fills tract_geoid
    apply_case_tracts(cases)
    if problems and not allow_unfrozen:
        raise SystemExit("refusing to run on unfrozen test cases:\n  " + "\n  ".join(problems)
                         + "\nrun `python -m careplan freeze cases/test` or pass --allow-unfrozen")
    tracts, resources = TractTable.load(tracts_path), ResourceTable.load(resources_path)
    profiles = load_profiles()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (out_root or ROOT / "runs") / f"{stamp}{'_' + tag if tag else ''}"
    (run_dir / "results").mkdir(parents=True)
    used = {}
    for cond in conditions:
        pname = profile_overrides.get(cond, CONDITIONS[cond]["default_profile"])
        used[cond] = asdict(profiles[pname]) if client_factory is None else {"name": "injected"}
    meta = {"started": stamp, "git": _git_rev(), "conditions": {c: CONDITIONS[c]["desc"] for c in conditions},
            "profiles": used, "reps": reps, "prefetch": prefetch, "final": final, "signals": signals,
            "tract_table": str(tracts.source), "tract_table_is_fixture": tracts.is_fixture,
            "resource_table": str(resources.source),
            "cases": {c.case_id: content_hash(c.data) for c in cases}, "unfrozen_warnings": problems}
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    calls_log = (run_dir / "calls.jsonl").open("w")
    for cond in conditions:
        pname = profile_overrides.get(cond, CONDITIONS[cond]["default_profile"])
        client = client_factory(cond) if client_factory else make_client(profiles[pname])
        for case in cases:
            for rep in range(1, reps + 1):
                t0 = time.time()
                try:
                    res = run_case(case, cond, client, tracts, resources, prefetch=prefetch, final=final, signals=signals)
                except Exception as e:  # keep going; record the failure
                    from .planner import RunResult
                    res = RunResult(case.case_id, cond, None, f"exception: {e}", 0)
                rec = {"case_id": case.case_id, "condition": cond, "rep": rep, "profile": pname,
                       "assessment": res.assessment, "error": res.error, "steps": res.steps,
                       "tool_calls": res.tool_calls, "usage": res.usage, "latency_ms": res.latency_ms,
                       "wall_s": round(time.time() - t0, 2), "log": res.log}
                (run_dir / "results" / f"{case.case_id}_{cond}_r{rep}.json").write_text(json.dumps(rec, indent=2, default=str))
                for entry in res.log:
                    calls_log.write(json.dumps({"case_id": case.case_id, "condition": cond, "rep": rep, **entry}, default=str) + "\n")
                calls_log.flush()
                status = "ok" if res.assessment else f"FAILED ({res.error})"
                print(f"{cond} {case.case_id} r{rep}: {status} steps={res.steps} tok_out={res.usage['completion_tokens']}")
    calls_log.close()
    return run_dir


def score_run(run_dir: Path, cases_path: Path, tracts_path: Path | None = None,
              resources_path: Path | None = None) -> dict:
    cases = {c.case_id: c for c in load_cases(cases_path)}
    apply_case_tracts(list(cases.values()))
    tracts, resources = TractTable.load(tracts_path), ResourceTable.load(resources_path)
    scores = []
    for f in sorted((run_dir / "results").glob("*.json")):
        rec = json.loads(f.read_text())
        case = cases.get(rec["case_id"])
        if case is None:
            continue
        scores.append(score_assessment(case, rec["assessment"], rec["condition"], rec["rep"], tracts, resources))
    summary = {"summary": summarize(scores), "per_case": [as_dict(s) for s in scores],
               "tract_table_is_fixture": tracts.is_fixture}
    (run_dir / "scores.json").write_text(json.dumps(summary, indent=2))
    return summary


def project_latency(run_dir: Path, decode_tps: float = 16.0, prefill_tps: float = 1000.0,
                    tool_overhead_s: float = 0.3, safety: float = 1.2) -> dict:
    """Project end-to-end card time on one GB10 from measured token counts (E5 fallback).

    Assumes prefix caching, so prefill ~= the final context size, plus all generated tokens
    at the decode rate. Defaults: ~16 tok/s decode for Super NVFP4 on one Spark (public
    reports, 15-23 tok/s); prefill_tps is a placeholder until measured.
    """
    out: dict[str, list[float]] = {}
    for f in sorted((run_dir / "results").glob("*.json")):
        rec = json.loads(f.read_text())
        steps = [e for e in rec["log"] if "step" in e]
        if not steps:
            continue
        final_ctx = max(int(e["usage"].get("prompt_tokens", 0) or 0) for e in steps)
        gen = sum(int(e["usage"].get("completion_tokens", 0) or 0) for e in steps)
        secs = safety * (final_ctx / prefill_tps + gen / decode_tps + tool_overhead_s * len(rec["tool_calls"]))
        out.setdefault(rec["condition"], []).append(secs)
    res = {}
    for cond, vals in out.items():
        vals.sort()
        res[cond] = {"n": len(vals), "p50_s": round(vals[len(vals) // 2], 1),
                     "p95_s": round(vals[min(len(vals) - 1, int(0.95 * len(vals)))], 1),
                     "assumptions": {"decode_tps": decode_tps, "prefill_tps": prefill_tps, "safety": safety}}
    return res
