"""Pipeline parsers against tiny files in the real formats (fake values, fake tract ids)."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import pandas as pd

from pipeline import build_tracts as bt
from pipeline.coverage import report

T1, T2, OTHER = "17031999901", "17031999902", "18089000100"


def make_raw(d: Path) -> None:
    (d / "2020_gaz_tracts_17.txt").write_text(
        "USPS\tGEOID\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG                    \n"
        f"IL\t{T1}\t1000\t0\t0.1\t0\t41.881000\t-87.731000         \n"
        f"IL\t{T2}\t1000\t0\t0.1\t0\t41.942000\t-87.653000         \n"
        f"IN\t{OTHER}\t1000\t0\t0.1\t0\t41.600000\t-87.400000         \n")
    (d / "acs_b08201_17031.json").write_text(json.dumps([
        ["NAME", "B08201_001E", "B08201_002E", "state", "county", "tract"],
        ["Tract 9999.01", "1000", "410", "17", "031", "999901"],
        ["Tract 9999.02", "800", "64", "17", "031", "999902"]]))
    with zipfile.ZipFile(d / "fara_2019.zip", "w") as z:
        z.writestr("FoodAccessResearchAtlasData2019.csv",
                   "CensusTract,State,LILATracts_1And10,LILATracts_halfAnd10,LowIncomeTracts\n"
                   "17031999800,Illinois,1,1,1\n17031999700,Illinois,0,1,0\n")
    (d / "tab20_tract20_tract10_natl.txt").write_text(
        "OID_TRACT_20|GEOID_TRACT_20|GEOID_TRACT_10|AREALAND_PART\n"
        f"1|{T1}|17031999800|900\n2|{T1}|17031999700|100\n3|{T2}|17031999700|1000\n")
    (d / "places_tract_il.csv").write_text(
        "StateAbbr,CountyFIPS,TractFIPS,LACKTRPT_CrudePrev,FOODINSECU_CrudePrev,DIABETES_CrudePrev\n"
        f"IL,17031,{T1},14.2,24.9,19.5\nIL,17031,{T2},5.1,8.3,8.0\n")
    with zipfile.ZipFile(d / "gtfs_cta.zip", "w") as z:
        z.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon,location_type\n"
                                 "A,Near early,41.8812,-87.7320,0\nB,Near late,41.8800,-87.7300,0\n"
                                 "C,Far,41.9500,-87.6500,0\nS,Station parent,41.8810,-87.7310,1\n")
        z.writestr("trips.txt", "route_id,service_id,trip_id\nR1,WK,t1\nR1,WK,t2\nR2,SAT,t3\n")
        z.writestr("calendar.txt", "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                                    "WK,1,1,1,1,1,0,0,20260101,20261231\nSAT,0,0,0,0,0,1,0,20260101,20261231\n")
        z.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                                      "t1,05:30:00,05:30:00,A,1\nt2,07:00:00,07:00:00,B,1\nt3,5:00:00,5:00:00,B,1\n"
                                      "t2,07:30:00,07:30:00,C,2\n")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.raw = Path(self.td.name)
        make_raw(self.raw)
        self.df = bt.build(self.raw, ["17031"]).set_index("tract_geoid")

    def tearDown(self):
        self.td.cleanup()

    def test_county_filter_and_centroids(self):
        self.assertEqual(sorted(self.df.index), [T1, T2])
        self.assertAlmostEqual(self.df.loc[T1, "intpt_lat"], 41.881)

    def test_acs_no_vehicle_share(self):
        self.assertAlmostEqual(self.df.loc[T1, "no_vehicle_share"], 0.41)
        self.assertAlmostEqual(self.df.loc[T2, "no_vehicle_share"], 0.08)

    def test_fara_crosswalk_area_weighted(self):
        self.assertEqual(self.df.loc[T1, "lila_1_10"], 1)      # 90% of land came from a flagged 2010 tract
        self.assertEqual(self.df.loc[T2, "lila_1_10"], 0)
        self.assertEqual(self.df.loc[T2, "lila_half_10"], 1)

    def test_transit_counts_and_early_service(self):
        self.assertEqual(self.df.loc[T1, "transit_stops_800m"], 2)   # A and B, not the parent station or C
        self.assertEqual(self.df.loc[T1, "early_service_stops"], 1)  # only A has a weekday departure before 06:00
        self.assertEqual(self.df.loc[T2, "early_service_stops"], 0)

    def test_places_measures(self):
        self.assertAlmostEqual(self.df.loc[T1, "places_lacktrpt"], 14.2)
        self.assertIn("places_diabetes", self.df.columns)

    def test_coverage_report(self):
        cases = pd.DataFrame({"case_id": ["D001", "D002"], "tract_geoid": [T1, T2]})
        text, passed = report(self.df.reset_index(), cases)
        self.assertIn("Geocode match rate: **100.0%**", text)
        self.assertTrue(passed)


if __name__ == "__main__":
    unittest.main()
