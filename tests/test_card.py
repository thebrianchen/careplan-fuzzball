import unittest

from careplan import ROOT
from careplan.card import render
from careplan.cases import load_case
from careplan.data import FIXTURE_RESOURCES, FIXTURE_TRACTS, ResourceTable, TractTable


class CardTests(unittest.TestCase):
    def test_card_resolves_evidence_resources_and_crossings(self):
        case = load_case(ROOT / "cases" / "dev" / "D001-dialysis.yaml")
        case.patient["tract_geoid"] = "17031999901"
        rec = {"case_id": "D001", "profile": "fake", "assessment": {"items": [
            {"item_id": "P1", "status": "blocked", "barriers": [
                {"barrier": "transport", "reason": "No car.", "evidence": ["turn:T02", "tract:no_vehicle_share", "med:99"]}],
             "alternatives": [{"category": "nemt", "resource_record_id": "FX003", "text": "Book rides."}]},
            {"item_id": "P5", "status": "likely_ok"}]},
            "tool_calls": [{"tool": "get_patient", "args": {}}, {"tool": "find_resources",
                           "args": {"tract_id": "17031999901", "need": "transport", "days": ["Mon"]}}]}
        out = render(case, rec, ResourceTable.load(FIXTURE_RESOURCES), TractTable.load(FIXTURE_TRACTS))
        self.assertIn("I sold the car in August", out)                  # transcript ref resolved to its words
        self.assertIn("households with no vehicle: 41%", out)            # tract ref resolved to its value
        self.assertIn("med:99 (not found in the record)", out)          # hallucinated ref is shown as such
        self.assertIn("FIXTURE Medical Ride Line", out)                 # cited resource expanded
        self.assertIn("<td>17031999901</td><td>transport</td><td>Mon</td>", out)   # the boundary panel
        self.assertNotIn("Raymond", out)                                 # no patient name on the card


if __name__ == "__main__":
    unittest.main()
