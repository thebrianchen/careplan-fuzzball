"""Synthetic training cases are valid, reproducible, and their gold trajectories score perfectly."""
import json
import random
import unittest

from careplan.cases import Case, build_ref_index, validate_case
from careplan.data import ResourceTable, TractTable
from careplan.scoring import score_assessment
from careplan.sft import trajectory
from careplan.synth import generate


class SynthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tracts, cls.resources = TractTable.load(), ResourceTable.load()

    def test_cases_valid_and_reproducible(self):
        a = [generate(random.Random(7), i, self.tracts) for i in range(1, 2)]
        b = [generate(random.Random(7), i, self.tracts) for i in range(1, 2)]
        self.assertEqual(json.dumps(a), json.dumps(b))
        rng = random.Random(3)
        for i in range(1, 41):
            self.assertEqual(validate_case(generate(rng, i, self.tracts)), [])

    def test_gold_trajectory_is_perfect_and_cites_only_real_records(self):
        rng = random.Random(5)
        checked = 0
        for i in range(1, 31):
            data = generate(rng, i, self.tracts)
            if not data["planted_conflicts"]:
                continue
            case = Case(data=data, path=None, refs=build_ref_index(data))
            ex = trajectory(case, self.tracts, self.resources)
            self.assertIsNotNone(ex)
            gold = json.loads(ex["messages"][-1]["tool_calls"][0]["function"]["arguments"])
            s = score_assessment(case, gold, "gold", 1, self.tracts, self.resources)
            self.assertEqual((s.evidence_ok, s.fp, s.invalid_resource_ids), (s.planted, 0, 0))
            self.assertNotIn(data["patient"]["mrn"], json.dumps(ex))   # the canary never appears in training text
            checked += 1
        self.assertGreater(checked, 10)

    def test_distill_replays_a_run_and_filters_dirty_cards(self):
        from careplan.distill import is_clean, replay
        rng = random.Random(11)
        data = next(d for d in (generate(rng, i, self.tracts) for i in range(1, 40)) if d["planted_conflicts"])
        case = Case(data=data, path=None, refs=build_ref_index(data))
        gold = json.loads(trajectory(case, self.tracts, self.resources)["messages"][-1]["tool_calls"][0]["function"]["arguments"])
        pid, tract = data["patient"]["patient_id"], data["patient"]["tract_geoid"]
        need = {"transport": "transport", "food_access": "food"}.get(data["planted_conflicts"][0]["barrier"], "transport")
        rec = {"case_id": data["case_id"], "assessment": gold, "log": [
            {"prompt_sha256": "x"},
            {"step": 1, "tool_calls": [{"name": "get_patient", "args": {"patient_id": pid}}, {"name": "get_meds", "args": {"patient_id": pid}}]},
            {"step": 2, "tool_calls": [{"name": "get_patient", "args": {"patient_id": pid}}]},            # a repeat: dropped
            {"step": 3, "tool_calls": [{"name": "get_tract_profile", "args": {"tract_id": tract}},
                                       {"name": "find_resources", "args": {"tract_id": tract, "need": need}}]},
            {"step": 4, "tool_calls": [{"name": "submit_assessment", "args": gold}]}]}
        self.assertEqual(is_clean(case, gold, self.tracts, self.resources), (True, "clean"))
        dirty = json.loads(json.dumps(gold))
        dirty["items"][0].setdefault("barriers", []).append({"barrier": "housing", "reason": "x", "evidence": ["turn:T01"]})
        dirty["items"][0]["status"] = "at_risk"
        self.assertFalse(is_clean(case, dirty, self.tracts, self.resources)[0])
        ex = replay(rec, case, self.tracts, self.resources)
        roles = [m["role"] for m in ex["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "tool", "assistant", "tool", "tool", "assistant"])
        self.assertEqual(json.loads(ex["messages"][-1]["tool_calls"][0]["function"]["arguments"]), gold)
        self.assertIn("home_tract", ex["messages"][3]["content"])            # results come from the real tools
        exg = replay(rec, case, self.tracts, self.resources, answers="gold")
        self.assertEqual(exg["source"], "distilled-gold-steps")
        self.assertNotIn(data["patient"]["mrn"], json.dumps(ex))


    def test_clinician_feedback_filter(self):
        import hashlib
        from careplan.distill import clinician_clean
        card = {"items": [{"item_id": "P1", "status": "at_risk", "barriers": []}, {"item_id": "P2", "status": "likely_ok"}]}
        key = hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()
        self.assertEqual(clinician_clean(card, {})[0], False)                                    # never reviewed
        self.assertEqual(clinician_clean(card, {key: {"P1": {"decision": "accept"}}}), (True, "accepted by clinician"))
        self.assertEqual(clinician_clean(card, {key: {"P1": {"decision": "edit", "note": "x"}}})[0], True)
        self.assertEqual(clinician_clean(card, {key: {"P1": {"decision": "dismiss"}}})[0], False)
        self.assertEqual(clinician_clean(card, {key: {"P2": {"decision": "accept"}}})[0], False)  # flagged item unreviewed


if __name__ == "__main__":
    unittest.main()
