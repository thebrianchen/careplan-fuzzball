"""The clinic app's backend (careplan.webapp): visits, one review stream for saved and live runs, decisions, system view."""
import json
import os
import shutil
import tempfile
import threading
import time
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
                                                   "label": "test", "fallback": False, "answers": True, "why": None, "skipped": []}
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
        self.assertEqual((row["review"]["flagged"], row["review"]["items"], row["plan_items"], row["review"]["decided"]), (2, 3, 3, 0))
        self.assertEqual(doc["tucked"], ["P3", "P4"])                      # the same two everywhere: list, visit and handout

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
                                                   "label": "test", "fallback": False, "answers": False, "why": "test did not answer", "skipped": []}
        evs = self.review("mode=live")
        self.assertEqual((len(evs), evs[0]["type"], evs[0]["ok"]), (1, "end", False))
        self.assertIn("not answering", evs[0]["error"])
        self.assertEqual(self.get("case/D001")["items"], [])

    def test_a_planner_that_goes_silent_ends_the_live_review_and_the_saved_run_says_where_it_was_recorded(self):
        seen = {}

        class Silent(FakeStreamingClient):
            timeout, retries = 300, 6

            def chat_stream(self, messages, tools, on_delta):
                seen["patience"] = (self.timeout, self.retries)
                raise RuntimeError("model call failed after 1 attempts: HTTPConnectionPool(host='x', port=8000): Read timed out.")
        client, self.app.client = self.app.client, Silent()
        try:
            evs = self.review("mode=live")
            after = (self.app.client.timeout, self.app.client.retries)
        finally:
            self.app.client = client
        self.assertEqual(seen["patience"], ((10, self.clinic.stall_s), 1))                 # one attempt, a short wait between bytes
        self.assertEqual(after, (300, 6))                                                  # and the client is left as it was
        self.assertEqual((evs[-1]["type"], evs[-1]["ok"]), ("end", False))
        self.assertIn("sent nothing for 30 seconds", evs[-1]["error"])
        recorded = self.review("mode=recorded&speed=1000")
        self.assertEqual((recorded[0]["where"], recorded[-1]["doc"]["meta"]["where"]), ("our own GB10", "our own GB10"))

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
        self.clinic.fallbacks, self.clinic.labels = ["http://spark.internal:8000/v1"], ["the event cluster", "our own GB10"]
        up = {"spark.internal"}
        answer = lambda url, **kw: mock.Mock(ok=any(host in url for host in up))
        try:
            with mock.patch("requests.get", side_effect=answer):
                pl = self.clinic.planner(fresh=True)
                self.assertEqual((pl["answers"], pl["host"], pl["skipped"]), (True, "spark.internal:8000", ["cluster.internal:8000 did not answer"]))
                self.assertEqual((pl["label"], pl["fallback"]), ("our own GB10", True))
                self.assertEqual(local.p.base_url, "http://spark.internal:8000/v1")          # where the next review goes
                up.add("cluster.internal")                                                    # the first choice comes back
                pl = self.clinic.planner(fresh=True)
                self.assertEqual((pl["host"], pl["skipped"], local.p.base_url), ("cluster.internal:8000", [], "http://cluster.internal:8000/v1"))
                self.assertEqual((pl["label"], pl["fallback"]), ("the event cluster", False))
                up.clear()
                pl = self.clinic.planner(fresh=True)
                self.assertEqual((pl["answers"], pl["skipped"]), (False, []))
                self.assertEqual(pl["why"], "cluster.internal:8000 did not answer; spark.internal:8000 did not answer")
        finally:
            self.app.client, self.clinic.fallbacks, self.clinic.labels = client, [], []

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

    def test_a_sample_review_follows_the_answer_key_says_it_is_a_sample_and_is_off_unless_asked_for(self):
        off = self.review("mode=sample")
        self.assertEqual((len(off), off[0]["ok"]), (1, False))
        self.assertFalse(self.get("case/D002")["can_sample"])
        self.clinic.samples = True
        try:
            with urllib.request.urlopen(f"{self.url}/api/app/review/D002?mode=sample") as r:
                evs = [json.loads(line[6:]) for line in r.read().decode().splitlines() if line.startswith("data: ")]
        finally:
            self.clinic.samples = False
            self.post("reset/D002", {})
        doc = evs[-1]["doc"]
        self.assertEqual((evs[0]["source"], doc["meta"]["source"], doc["meta"]["model"]), ("sample", "sample", "sample"))
        key = self.app.cases["D002"].data["planted_conflicts"]
        flagged = {i["item_id"]: i for i in doc["items"] if i["status"] != "likely_ok"}
        self.assertEqual(set(flagged), {c["item_id"] for c in key})
        self.assertEqual(flagged["P4"]["status"], "blocked")
        self.assertEqual({b["barrier"] for b in flagged["P1"]["barriers"]}, {"utilities"})
        self.assertTrue(any(a["resource"] for a in flagged["P1"]["alternatives"]))            # a real place from the resource table
        self.assertEqual([c["need"] for c in doc["crossings"]], ["utilities"])

    def test_with_samples_on_the_day_starts_reviewed_except_the_visits_that_have_a_saved_run(self):
        self.clinic.samples = True
        before = len(self.get("system")["runs"])
        try:
            self.clinic.prefill()
            rows = {c["case_id"]: c for c in self.get("cases")["cases"]}
            added = len(self.get("system")["runs"]) - before
        finally:
            self.clinic.samples = False
            for cid in list(self.clinic.shown):
                self.post(f"reset/{cid}", {})
        self.assertIsNone(rows["D001"]["review"])                                          # it has a saved run: left to be played
        self.assertEqual(rows["D002"]["review"]["source"], "sample")
        self.assertEqual(sum(1 for c in rows.values() if c["review"]), len(rows) - 1)
        self.assertEqual(added, 0)                                                         # a stand-in is not a review on the System page

    def test_the_panel_is_counts_by_neighborhood_and_the_stand_in_is_shown_only_when_samples_are_on(self):
        from careplan import webapp
        real, webapp.PANEL["reviews"] = webapp.PANEL["reviews"], Path(self.tmp.name) / "no_reviews_yet.json"
        try:
            self.assertIsNone(self.get("panel")["panel"])
            self.clinic.samples = True
            p = self.get("panel")["panel"]
        finally:
            self.clinic.samples, webapp.PANEL["reviews"] = False, real
        self.assertEqual((p["source"], p["patients"]), ("sample", 600))
        self.assertTrue(all(t["patients"] >= p["k"] for t in p["tracts"]))                 # nothing under the threshold is published
        clinic = next(t for t in p["tracts"] if t["tract_id"] == p["clinic"]["tract_id"])
        self.assertEqual(clinic["area"], "West Garfield Park")
        self.assertEqual(sum(a["patients"] for a in p["areas"]), sum(t["patients"] for t in p["tracts"]))
        self.assertEqual(sum(a["tracts"] for a in p["areas"]), len(p["tracts"]))
        self.assertEqual(sum(b["flags"] for b in p["barriers"]), sum(sum(t["barriers"].values()) for t in p["tracts"]))
        t = p["tracts"][0]
        self.assertTrue(41 < t["lat"] < 43 and t["facts"] and set(t["places_near"]) == {"food_access", "utilities", "med_cost"})
        self.assertEqual(set(t) & {"name", "mrn", "dob", "address", "patient_id"}, set())  # counts and public figures only
        self.assertEqual(sum(t["kinds_near"]["food_access"].values()), t["places_near"]["food_access"])
        kinds = [x["kind"] for x in t["nearest"]["food_access"]]
        self.assertEqual(set(kinds), set(t["kinds_near"]["food_access"]))                  # every kind that is near is represented
        self.assertNotIn("dialysis", p["programs"]["food_access"][0]["eligible_when"])     # a program for any patient leads
        self.assertTrue(set(t["public_points_to"]) <= set(p["public_kinds"]) == {"food_access", "housing", "transport", "utilities"})
        self.assertIn("how_to_start", p["programs"]["transport"][0])

    def test_panel_counts_written_by_an_older_aggregates_are_not_shown(self):
        from careplan import webapp
        old = Path(self.tmp.name) / "old_counts.json"
        old.write_text(json.dumps({"built": "x", "k": 5, "tracts_suppressed_under_k": 0,
                                   "tracts": [{"tract_geoid": "17031260300", "patients": 9, "appointments": 1, "no_shows": 0, "flagged_items": 3, "barriers": {}}]}))
        real, webapp.PANEL["reviews"] = webapp.PANEL["reviews"], old
        try:
            self.assertIsNone(self.get("panel")["panel"])
        finally:
            webapp.PANEL["reviews"] = real

    def test_the_basemap_has_an_outline_for_every_neighborhood_in_the_panel(self):
        base = self.get("basemap")["basemap"]
        self.clinic.samples = True
        try:
            panel = self.get("panel")["panel"]
        finally:
            self.clinic.samples = False
        self.assertTrue(all(t["tract_id"] in base["tracts"] for t in panel["tracts"]))
        lat, lon = base["tracts"][panel["clinic"]["tract_id"]][0][0]
        self.assertTrue(41 < lat < 43 and -89 < lon < -87)
        self.assertGreater(len(base["streets"]), 1000)
        wide, box = self.get("basemap")["wide"], base["box"]
        self.assertTrue(wide["box"]["south"] < box["south"] and wide["box"]["north"] > box["north"] and wide["box"]["east"] > box["east"])
        self.assertTrue(any(w["name"] == "Lk Michigan" for w in wide["water"]))
        self.assertEqual(len(wide["areas"]), 77)

    def test_a_page_that_cannot_hear_the_stream_gets_the_same_events_by_asking(self):
        streamed = self.review("mode=recorded&speed=1000&rid=abc")
        asked = self.get("progress/D001?rid=abc&after=-1")["events"]
        self.assertEqual([e["type"] for e in asked], [e["type"] for e in streamed])
        self.assertEqual([e["i"] for e in asked], list(range(len(asked))))
        self.assertEqual(len(self.get("progress/D001?rid=abc&after=2")["events"]), len(asked) - 3)
        self.assertEqual(self.get("progress/D001?rid=nobody")["events"], [])

    def test_the_handout_is_written_once_for_the_items_shown_and_the_alternatives_chosen(self):
        asked = []

        class Writer(FakeStreamingClient):
            def chat(self, messages, tools):
                asked.append(messages[0]["content"])
                if len(asked) == 1:                                                        # a sheet with no sections is asked for again
                    return Reply(json.dumps({"items": [], "closing": "x"}), [], {}, 5, {})
                return Reply(json.dumps({"items": [{"item_id": "P1", "heading": "Dialysis", "steps": ["Call your plan for a ride."]},
                                                   {"item_id": "P2", "heading": "Diet", "steps": ["Go to the pantry."]},
                                                   {"item_id": "P3", "heading": "not shown", "steps": ["x"]},
                                                   {"item_id": "P5", "heading": "Log", "steps": ["Keep it up."]}], "closing": "Call us."}), [], {}, 5, {})
        client, self.app.client = self.app.client, Writer()
        try:
            self.review("mode=recorded&speed=1000")
            first, second = self.get("case/D001")["items"][0]["alternatives"][:2]
            self.post("decision/D001", {"item_id": "P1", "decision": "accept", "note": first["text"]})
            self.assertIsNone(self.get("case/D001")["handout"])                            # nothing written for these decisions yet
            code, out = self.post("handout/D001", {})
            self.assertEqual((code, [i["item_id"] for i in out["handout"]["items"]], out["handout"]["where"]), (200, ["P1", "P2", "P5"], "test"))
            self.assertTrue(first["text"] in asked[1] and second["text"] not in asked[1])  # only the alternative the doctor chose
            self.assertTrue("- P1:" in asked[1] and "- P3:" not in asked[1] and "- P4:" not in asked[1])   # only the items shown
            self.assertEqual(self.post("handout/D001", {})[1]["handout"]["saved"], out["handout"]["saved"])
            self.assertEqual(self.get("case/D001")["handout"]["closing"], "Call us.")
            self.assertEqual(len(asked), 2)                                                # kept: not asked again for the same decisions
            self.post("decision/D001", {"item_id": "P1", "decision": "accept", "note": second["text"]})
            self.assertIsNone(self.get("case/D001")["handout"])                            # other decisions, another handout
        finally:
            self.app.client = client

    def test_a_review_runs_to_its_end_after_the_page_leaves_and_the_page_can_pick_it_up(self):
        r = urllib.request.urlopen(f"{self.url}/api/app/review/D001?mode=recorded&speed=30&rid=left1")     # about 3 s at this speed
        r.readline()
        r.close()                                                                          # the page leaves after the first event
        doc = self.get("case/D001")
        self.assertEqual((doc["running"]["rid"], doc["running"]["mode"], doc["items"]), ("left1", "recorded", []))
        self.assertTrue(next(c for c in self.get("cases")["cases"] if c["case_id"] == "D001")["running"])
        self.assertEqual((self.review("mode=recorded&speed=1000&rid=second")[0]["type"], self.get("progress/D001?rid=second")["events"]),
                         ("busy", []))                                                     # a second request does not start another
        for _ in range(100):
            if not self.get("case/D001")["running"]:
                break
            time.sleep(0.1)
        doc, events = self.get("case/D001"), self.get("progress/D001?rid=left1")["events"]
        self.assertEqual((doc["running"], doc["meta"]["source"], len(doc["items"])), (None, "recorded", 5))
        self.assertEqual((events[0]["type"], events[-1]["type"], events[-1]["ok"]), ("start", "end", True))

    def test_a_review_that_failed_while_the_page_was_away_is_still_there_to_be_read(self):
        self.clinic.planner = lambda fresh=False: {"profile": "p", "model": "m", "host": "x", "hosted": False, "label": "x", "fallback": False,
                                                   "answers": False, "why": "x did not answer", "skipped": []}
        self.review("mode=live&rid=failed1")
        cur = self.get("case/D001")["running"]
        self.assertEqual((cur["rid"], cur["done"]), ("failed1", True))
        self.assertFalse(self.get("progress/D001?rid=failed1")["events"][-1]["ok"])
        self.assertEqual(self.review("mode=recorded&speed=1000")[-1]["ok"], True)          # a failed one does not block the next
        self.assertIsNone(self.get("case/D001")["running"])

    def test_pages_are_served_and_nothing_outside_them(self):
        with urllib.request.urlopen(f"{self.url}/app/") as r:
            self.assertIn("Care Plan Check", r.read().decode())
        for path in ("/app/../serve.py", "/app/%2e%2e/%2e%2e/serve.py", "/app/nope.js"):
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(self.url + path)


if __name__ == "__main__":
    unittest.main()
