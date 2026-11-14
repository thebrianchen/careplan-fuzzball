"""Load, validate, index and freeze case files."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from . import ROOT

SCHEMA_PATH = ROOT / "schemas" / "case.schema.json"
B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
# Fields that are scoring metadata. They are never shown to a model.
HIDDEN_KEYS = {"topics", "planted_conflicts", "decoys", "expected_ok_items", "reviewed_by",
               "author", "frozen_sha256", "needs_tract_check", "note", "title"}


def new_mrn() -> str:
    return "MRN-" + "".join(secrets.choice(B32) for _ in range(10))


def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


@dataclass
class Case:
    data: dict[str, Any]
    path: Path | None = None
    refs: dict[str, dict] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.data["case_id"]

    @property
    def patient(self) -> dict:
        return self.data["patient"]

    @property
    def visit(self) -> dict:
        return self.data["visit"]

    @property
    def plan(self) -> list[dict]:
        return self.data["draft_plan"]

    @property
    def tract_geoid(self) -> str | None:
        return self.patient.get("tract_geoid")

    def ground_truth(self) -> dict:
        return {
            "conflicts": self.data["planted_conflicts"],
            "decoys": self.data["decoys"],
            "ok_items": self.data.get("expected_ok_items", []),
        }

    def canaries(self) -> list[str]:
        """Values that must never leave the private zone (used by the leak test)."""
        p = self.patient
        return [p["mrn"], p["name"], p["dob"], p["patient_id"]]


def build_ref_index(data: dict) -> dict[str, dict]:
    """Map every ref string in the case to the object that carries it."""
    idx: dict[str, dict] = {}
    p = data["patient"]
    for key in ("problems", "meds", "appointments", "notes"):
        for obj in p.get(key, []):
            idx[obj["ref"]] = obj
    for turn in data["visit"]["transcript"]:
        idx[turn["ref"]] = turn
    for q, a in (p.get("screener", {}) or {}).get("answers", {}).items():
        idx[f"screener:{q}"] = {"question": q, "answer": a}
    return idx


def check_semantics(data: dict) -> list[str]:
    """Checks the JSON schema can't express."""
    errs: list[str] = []
    refs = build_ref_index(data)
    items = {i["item_id"] for i in data["draft_plan"]}
    seen = set()
    for r in refs:
        if r in seen:
            errs.append(f"duplicate ref {r}")
        seen.add(r)
    for c in data["planted_conflicts"]:
        if c["item_id"] not in items:
            errs.append(f"{c['conflict_id']}: unknown plan item {c['item_id']}")
        strong = False
        for ev in c["evidence"]:
            ref = ev["ref"]
            if ref.startswith(("tract:", "resource:", "transit:")):
                strong = True
                continue
            if ref not in refs:
                errs.append(f"{c['conflict_id']}: evidence ref {ref} not found in case")
                continue
            obj = refs[ref]
            if ref.startswith(("turn:", "note:")):
                if c["barrier"] not in obj.get("topics", []):
                    errs.append(f"{c['conflict_id']}: {ref} is cited for {c['barrier']} but lacks that topic tag")
                else:
                    strong = True
            elif ref.startswith("screener:") or ref.startswith("med:"):
                strong = True
        if not strong:
            errs.append(f"{c['conflict_id']}: needs at least one non-weak piece of evidence")
    for d in data["decoys"]:
        if d["ref"] not in refs and not d["ref"].startswith("tract:"):
            errs.append(f"decoy ref {d['ref']} not found in case")
    for ok in data.get("expected_ok_items", []):
        if ok not in items:
            errs.append(f"expected_ok_items: unknown plan item {ok}")
        if any(c["item_id"] == ok for c in data["planted_conflicts"]):
            errs.append(f"{ok} is both expected_ok and has a planted conflict")
    return errs


def load_case(path: Path) -> Case:
    data = yaml.safe_load(path.read_text())
    return Case(data=data, path=path, refs=build_ref_index(data))


def validate_case_file(path: Path) -> list[str]:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        return [f"YAML error: {e}"]
    return validate_case(data)


def validate_case(data: dict) -> list[str]:
    errs = [f"schema: {'/'.join(map(str, e.path))}: {e.message}"
            for e in sorted(_validator().iter_errors(data), key=lambda e: list(e.path))]
    if errs:
        return errs
    errs = check_semantics(data)
    if data.get("frozen_sha256") and data["frozen_sha256"] != content_hash(data):
        errs.append("frozen case was edited after freezing (hash mismatch)")
    return errs


def iter_case_paths(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())


def load_cases(root: Path, split: str | None = None) -> list[Case]:
    cases = [load_case(p) for p in iter_case_paths(root)]
    return [c for c in cases if split is None or c.data["split"] == split]


def content_hash(data: dict) -> str:
    clean = {k: v for k, v in data.items() if k != "frozen_sha256"}
    blob = json.dumps(clean, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def strip_hidden(obj: Any) -> Any:
    """Remove scoring metadata before anything is shown to a model."""
    if isinstance(obj, dict):
        return {k: strip_hidden(v) for k, v in obj.items() if k not in HIDDEN_KEYS}
    if isinstance(obj, list):
        return [strip_hidden(v) for v in obj]
    return obj
