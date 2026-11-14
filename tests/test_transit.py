"""get_transit_trip on box 2's local schedules, and transit refs as scored evidence."""
import copy
import unittest

from careplan import ROOT, transit_tool
from careplan.cases import Case, build_ref_index, load_case
from careplan.data import ResourceTable, TractTable
from careplan.scoring import check_evidence
from careplan.tools import Toolbox, ToolError
from pipeline.transit import CACHE

MT_SINAI = "142302"


@unittest.skipUnless(CACHE.exists() and (ROOT / "data/processed/dialysis_chicago.csv").exists(),
                     "needs `python -m pipeline.transit build` and the dialysis facility table")
class TransitToolTests(unittest.TestCase):
    def setUp(self):
        base = load_case(ROOT / "cases" / "dev" / "D001-dialysis.yaml").data
        data = copy.deepcopy(base)
        data["patient"]["tract_geoid"] = "17031260300"
        data["patient"]["appointments"][0].update(facility_ccn=MT_SINAI, time="06:00")
        self.case = Case(data=data, path=None, refs=build_ref_index(data))
        self.tracts = TractTable.load()
        self.tb = Toolbox(self.case, self.tracts, ResourceTable.load())
        self.ref = data["patient"]["appointments"][0]["ref"]

    def test_feasible_trip_and_evidence(self):
        out = self.tb.call("get_transit_trip", {"appt_ref": self.ref})
        self.assertTrue(out["feasible"])
        self.assertLessEqual(out["arrival"], "06:00")
        chk = check_evidence(self.case, "transport", [f"transit:{self.ref}"], self.tracts.get("17031260300"))
        self.assertFalse(chk.ok)                     # an on-time trip exists, so this is not transport evidence

    def test_infeasible_trip_supports_transport(self):
        self.case.refs[self.ref]["time"] = "03:30"   # nothing runs early enough
        out = self.tb.call("get_transit_trip", {"appt_ref": self.ref})
        self.assertFalse(out["feasible"])
        chk = check_evidence(self.case, "transport", [f"transit:{self.ref}"], self.tracts.get("17031260300"))
        self.assertTrue(chk.ok)

    def test_the_rides_drawn_on_the_map_are_the_trip_the_tool_reports(self):
        t = self.tracts.get("17031260300")
        home = (t["intpt_lat"], t["intpt_lon"], MT_SINAI, "06:00")
        trip, rides = transit_tool.trip(*home), transit_tool.rides(*home)
        self.assertTrue(rides)
        self.assertGreaterEqual(rides[0]["board"], trip["latest_departure"])
        self.assertLessEqual(rides[-1]["alight"], trip["arrival"])
        times = [x for r in rides for x in (r["board"], r["alight"])]
        self.assertEqual(times, sorted(times))                               # one ride after another
        self.assertTrue(all(len(r["path"]) >= 2 for r in rides))
        self.assertEqual(transit_tool.rides(t["intpt_lat"], t["intpt_lon"], MT_SINAI, "03:30"), [])     # no on-time trip, nothing to draw

    def test_no_facility_is_a_tool_error(self):
        del self.case.refs[self.ref]["facility_ccn"]
        with self.assertRaises(ToolError):
            self.tb.call("get_transit_trip", {"appt_ref": self.ref})


if __name__ == "__main__":
    unittest.main()
