"""Card streaming: items come out of the submit_assessment arguments as they're written."""
import json
import unittest

from careplan import ROOT
from careplan.cases import load_case
from careplan.data import FIXTURE_RESOURCES, FIXTURE_TRACTS, ResourceTable, TractTable
from careplan.llm import Reply, ToolCall
from careplan.planner import run_case
from careplan.stream import ItemStreamer

ASSESSMENT = {"items": [
    {"item_id": "P1", "status": "blocked", "barriers": [{"barrier": "transport", "reason": "No car {really}, \"two\" buses.",
                                                          "evidence": ["turn:T02"]}], "alternatives": []},
    {"item_id": "P2", "status": "likely_ok"},
    {"item_id": "P3", "status": "likely_ok"}, {"item_id": "P4", "status": "likely_ok"}, {"item_id": "P5", "status": "likely_ok"}]}


class ItemStreamerTests(unittest.TestCase):
    def test_items_emerge_one_by_one_from_tiny_fragments(self):
        text = json.dumps(ASSESSMENT)
        s, got, when = ItemStreamer(), [], []
        for i in range(0, len(text), 3):                     # 3-character chunks, braces and quotes inside strings
            for item in s.feed(text[i:i + 3]):
                got.append(item)
                when.append(i)
        self.assertEqual(got, ASSESSMENT["items"])
        self.assertLess(when[0] + 100, when[-1])            # the first item arrives well before the last one


class FakeStreamingClient:
    """Streams a single submit_assessment call in small fragments."""
    p = type("P", (), {"name": "fake-stream", "model": "fake"})()

    def chat_stream(self, messages, tools, on_delta):
        raw = json.dumps(ASSESSMENT)
        for i in range(0, len(raw), 7):
            on_delta(0, "submit_assessment", raw[i:i + 7])
        return Reply("", [ToolCall("c1", "submit_assessment", ASSESSMENT, raw, None)], {}, 5, {})

    def chat(self, messages, tools):
        raise AssertionError("streaming path should be used when on_event is given")


class StreamingPlannerTests(unittest.TestCase):
    def test_run_case_emits_items_before_done(self):
        case = load_case(ROOT / "cases" / "dev" / "D001-dialysis.yaml")
        events = []
        res = run_case(case, "C2", FakeStreamingClient(), TractTable.load(FIXTURE_TRACTS),
                       ResourceTable.load(FIXTURE_RESOURCES), on_event=events.append)
        self.assertEqual(res.assessment, ASSESSMENT)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds.count("item"), 5)
        self.assertLess(kinds.index("item"), kinds.index("done"))


if __name__ == "__main__":
    unittest.main()
