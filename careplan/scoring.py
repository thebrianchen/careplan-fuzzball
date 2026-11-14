"""Score planner assessments against a case's planted conflicts (E1 metrics)."""
from __future__ import annotations

import random
import re
from dataclasses import asdict, dataclass, field

import yaml

from . import ROOT
from .cases import Case
from .data import ResourceTable, TractTable

RULES = yaml.safe_load((ROOT / "rules" / "barrier_rules.yaml").read_text())
_CMP = re.compile(r"^\s*(>=|<=|==|>|<)\s*(-?[0-9.]+)\s*$")


def _cmp(value, rule: str) -> bool:
    m = _CMP.match(rule)
    if not m or value is None:
        return False
    op, num = m.group(1), float(m.group(2))
    v = float(value)
    return {">=": v >= num, "<=": v <= num, "==": v == num, ">": v > num, "<": v < num}[op]


@dataclass
class EvidenceCheck:
    ok: bool
    unverifiable: bool
    hallucinated: list[str] = field(default_factory=list)
    strong: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)


def check_evidence(case: Case, barrier: str, refs: list[str], tract_row: dict | None,
                   resources: ResourceTable | None = None) -> EvidenceCheck:
    chk = EvidenceCheck(ok=False, unverifiable=False)
    for ref in refs:
        kind, _, key = ref.partition(":")
        if kind == "tract":
            if tract_row is None:
                chk.unverifiable = True
                continue
            if key not in tract_row:
                chk.hallucinated.append(ref)
                continue
            rule = RULES["tract_fields"].get(key)
            if rule and barrier in rule["supports"] and _cmp(tract_row[key], rule["rule"]):
                chk.strong.append(ref)
            continue
        if kind == "transit":
            from .transit_tool import supports_transport
            ok = supports_transport(case, key, tract_row) if barrier == "transport" else False
            if ok is None:
                chk.unverifiable = True
            elif ok:
                chk.strong.append(ref)
            continue
        if kind == "resource":
            if resources is not None and key not in resources.by_id:
                chk.hallucinated.append(ref)
            continue
        obj = case.refs.get(ref)
        if obj is None:
            chk.hallucinated.append(ref)
            continue
        if kind in ("turn", "note"):
            if barrier in obj.get("topics", []):
                chk.strong.append(ref)
        elif kind == "screener":
            rule = RULES["screener"].get(obj["question"])
            if rule and barrier in rule["supports"]:
                ans = str(obj["answer"]).lower()
                if ("when_any" in rule and ans in [str(a).lower() for a in rule["when_any"]]) or \
                   ("when_none_of" in rule and ans not in [str(a).lower() for a in rule["when_none_of"]]):
                    chk.strong.append(ref)
        elif kind == "appt":
            if obj.get("status") == "no_show" and barrier in RULES["record"]["appointment_no_show"]["supports"]:
                chk.weak.append(ref)
        elif kind == "med":
            copay, tier = obj.get("monthly_copay_usd") or 0, obj.get("cost_tier") or 0
            if barrier == "med_cost" and (copay >= 30 or tier >= 4):
                chk.strong.append(ref)
            elif barrier in ("med_cost", "transport") and (obj.get("refill_gap_days") or 0) >= 7:
                chk.weak.append(ref)
            elif barrier == "utilities" and obj.get("needs_refrigeration"):
                chk.weak.append(ref)
    chk.ok = not chk.hallucinated and bool(chk.strong)
    return chk


@dataclass
class CaseScore:
    case_id: str
    condition: str
    rep: int
    failed: bool
    planted: int = 0
    flags: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    decoy_hits: int = 0
    decoys: int = 0
    ok_items_flagged: int = 0
    evidence_ok: int = 0
    evidence_unverifiable: int = 0
    hallucinated_refs: int = 0
    alt_hit: int = 0
    status_exact: int = 0
    invalid_resource_ids: int = 0
    missed: list[str] = field(default_factory=list)
    false_flags: list[str] = field(default_factory=list)


def score_assessment(case: Case, assessment: dict | None, condition: str, rep: int,
                     tracts: TractTable, resources: ResourceTable) -> CaseScore:
    gt = case.ground_truth()
    planted = {(c["item_id"], c["barrier"]): c for c in gt["conflicts"]}
    decoy_barriers = {d["barrier"] for d in gt["decoys"]}
    s = CaseScore(case.case_id, condition, rep, failed=assessment is None,
                  planted=len(planted), decoys=len(gt["decoys"]))
    if assessment is None:
        s.fn = len(planted)
        s.missed = [f"{i}:{b}" for i, b in planted]
        return s
    tract_row = tracts.get(case.tract_geoid)
    flags: dict[tuple, dict] = {}
    for item in assessment["items"]:
        if item["status"] == "likely_ok":
            continue
        if item["item_id"] in gt["ok_items"]:
            s.ok_items_flagged += 1
        for b in item.get("barriers", []):
            flags.setdefault((item["item_id"], b["barrier"]), {"status": item["status"], "evidence": [],
                                                              "alternatives": item.get("alternatives", [])})
            flags[(item["item_id"], b["barrier"])]["evidence"] += b["evidence"]
        for alt in item.get("alternatives", []) or []:
            rid = alt.get("resource_record_id")
            if rid and rid not in resources.by_id:
                s.invalid_resource_ids += 1
    s.flags = len(flags)
    for key, f in flags.items():
        chk = check_evidence(case, key[1], f["evidence"], tract_row, resources)
        s.hallucinated_refs += len(chk.hallucinated)
        if key in planted:
            s.tp += 1
            c = planted[key]
            s.evidence_ok += int(chk.ok)
            s.evidence_unverifiable += int(chk.unverifiable and not chk.ok)
            s.status_exact += int(f["status"] == c["expected_status"])
            acceptable = {a.lower() for a in c.get("acceptable_alternative_categories", [])}
            s.alt_hit += int(any(str(a.get("category", "")).lower() in acceptable for a in f["alternatives"]))
        else:
            s.fp += 1
            s.false_flags.append(f"{key[0]}:{key[1]}")
            if key[1] in decoy_barriers:
                s.decoy_hits += 1
    s.fn = len(planted) - s.tp
    s.missed = [f"{i}:{b}" for (i, b) in planted if (i, b) not in flags]
    return s


def _agg(scores: list[CaseScore]) -> dict:
    n = len(scores)
    planted = sum(s.planted for s in scores)
    flags = sum(s.flags for s in scores)
    tp = sum(s.tp for s in scores)
    decoys = sum(s.decoys for s in scores)
    return {
        "n": n,
        "recall": tp / planted if planted else None,
        # A hit counts only if its cited evidence supports the barrier under the rules. Plain recall
        # rewards plausible guesses (e.g. inferring food access from transport turns).
        "supported_recall": sum(s.evidence_ok for s in scores) / planted if planted else None,
        "precision": tp / flags if flags else None,
        "false_flags_per_case": sum(s.fp for s in scores) / n if n else None,
        "decoy_hit_rate": sum(s.decoy_hits for s in scores) / decoys if decoys else None,
        "evidence_correct": sum(s.evidence_ok for s in scores) / tp if tp else None,
        "evidence_unverifiable": sum(s.evidence_unverifiable for s in scores),
        "alt_hit_rate": sum(s.alt_hit for s in scores) / tp if tp else None,
        "status_exact": sum(s.status_exact for s in scores) / tp if tp else None,
        "hallucinated_refs_per_case": sum(s.hallucinated_refs for s in scores) / n if n else None,
        "invalid_resource_ids": sum(s.invalid_resource_ids for s in scores),
        "failed_runs": sum(s.failed for s in scores),
    }


def bootstrap(scores: list[CaseScore], metric: str, iters: int = 2000, seed: int = 7) -> tuple[float, float] | None:
    by_case: dict[str, list[CaseScore]] = {}
    for s in scores:
        by_case.setdefault(s.case_id, []).append(s)
    ids = list(by_case)
    if len(ids) < 2:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(iters):
        sample = [s for cid in (rng.choice(ids) for _ in ids) for s in by_case[cid]]
        v = _agg(sample)[metric]
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def summarize(scores: list[CaseScore]) -> dict:
    out = {}
    for cond in sorted({s.condition for s in scores}):
        sub = [s for s in scores if s.condition == cond]
        agg = _agg(sub)
        agg["recall_ci95"] = bootstrap(sub, "recall")
        agg["supported_recall_ci95"] = bootstrap(sub, "supported_recall")
        agg["false_flags_ci95"] = bootstrap(sub, "false_flags_per_case")
        out[cond] = agg
    return out


def as_dict(s: CaseScore) -> dict:
    return asdict(s)
