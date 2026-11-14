import tempfile
import unittest
from pathlib import Path

from pipeline.bundle import build, keygen, verify
from pipeline.extract import ground


class BundleTests(unittest.TestCase):
    def test_signed_bundle_verifies_and_tampering_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            src = t / "src"
            src.mkdir()
            (src / "resources.csv").write_text("record_id,name\nR1,Real Pantry\n")
            keygen(t / "keys")
            b = build(t / "keys" / "bundle.key", t / "bundle", src, ["resources.csv"])
            self.assertEqual(verify(t / "keys" / "bundle.pub", b), [])
            (b / "resources.csv").write_text("record_id,name\nR1,Attacker Pantry\n")
            self.assertEqual(verify(t / "keys" / "bundle.pub", b), ["hash mismatch: resources.csv"])
            (b / "resources.csv").write_text("record_id,name\nR1,Real Pantry\n")
            (b / "extra.csv").write_text("x")
            self.assertEqual(verify(t / "keys" / "bundle.pub", b), ["unlisted file: extra.csv"])
            keygen(t / "other")                                   # signed by someone else's key
            self.assertEqual(verify(t / "other" / "bundle.pub", b), ["signature does not match the manifest"])


class GroundingTests(unittest.TestCase):
    PAGE = "Shepherd's Hope 838 W Marquette Rd 773-351-3336 Food Distribution Tuesday 10:00 AM - 2:00 PM"

    def test_keeps_what_the_page_says_and_drops_inventions(self):
        rec, dropped = ground({"name": "Shepherd's Hope", "phone": "773-351-3336",
                               "hours": "Tuesday 10:00 AM - 2:00 PM; Friday 7:00 PM - 9:00 PM"}, self.PAGE)
        self.assertEqual(rec["phone"], "773-351-3336")
        self.assertEqual(rec["schedule"], "Tue 10:00-14:00")      # Friday 7 PM isn't on the page
        rec, dropped = ground({"name": "x", "phone": "312-555-0000", "hours": ""}, self.PAGE)
        self.assertNotIn("phone", rec)
        self.assertEqual(dropped, ["phone"])


if __name__ == "__main__":
    unittest.main()
