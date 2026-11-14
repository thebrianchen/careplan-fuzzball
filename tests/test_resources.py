"""Open-data resource table: hours parsing, schedule-aware search, and the builder (fixtures only)."""
import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from careplan.data import Resource, ResourceTable, read_schedule
from pipeline.build_resources import FIELDS, build
from pipeline.hours import format_schedule, parse_hours

WEEKDAYS_9_8 = {d: ("09:00", "20:00") for d in ("Mon", "Tue", "Wed", "Thu", "Fri")}


class HoursTests(unittest.TestCase):
    def test_city_formats(self):
        cases = {
            "M-F 9am-8pm; Sat & Sun 10am-4pm, closed 1/19": {**WEEKDAYS_9_8, "Sat": ("10:00", "16:00"), "Sun": ("10:00", "16:00")},
            "M-F 9am-8pm, Sat & Sun Closed, open 1/19 10am-3pm": WEEKDAYS_9_8,
            "M, W: 12PM-8PM; TU, TH: 10AM-6PM; F, SA: 9AM-5PM; SU: 1PM-5PM": {
                "Mon": ("12:00", "20:00"), "Tue": ("10:00", "18:00"), "Wed": ("12:00", "20:00"), "Thu": ("10:00", "18:00"),
                "Fri": ("09:00", "17:00"), "Sat": ("09:00", "17:00"), "Sun": ("13:00", "17:00")},
            "Mon. & Wed., 10-6; Tues. & Thurs., Noon-8; Fri. & Sat., 9-5; Sun., 1-5": {
                "Mon": ("10:00", "18:00"), "Tue": ("12:00", "20:00"), "Wed": ("10:00", "18:00"), "Thu": ("12:00", "20:00"),
                "Fri": ("09:00", "17:00"), "Sat": ("09:00", "17:00"), "Sun": ("13:00", "17:00")},
            "M-F: 8:30 a.m. to 4:30 p.m.": {d: ("08:30", "16:30") for d in ("Mon", "Tue", "Wed", "Thu", "Fri")},
        }
        for text, want in cases.items():
            with self.subTest(text):
                self.assertEqual(parse_hours(text), want)

    def test_unknown_hours_are_empty_not_guessed(self):
        for text in ("Open 1/19 11am-10pm", "Closed until further notice", None, "", "Call for hours"):
            self.assertEqual(parse_hours(text), {})

    def test_schedule_round_trip(self):
        s = parse_hours("M-F 9am-8pm; Sat 10am-4pm")
        self.assertEqual(read_schedule(format_schedule(s)), s)


def _res(rid, schedule=None, lat=41.88):
    return Resource(rid, rid, "utilities", "", lat, -87.73, [], "", "", "", "", "", "", "", "test", schedule or {})


class SearchTests(unittest.TestCase):
    def test_schedule_filters_by_day_and_time_and_ranks_known_hours_first(self):
        weekday = _res("WEEKDAY", WEEKDAYS_9_8, lat=41.8801)
        unknown = _res("UNKNOWN", lat=41.8800)                     # closer, but hours not listed
        table = ResourceTable([weekday, unknown], Path("mem"))
        sat = [r["record_id"] for r in table.search(41.88, -87.73, "utilities", ["Sat"])]
        self.assertEqual(sat, ["UNKNOWN"])                         # weekday-only place is closed Saturday
        tue_eve = [r["record_id"] for r in table.search(41.88, -87.73, "utilities", ["Tue"], "18:00", "19:00")]
        self.assertEqual(tue_eve, ["WEEKDAY", "UNKNOWN"])         # confirmed open beats closer-but-unknown
        tue_late = [r["record_id"] for r in table.search(41.88, -87.73, "utilities", ["Tue"], "21:00", "22:00")]
        self.assertEqual(tue_late, ["UNKNOWN"])
        self.assertEqual(table.search(41.88, -87.73, "utilities")[0]["hours"], "hours not listed; call ahead")

    def test_known_hours_boost_only_applies_nearby(self):
        far_known = _res("FAR_KNOWN", WEEKDAYS_9_8, lat=42.05)    # ~19 km away
        near_unknown = _res("NEAR_UNKNOWN", lat=41.881)
        table = ResourceTable([far_known, near_unknown], Path("mem"))
        self.assertEqual(table.search(41.88, -87.73, "utilities", ["Tue"])[0]["record_id"], "NEAR_UNKNOWN")


class SupersedeTests(unittest.TestCase):
    def test_curated_row_replaces_open_row_at_same_spot(self):
        from pipeline.build_resources import drop_superseded
        open_rows = [{"record_id": "PL1", "need": "food", "lat": 41.8800, "lon": -87.7300},
                     {"record_id": "PL2", "need": "food", "lat": 41.9000, "lon": -87.7300},
                     {"record_id": "WARM1", "need": "utilities", "lat": 41.8800, "lon": -87.7300}]
        cur = [{"record_id": "CUR1", "need": "food", "lat": "41.8802", "lon": "-87.7301"}]
        self.assertEqual([r["record_id"] for r in drop_superseded(open_rows, cur)], ["PL2", "WARM1"])


class BuildTests(unittest.TestCase):
    def test_build_from_fixture_raw_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            center = {"site_type": "Library", "site_name": "Legler", "hours_of_operation": "M-F 9am-8pm",
                      "address": "115 S Pulaski Rd", "zip": "60624", "location": {"coordinates": [-87.7259, 41.8796]}}
            spray = {**center, "site_type": "Park District Spray Feature", "site_name": "Spray"}
            (raw / "chi_warming_centers.json").write_text(json.dumps([center]))
            (raw / "chi_cooling_centers.json").write_text(json.dumps([center, spray]))
            (raw / "chi_libraries.json").write_text(json.dumps([
                {"branch_": "Legler", "service_hours": "Mon. - Thurs., 9-8; Fri. & Sat., 9-5", "address": "115 S Pulaski Rd",
                 "zip": "60624", "location": {"latitude": "41.8796", "longitude": "-87.7259"}},
                {"branch_": "Closed One", "service_hours": "Closed until further notice", "location": {"latitude": "41.9", "longitude": "-87.7"}}]))
            hrsa_cols = ["Health Center Type", "Site Name", "Site Address", "Site City", "Site Postal Code",
                         "Site Telephone Number", "Site Web Address", "Operating Hours per Week", "Site Status Description",
                         "Health Center Name", "Geocoding Artifact Address Primary X Coordinate",
                         "Geocoding Artifact Address Primary Y Coordinate",
                         "State and County Federal Information Processing Standard Code"]
            with (raw / "hrsa_health_center_sites.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=hrsa_cols)
                w.writeheader()
                base = dict(zip(hrsa_cols, ["Federally Qualified Health Center (FQHC)", "Bridges HC", "4221 W Lake St",
                                            "Chicago", "60624-1723", "872-235-0777", "", "40.00", "Active", "PCC WELLNESS",
                                            "-87.7312", "41.8857", "17031"]))
                w.writerow(base)
                w.writerow({**base, "Site Name": "Elsewhere", "State and County Federal Information Processing Standard Code": "17043"})
            snap_cols = ["Record ID", "Store Name", "Store Type", "Street Number", "Street Name", "City", "State",
                         "Zip Code", "County", "Latitude", "Longitude", "End Date"]
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=snap_cols)
            w.writeheader()
            w.writerow(dict(zip(snap_cols, ["1", "SAVE A LOT", "Supermarket", "4500", "W Madison St", "CHICAGO", "IL", "60624", "COOK", "41.88", "-87.74", " "])))
            w.writerow(dict(zip(snap_cols, ["2", "GAS N GO", "Convenience Store", "1", "Main", "CHICAGO", "IL", "60624", "COOK", "41.88", "-87.74", " "])))
            w.writerow(dict(zip(snap_cols, ["3", "CLOSED MART", "Supermarket", "1", "Main", "CHICAGO", "IL", "60624", "COOK", "41.88", "-87.74", "1/2/2020"])))
            with zipfile.ZipFile(raw / "snap_retailers_2005_2025.zip", "w") as z:
                z.writestr("snap.csv", buf.getvalue())
            rows, problems = build(raw, raw / "no-curated.csv")
        kinds = sorted(r["kind"] for r in rows)
        self.assertEqual(kinds, ["cooling_center", "fqhc", "library", "snap_grocery", "warming_center"])
        self.assertEqual(problems, [])
        self.assertTrue(all(set(r) == set(FIELDS) and r["source_url"] and r["source_hash"] for r in rows))
        grocery = next(r for r in rows if r["kind"] == "snap_grocery")
        self.assertEqual(grocery["name"], "Save A Lot")
        lib = next(r for r in rows if r["kind"] == "library")
        self.assertEqual(read_schedule(lib["schedule"])["Sat"], ("09:00", "17:00"))


if __name__ == "__main__":
    unittest.main()
