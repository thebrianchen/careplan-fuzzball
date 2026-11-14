import copy
import json
import tempfile
import unittest
from pathlib import Path

from careplan import BARRIERS, ROOT
from careplan.cases import Case, build_ref_index, check_semantics, load_case, validate_case_file
from careplan.data import FIXTURE_RESOURCES, FIXTURE_TRACTS, ResourceTable, TractTable
from careplan.llm import FakeClient, Reply, _first_json_object, fake_call
from careplan.planner import run_case, user_prompt
from careplan.scoring import check_evidence, score_assessment, summarize
from careplan.sqltool import build_db, run_sql
from careplan.tools import ToolError, Toolbox, validate_assessment

D001 = ROOT / "cases" / "dev" / "D001-dialysis.yaml"
FX_TRACT = "17031999901"


def hero() -> Case:
    c = load_case(D001)
    c.patient["tract_geoid"] = FX_TRACT
    return c


def tables():
    return TractTable.load(FIXTURE_TRACTS), ResourceTable.load(FIXTURE_RESOURCES)


GOOD = {"items": [
    {"item_id": "P1", "status": "blocked", "barriers": [
        {"barrier": "transport", "reason": "No car since August and no early bus.",
         "evidence": ["turn:T02", "turn:T04", "appt:2", "tract:no_vehicle_share"]}],
     "alternatives": [{"category": "nemt", "resource_record_id": "FX003", "text": "Set up standing Medicaid rides."}]},
    {"item_id": "P2", "status": "at_risk", "barriers": [
        {"barrier": "food_access", "reason": "Corner-store diet, low-access tract.", "evidence": ["turn:T08", "tract:lila_1_10"]}],
     "alternatives": [{"category": "food_pantry", "resource_record_id": "FX001", "text": "Pantry open Tue/Thu."}]},
    {"item_id": "P3", "status": "likely_ok"},
    {"item_id": "P4", "status": "at_risk", "barriers": [
        {"barrier": "transport", "reason": "Another trip without a car.", "evidence": ["turn:T14"]}],
     "alternatives": [{"category": "coordinate_with_existing_visit", "text": "Schedule on a dialysis day."}]},
    {"item_id": "P5", "status": "likely_ok"},
]}


class CaseFileTests(unittest.TestCase):
    def test_hero_case_is_valid(self):
        self.assertEqual(validate_case_file(D001), [])

    def test_semantic_check_catches_missing_topic_tag(self):
        c = load_case(D001)
        data = copy.deepcopy(c.data)
        data["visit"]["transcript"][3]["topics"] = []          # T04 loses its transport tag
        data["planted_conflicts"][0]["evidence"] = [{"source": "transcript", "ref": "turn:T04"}]
        errs = check_semantics(data)
        self.assertTrue(any("lacks that topic tag" in e for e in errs), errs)

    def test_prompt_hides_answer_key(self):
        text = user_prompt(hero())
        self.assertNotIn("topics", text)
        self.assertNotIn("planted", text)
        self.assertIn("[turn:T04]", text)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tb = Toolbox(hero(), *tables())

    def test_patient_and_tract(self):
        p = self.tb.call("get_patient", {"patient_id": "P-D001"})
        self.assertEqual(p["home_tract"], FX_TRACT)
        self.assertNotIn("mrn", p)
        prof = self.tb.call("get_tract_profile", {"tract_id": FX_TRACT})
        refs = {f["ref"] for f in prof["fields"]}
        self.assertIn("tract:no_vehicle_share", refs)

    def test_transcript_tool_does_not_leak_topic_tags(self):
        turns = self.tb.call("get_visit_transcript", {"visit_id": "V-D001", "topic": "transport"})
        self.assertTrue(turns)
        self.assertTrue(all("topics" not in t for t in turns))

    def test_find_resources_filters_by_need_and_time(self):
        res = self.tb.call("find_resources", {"tract_id": FX_TRACT, "need": "food", "days": ["Thu"], "start": "11:00", "end": "12:00"})
        self.assertEqual([r["record_id"] for r in res], ["FX001"])

    def test_bad_args_rejected(self):
        with self.assertRaises(ToolError):
            self.tb.call("find_resources", {"tract_id": FX_TRACT, "need": "food", "free_text": "Raymond Ellis"})
        with self.assertRaises(ToolError):
            self.tb.call("get_meds", {"patient_id": "someone-else"})

    def test_gateway_receives_only_typed_fields(self):
        seen = []
        tb = Toolbox(hero(), *tables(), gateway=lambda req: seen.append(req) or [])
        tb.call("find_resources", {"tract_id": FX_TRACT, "need": "transport"})
        self.assertEqual(set(seen[0]), {"tract_id", "need", "days", "start", "end"})


class SqlTests(unittest.TestCase):
    def test_select_works_and_writes_are_denied(self):
        con = build_db(hero(), *tables())
        out = run_sql(con, "select ref, status from appointments where status='no_show'")
        self.assertEqual(len(out["rows"]), 3)
        self.assertIn("error", run_sql(con, "delete from appointments"))
        self.assertIn("error", run_sql(con, "select 1; drop table meds"))


class ScoringTests(unittest.TestCase):
    def test_good_assessment(self):
        tr, rs = tables()
        s = score_assessment(hero(), GOOD, "C2", 1, tr, rs)
        self.assertEqual((s.tp, s.fp, s.fn), (3, 0, 2))   # C4 (binder/meals) and C5 (P2 caregiving, added in Lynna's review) missed
        self.assertEqual(s.evidence_ok, 3)
        self.assertEqual(s.alt_hit, 3)
        self.assertEqual(s.hallucinated_refs, 0)

    def test_false_flag_decoy_and_hallucinated_ref(self):
        tr, rs = tables()
        bad = json.loads(json.dumps(GOOD))
        bad["items"][2] = {"item_id": "P3", "status": "at_risk", "barriers": [
            {"barrier": "med_cost", "reason": "Cost.", "evidence": ["turn:T12", "med:9"]}]}
        s = score_assessment(hero(), bad, "C2", 1, tr, rs)
        self.assertEqual(s.fp, 1)
        self.assertEqual(s.decoy_hits, 1)
        self.assertEqual(s.hallucinated_refs, 1)

    def test_tract_evidence_uses_thresholds(self):
        c, (tr, rs) = hero(), tables()
        self.assertTrue(check_evidence(c, "transport", ["tract:no_vehicle_share"], tr.get(FX_TRACT)).ok)
        low = tr.get("17031999902")
        self.assertFalse(check_evidence(c, "transport", ["tract:no_vehicle_share"], low).ok)
        self.assertTrue(check_evidence(c, "transport", ["tract:no_vehicle_share"], None).unverifiable)

    def test_supported_recall_ignores_unsupported_guesses(self):
        # D001's C2 conflict (P2 food_access) "hit" with only an unrelated transport turn as evidence.
        tr, rs = tables()
        guess = json.loads(json.dumps(GOOD))
        guess["items"][1]["barriers"][0]["evidence"] = ["turn:T06"]
        s = summarize([score_assessment(hero(), guess, "C0", 1, tr, rs)])["C0"]
        self.assertAlmostEqual(s["recall"], 0.6)             # 3 of D001's 5 conflicts (5 since Lynna's review)
        self.assertAlmostEqual(s["supported_recall"], 0.4)

    def test_validate_assessment(self):
        self.assertEqual(validate_assessment(GOOD, ["P1", "P2", "P3", "P4", "P5"]), [])
        self.assertTrue(validate_assessment({"items": GOOD["items"][:2]}, ["P1", "P2", "P3"]))


class PlannerLoopTests(unittest.TestCase):
    def test_tool_loop_with_fake_model(self):
        script = [
            Reply("", [fake_call("get_patient", {"patient_id": "P-D001"})]),
            Reply("", [fake_call("get_tract_profile", {"tract_id": FX_TRACT})]),
            Reply("", [fake_call("submit_assessment", {"items": [{"item_id": "P1", "status": "likely_ok"}]})]),  # incomplete
            Reply("", [fake_call("submit_assessment", GOOD)]),
        ]
        tr, rs = tables()
        res = run_case(hero(), "C2", FakeClient(script), tr, rs)
        self.assertIsNone(res.error)
        self.assertEqual(res.assessment, GOOD)
        self.assertEqual([t["tool"] for t in res.tool_calls], ["get_patient", "get_tract_profile"])
        s = summarize([score_assessment(hero(), res.assessment, "C2", 1, tr, rs)])
        self.assertAlmostEqual(s["C2"]["recall"], 0.6)

    def test_repeated_identical_call_is_not_rerun(self):
        call = fake_call("get_patient", {"patient_id": "P-D001"})
        script = [Reply("", [call]), Reply("", [call]), Reply("", [fake_call("submit_assessment", GOOD)])]
        tr, rs = tables()
        seen = []
        client = FakeClient(lambda m, t: (seen.append(m[-1]), script[len(seen) - 1])[1])
        res = run_case(hero(), "C2", client, tr, rs)
        self.assertEqual(len(res.tool_calls), 1)
        self.assertIn("already made this exact call", seen[2]["content"])

    def test_prefetch_seeds_record_reads_and_blocks_repeats(self):
        tr, rs = tables()
        seen = []

        def script(messages, tools):
            seen.append(list(messages))
            if len(seen) == 1:   # model ignores the note and re-reads the record: answered from context, no re-run
                return Reply("", [fake_call("get_patient", {"patient_id": "P-D001"})])
            return Reply("", [fake_call("submit_assessment", GOOD)])
        case = hero()
        case.patient["tract_geoid"] = FX_TRACT
        events = []
        res = run_case(case, "C2", FakeClient(script), tr, rs, prefetch=True, on_event=events.append)
        self.assertEqual(res.assessment, GOOD)
        first = seen[0]
        self.assertIn("already retrieved", first[1]["content"])
        self.assertEqual([c["function"]["name"] for c in first[2]["tool_calls"]],
                         ["get_patient", "get_problems", "get_meds", "get_appointments", "get_screener", "get_tract_profile"])
        self.assertEqual([m["role"] for m in first[3:9]], ["tool"] * 6)
        self.assertIn("home_tract", first[3]["content"])
        self.assertEqual(sum(1 for t in res.tool_calls if t.get("prefetched")), 6)
        self.assertEqual(len(res.tool_calls), 6)                       # the repeat was not re-run
        self.assertIn("already made this exact call", seen[1][-1]["content"])
        self.assertEqual(sum(1 for e in events if e["type"] == "tool" and e.get("prefetched")), 6)
        # Prefetch is a tools-mode lever only; C0 ignores it.
        res0 = run_case(hero(), "C0", FakeClient([Reply("", [fake_call("submit_assessment", GOOD)])]), tr, rs, prefetch=True)
        self.assertEqual(res0.tool_calls, [])

    def test_text_final_streams_items_and_accepts_json_reply(self):
        tr, rs = tables()
        seen_tools = []

        class StreamingFake(FakeClient):
            def chat_stream(self, messages, tools, on_delta):
                seen_tools.append([t["function"]["name"] for t in tools])
                if len(seen_tools) == 1:
                    return Reply("", [fake_call("get_patient", {"patient_id": "P-D001"})])
                text = "Here is the card:\n" + json.dumps(GOOD)
                for i in range(0, len(text), 7):            # content arrives in small fragments, like vLLM
                    on_delta(-1, "", text[i:i + 7])
                return Reply(text, [])
        events = []
        res = run_case(hero(), "C2", StreamingFake([]), tr, rs, final="text", on_event=events.append)
        self.assertEqual(res.assessment, GOOD)
        self.assertNotIn("submit_assessment", seen_tools[0])        # no submit tool in text mode
        self.assertIn("find_resources", seen_tools[0])
        items = [e["item"]["item_id"] for e in events if e["type"] == "item"]
        self.assertEqual(items, [i["item_id"] for i in GOOD["items"]])   # each item surfaced as it closed
        self.assertEqual(sum(1 for e in events if e["type"] == "done"), 1)

    def test_text_final_rejects_then_retries_and_tool_mode_prompt_unchanged(self):
        tr, rs = tables()
        bad = {"items": GOOD["items"][:1]}
        script = [Reply(json.dumps(bad), []), Reply(json.dumps(GOOD), [])]
        seen = []
        client = FakeClient(lambda m, t: (seen.append(list(m)), script[len(seen) - 1])[1])
        res = run_case(hero(), "C2", client, tr, rs, final="text")
        self.assertEqual(res.assessment, GOOD)
        self.assertIn("missing plan items", seen[1][-1]["content"])
        from careplan.planner import system_prompt
        self.assertIn("Call submit_assessment exactly once", system_prompt("tools"))
        self.assertIn("reply with the final assessment as one JSON object", system_prompt("tools", "text"))
        self.assertNotIn("submit_assessment", system_prompt("tools", "text"))
        self.assertNotIn("submit_assessment", user_prompt(hero(), "text"))

    def test_step_budget_warning(self):
        tr, rs = tables()
        seen = []

        def script(messages, tools):
            seen.append(messages[-1]["content"])
            if "steps left" in messages[-1]["content"]:
                return Reply("", [fake_call("submit_assessment", GOOD)])
            return Reply("", [fake_call("get_visit_transcript", {"visit_id": "V-D001", "topic": BARRIERS[len(seen) % 10]})])
        res = run_case(hero(), "C2", FakeClient(script), tr, rs, max_steps=8)
        self.assertEqual(res.assessment, GOOD)
        self.assertEqual(res.steps, 6)

    def test_model_that_never_submits_fails_cleanly(self):
        tr, rs = tables()
        res = run_case(hero(), "C0", FakeClient([Reply("I think it's fine.", [])]), tr, rs)
        self.assertIsNone(res.assessment)
        self.assertIn("submit_assessment", res.error)

    def test_json_mode_parser(self):
        self.assertEqual(json.loads(_first_json_object('<think>x</think> ok {"tool": "get_patient", "arguments": {"patient_id": "P"}}'))["tool"], "get_patient")


class RunnerTests(unittest.TestCase):
    def test_profile_base_url_follows_its_env_var(self):
        import os
        from unittest import mock
        from careplan.runner import load_profiles
        with mock.patch.dict(os.environ, {"PLANNER_URL": "http://planner:8000/v1"}):
            self.assertEqual(load_profiles()["super35-vllm"].base_url, "http://planner:8000/v1")
            self.assertEqual(load_profiles()["super-vllm"].base_url, "http://localhost:8000/v1")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLANNER_URL", None)
            self.assertEqual(load_profiles()["super35-vllm"].base_url, "http://localhost:8000/v1")

    def test_run_and_score_end_to_end(self):
        from careplan import runner
        with tempfile.TemporaryDirectory() as td:
            cases_dir = Path(td) / "cases"
            cases_dir.mkdir()
            data = load_case(D001).data
            data["patient"]["tract_geoid"] = FX_TRACT
            import yaml
            (cases_dir / "D001.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
            factory = lambda cond: FakeClient([Reply("", [fake_call("submit_assessment", GOOD)])])
            run_dir = runner.run(cases_dir, ["C0", "C2"], 2, {}, "test", FIXTURE_TRACTS, FIXTURE_RESOURCES,
                                 client_factory=factory, out_root=Path(td) / "runs")
            summary = runner.score_run(run_dir, cases_dir, FIXTURE_TRACTS, FIXTURE_RESOURCES)
            self.assertEqual(summary["summary"]["C2"]["n"], 2)
            self.assertAlmostEqual(summary["summary"]["C0"]["recall"], 0.6)
            lat = runner.project_latency(run_dir)
            self.assertIn("C2", lat)


if __name__ == "__main__":
    unittest.main()
