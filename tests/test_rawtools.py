import json
import random
import unittest

from careplan.rawtools import ParsedCall, ToolCallTextParser, prepare_messages, type_arguments
from careplan.stream import ItemStreamer
from careplan.tools import SUBMIT_SPEC, TOOL_SPECS

CARD = {"items": [{"item_id": "P1", "status": "likely_ok"},
                  {"item_id": "P2", "status": "at_risk", "barriers": [{"barrier": "transport", "reason": "two buses",
                                                                        "evidence": ["turn:T02"]}]}]}
SUBMIT_XML = ("Here is my assessment.\n<tool_call>\n<function=submit_assessment>\n<parameter=items>\n"
              + json.dumps(CARD["items"], indent=1) + "\n</parameter>\n</function>\n</tool_call>\n")
TWO_CALLS = ("<tool_call>\n<function=get_patient>\n<parameter=patient_id>\nP-D001\n</parameter>\n</function>\n</tool_call>\n"
             "<tool_call>\n<function=find_resources>\n<parameter=tract_id>\n17031840300\n</parameter>\n<parameter=need>\nfood\n"
             "</parameter>\n<parameter=days>\n[\"thu\", \"sun\"]\n</parameter>\n</function>\n</tool_call>")


def chunks(text: str, seed: int):
    rng = random.Random(seed)
    i = 0
    while i < len(text):
        n = rng.randint(1, 9)
        yield text[i:i + n]
        i += n


class ParserTests(unittest.TestCase):
    def test_submit_streams_items_regardless_of_chunking(self):
        for seed in range(12):
            deltas, p = [], ToolCallTextParser(lambda i, n, f: deltas.append((i, n, f)))
            for c in chunks(SUBMIT_XML, seed):
                p.feed(c)
            content, calls = p.result()
            self.assertEqual(content.strip(), "Here is my assessment.")
            self.assertEqual([c.name for c in calls], ["submit_assessment"])
            args, err = type_arguments(calls[0], [SUBMIT_SPEC])
            self.assertIsNone(err)
            self.assertEqual(args, CARD)
            self.assertEqual("".join(f for i, n, f in deltas if i == -1).strip(), "Here is my assessment.")
            s = ItemStreamer()
            items = [it for i, n, f in deltas if n == "submit_assessment" for it in s.feed(f)]
            self.assertEqual([it["item_id"] for it in items], ["P1", "P2"], f"seed {seed}")

    def test_two_calls_and_typing(self):
        for seed in range(6):
            p = ToolCallTextParser()
            for c in chunks(TWO_CALLS, seed):
                p.feed(c)
            content, calls = p.result()
            self.assertEqual(content.strip(), "")
            self.assertEqual([c.name for c in calls], ["get_patient", "find_resources"])
            tools = [TOOL_SPECS["get_patient"], TOOL_SPECS["find_resources"]]
            self.assertEqual(type_arguments(calls[0], tools)[0], {"patient_id": "P-D001"})
            self.assertEqual(type_arguments(calls[1], tools)[0], {"tract_id": "17031840300", "need": "food", "days": ["thu", "sun"]})

    def test_plain_content_and_unterminated_call(self):
        p = ToolCallTextParser()
        p.feed("The plan looks fine. <tool")
        p.feed("_cal")          # looks like a tag start but never completes
        content, calls = p.result()
        self.assertEqual(content, "The plan looks fine. <tool_cal")
        self.assertEqual(calls, [])
        p = ToolCallTextParser()
        p.feed("<tool_call>\n<function=submit_assessment>\n<parameter=items>\n[{\"item_id\": \"P1\"")
        content, calls = p.result()             # stream cut off: keep what we have, planner validation rejects it
        self.assertEqual(calls[0].params["items"], '[{"item_id": "P1"')
        self.assertIsNotNone(type_arguments(calls[0], [SUBMIT_SPEC])[1])

    def test_prepare_messages_parses_argument_strings(self):
        msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function",
                 "function": {"name": "get_patient", "arguments": "{\"patient_id\": \"P1\"}"}}]}]
        out = prepare_messages(msgs)
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"], {"patient_id": "P1"})
        self.assertIsInstance(msgs[0]["tool_calls"][0]["function"]["arguments"], str)   # input untouched

    def test_bad_json_in_typed_param_is_reported(self):
        c = ParsedCall("find_resources", {"tract_id": "1", "need": "food", "days": "[thu"})
        args, err = type_arguments(c, [TOOL_SPECS["find_resources"]])
        self.assertIsNone(args)
        self.assertIn("days", err)


if __name__ == "__main__":
    unittest.main()
