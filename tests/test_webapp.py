"""The clinic app's backend (careplan.webapp): visits, one review stream for saved and live runs, decisions, system view."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from careplan.llm import ModelProfile, Reply, ToolCall

ASSESSMENT = {"items": [
    {"item_id": "P1", "status": "blocked", "barriers": [{"barrier": "transport", "reason": "No car, two buses.", "evidence": ["turn:T02", "appt:2"]}],
     "alternatives": [{"category": "medicaid_nemt", "resource_record_id": None, "text": "Book plan rides."}]},
    {"item_id": "P2", "status": "likely_ok"}, {"item_id": "P3", "status": "likely_ok"},
    {"item_id": "P4", "status": "likely_ok"}, {"item_id": "P5", "status": "likely_ok"}]}


class FakeStreamingClient:
    p = type("P", (), {"name": "fake-stream", "model": "fake", "base_url": ""})()

    def chat_stream(self, messages, tools, on_delta):
        raw = json.dumps(ASSESSMENT)
        for i in range(0, len(raw), 9):
            on_delta(0, "submit_assessment", raw[i:i + 9])
        return Reply("", [ToolCall("c1", "submit_assessment", ASSESSMENT, raw, None)], {}, 5, {})


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from careplan import serve, webapp
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["CAREPLAN_FEEDBACK"] = os.path.join(cls.tmp.name, "feedback.jsonl")
        cls.app = serve.App()
        del os.environ["CAREPLAN_FEEDBACK"]
        cls.app.client = FakeStreamingClient()
        cls.serve, cls.rec_dir = serve, serve.REC_DIR                     # saved runs: a copy, so a test can add and remove them
        serve.REC_DIR = Path(cls.tmp.name) / "recordings"
        serve.REC_DIR.mkdir()
        shutil.copy(cls.rec_dir / "stream_D001.json", serve.REC_DIR)
        cls.clinic = webapp.clinic(cls.app)
        cls.srv = serve.make_server(cls.app, host="127.0.0.1", port=0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.serve.REC_DIR = cls.rec_dir
        cls.tmp.cleanup()

    def setUp(self):
        self.clinic.planner = lambda fresh=False: {"profile": "fake-stream", "model": "fake", "host": "test", "hosted": False,
                                                   "answers": True, "why": None, "skipped": []}
        self.post("reset/D001", {})

    def get(self, path):
        with urllib.request.urlopen(f"{self.url}/api/app/{path}") as r:
            return json.loads(r.read())

    def post(self, path, body):
        req = urllib.request.Request(f"{self.url}/api/app/{path}", data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())

    def review(self, query):
        with urllib.request.urlopen(f"{self.url}/api/app/review/D001?{query}") as r:
            return [json.loads(line[6:]) for line in r.read().decode().splitlines() if line.startswith("data: ")]

    def test_visit_before_review_has_the_chart_and_no_findings(self):
        doc = self.get("case/D001")
        self.assertEqual((doc["meta"]["source"], doc["items"], doc["decisions"]), ("none", [], {}))
        self.assertTrue(doc["has_recording"])
        self.assertIn("appt:2", [a["ref"] for a in doc["chart"]["appointments"]])
        self.assertIn("screener:transportation", [a["ref"] for a in doc["chart"]["screener"]["answers"]])
        row = next(c for c in self.get("cases")["cases"] if c["case_id"] == "D001")
        self.assertIsNone(row["review"])

    def test_saved_run_replays_as_steps_items_and_a_document(self):
        evs = self.review("mode=recorded&speed=1000")
        self.assertEqual((evs[0]["type"], evs[0]["source"], evs[-1]["type"], evs[-1]["ok"]), ("start", "recorded", "end", True))
        self.assertTrue(any(e["type"] == "step" and e["step"]["kind"] == "crossing" for e in evs))
        items = [e["item"] for e in evs if e["type"] == "item"]
        self.assertEqual([i["item_id"] for i in items], ["P1", "P2", "P3", "P4", "P5"])
        self.assertTrue(any(not ev["said_in_visit"] for b in items[0]["barriers"] for ev in b["evidence"]))     # citations resolved to text
        doc = evs[-1]["doc"]
        self.assertEqual((doc["meta"]["source"], len(doc["items"]), len(doc["crossings"])), ("recorded", 5, 2))
        row = next(c for c in self.get("cases")["cases"] if c["case_id"] == "D001")
        self.assertEqual((row["review"]["flagged"], row["review"]["decided"]), (4, 0))

    def test_a_changed_mind_starts_from_the_finding_as_first_shown(self):
        self.assertEqual(self.post("decision/D001", {"item_id": "P1", "decision": "accept"})[0], 400)        # nothing reviewed yet
        self.review("mode=recorded&speed=1000")
        self.assertEqual(self.post("decision/D001", {"item_id": "P1", "decision": "edit"})[0], 400)          # an edit needs its wording
        self.assertEqual(self.post("decision/D001", {"item_id": "P9", "decision": "accept"})[0], 400)
        code, out = self.post("decision/D001", {"item_id": "P1", "decision": "dismiss"})
        self.assertEqual((code, out["decisions"]["P1"]["decision"]), (200, "dismiss"))
        item = lambda: next(i for i in self.app.results["D001"]["assessment"]["items"] if i["item_id"] == "P1")
        self.assertEqual(item()["status"], "likely_ok")                    # what the handout and the night job see
        self.assertEqual(self.post("decision/D001", {"item_id": "P1", "decision": "accept"})[0], 200)
        self.assertEqual(item()["status"], "at_risk")                      # restored, not left dismissed
        self.assertTrue(item()["barriers"])
        doc = self.get("case/D001")
        self.assertEqual(doc["decisions"]["P1"]["decision"], "accept")
        self.assertEqual(next(i for i in doc["items"] if i["item_id"] == "P1")["status"], "at_risk")         # the page keeps the finding
        rows = [json.loads(l) for l in self.app.feedback_path.read_text().splitlines()]
        self.assertEqual([r["decision"] for r in rows][-2:], ["dismiss", "accept"])

    def test_live_review_streams_items_and_reports_without_patient_fields(self):
        evs = self.review("mode=live")
        self.assertEqual((evs[0]["source"], evs[-1]["ok"]), ("live", True))
        self.assertEqual([e["item"]["item_id"] for e in evs if e["type"] == "item"], ["P1", "P2", "P3", "P4", "P5"])
        self.assertEqual(evs[-1]["doc"]["items"][0]["barriers"][0]["evidence"][0]["text"][:8], "Patient:")
        system = self.get("system")
        self.assertEqual((system["runs"][0]["case_id"], system["runs"][0]["source"], system["runs"][0]["flagged"]), ("D001", "live", 1))
        p = self.app.cases["D001"].patient
        for canary in (p["name"], p["mrn"], p["dob"], "sold the car"):
            self.assertNotIn(canary, json.dumps(system))

    def test_a_live_run_can_be_kept_as_the_saved_run_but_never_replaces_one(self):
        saved = self.clinic.recording("D001")
        before = saved.read_bytes()
        self.review("mode=live&record=1")
        self.assertEqual(saved.read_bytes(), before)
        saved.unlink()
        try:
            self.assertFalse(self.get("case/D001")["has_recording"])
            self.review("mode=live&record=1")
            evs = self.review("mode=recorded&speed=1000")
            self.assertEqual((evs[0]["source"], evs[0]["model"], len(evs[-1]["doc"]["items"])), ("recorded", "fake-stream", 5))
        finally:
            saved.write_bytes(before)

    def test_live_review_fails_at_once_when_the_planner_does_not_answer(self):
        self.clinic.planner = lambda fresh=False: {"profile": "fake-stream", "model": "fake", "host": "test", "hosted": False,
                                                   "answers": False, "why": "test did not answer", "skipped": []}
        evs = self.review("mode=live")
        self.assertEqual((len(evs), evs[0]["type"], evs[0]["ok"]), (1, "end", False))
        self.assertIn("not answering", evs[0]["error"])
        self.assertEqual(self.get("case/D001")["items"], [])

    def test_a_hosted_planner_without_a_key_is_not_answering_and_says_why(self):
        del self.clinic.planner                                            # the real check, not the stub
        hosted = type("C", (), {"p": ModelProfile(name="super", model="nvidia/nemotron-3-super-120b-a12b"), "key": ""})()
        client, self.app.client = self.app.client, hosted
        try:
            with mock.patch("requests.get", side_effect=AssertionError("no network call")) as get:
                pl = self.get("cases")["planner"]
                evs = self.review("mode=live")
            self.assertFalse(get.called)
        finally:
            self.app.client = client
        self.assertEqual((pl["answers"], pl["hosted"]), (False, True))
        self.assertIn("OPENROUTER_API_KEY", pl["why"])
        self.assertEqual((len(evs), evs[0]["type"], evs[0]["ok"]), (1, "end", False))
        self.assertIn(pl["why"], evs[0]["error"])

    def test_the_first_planner_that_answers_is_used_and_the_page_is_told_which(self):
        del self.clinic.planner                                            # the real check, not the stub
        local = type("C", (), {"p": ModelProfile(name="event", model="m", base_url="http://cluster.internal:8000/v1"), "key": ""})()
        client, self.app.client = self.app.client, local
        self.clinic.fallbacks = ["http://spark.internal:8000/v1"]
        up = {"spark.internal"}
        answer = lambda url, **kw: mock.Mock(ok=any(host in url for host in up))
        try:
            with mock.patch("requests.get", side_effect=answer):
                pl = self.clinic.planner(fresh=True)
                self.assertEqual((pl["answers"], pl["host"], pl["skipped"]), (True, "spark.internal:8000", ["cluster.internal:8000 did not answer"]))
                self.assertEqual(local.p.base_url, "http://spark.internal:8000/v1")          # where the next review goes
                up.add("cluster.internal")                                                    # the first choice comes back
                pl = self.clinic.planner(fresh=True)
                self.assertEqual((pl["host"], pl["skipped"], local.p.base_url), ("cluster.internal:8000", [], "http://cluster.internal:8000/v1"))
                up.clear()
                pl = self.clinic.planner(fresh=True)
                self.assertEqual((pl["answers"], pl["skipped"]), (False, []))
                self.assertEqual(pl["why"], "cluster.internal:8000 did not answer; spark.internal:8000 did not answer")
        finally:
            self.app.client, self.clinic.fallbacks = client, []

    def test_a_saved_run_names_its_model_and_the_public_side(self):
        evs = self.review("mode=recorded&speed=1000")
        meta = evs[-1]["doc"]["meta"]
        self.assertEqual((meta["profile"], meta["model"]), ("super-vllm-raw", "Nemotron-3-Super-120B-A12B-NVFP4"))
        self.assertEqual((evs[0]["profile"], evs[0]["model"]), ("super-vllm-raw", "Nemotron-3-Super-120B-A12B-NVFP4"))
        crossings = [s["label"] for s in evs[-1]["doc"]["steps"] if s["kind"] == "crossing"]
        self.assertEqual(crossings, ["Asked the public side for transport help near home", "Asked the public side for food help near home"])

    def test_a_visit_says_where_things_are_and_how_the_neighborhood_compares(self):
        chart = self.get("case/D001")["chart"]
        self.assertEqual(chart["home"]["tract_id"], "17031260300")
        place = {a["dept"]: a["place"] for a in chart["appointments"]}
        self.assertEqual(place["dialysis"]["name"], "Davita Park Manor Dialysis")
        self.assertGreater(place["dialysis"]["miles_from_home"], 10)
        trip = place["dialysis"]["trip"]                                   # None without the schedule cache on this machine
        self.assertTrue(trip is None or trip["minutes"] > 0)
        self.assertIsNone(place["nephrology"])                             # no facility on record: nothing to place
        fact = {f["ref"]: f for f in chart["neighborhood"]["facts"]}
        food = fact["tract:places_foodinsecu"]
        self.assertLess(food["county"]["median"], food["county"]["p75"])
        self.assertGreaterEqual(food["number"], food["county"]["p75"])
        self.assertIsNone(fact["tract:lila_half_10"]["county"])            # a yes/no flag has no scale
        items = self.review("mode=recorded&speed=1000")[-1]["doc"]["items"]
        pantry = next(a["resource"] for i in items for a in i["alternatives"] if a["resource"])
        self.assertTrue(41 < pantry["lat"] < 43 and -89 < pantry["lon"] < -87)

    def test_the_map_backdrop_is_public_points_and_nothing_else(self):
        city = self.get("city")
        self.assertEqual(set(city), {"tracts", "stops"})
        self.assertEqual(len(city["tracts"]["lat"]), len(city["tracts"]["lon"]))
        self.assertGreater(len(city["tracts"]["lat"]), 1000)

    def test_pages_are_served_and_nothing_outside_them(self):
        with urllib.request.urlopen(f"{self.url}/app/") as r:
            self.assertIn("Care Plan Check", r.read().decode())
        for path in ("/app/../serve.py", "/app/%2e%2e/%2e%2e/serve.py", "/app/nope.js"):
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(self.url + path)


if __name__ == "__main__":
    unittest.main()
