import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "event_results.py"

COND = {"all": {"conflict_reps": 4, "supported_recall": 0.5, "ci95": [0.2, 0.8]},
        "record_only": {"conflict_reps": 2, "supported_recall": 0.25, "ci95": [0.0, 0.5]},
        "spoken": {"conflict_reps": 2, "supported_recall": 0.75, "ci95": [0.5, 1.0]}}


def scores(cond="C2"):
    c = dict(COND)
    c.update({"false_flags_per_case": 0.5, "failed_runs": 0, "runs": 2})
    return {"by_condition": {"C0": c, cond: c}}


def leaktest():
    side = {"ALL": {"runs": 2, "leaks": 1, "leak_rate": 0.5, "leak_ci95": [0, 1],
                    "hijacked_runs": 1, "misled_runs": 1, "misled_before_check": 1},
            "errored_runs": 0}
    return {"runs": 4, "profile": "p", "summary": {"A": side, "B": side}, "rows": []}


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


class EventResultsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "scores_event1_test.json").write_text(json.dumps(scores()))
        (self.dir / "scores_event1_dev.json").write_text(json.dumps(scores()))
        (self.dir / "split_event1_dev.json").write_text(json.dumps(scores()))
        (self.dir / "leaktest_event1.json").write_text(json.dumps(leaktest()))
        (self.dir / "leaktest_event1.times").write_text("1 0 60 0\n2 30 90 0\n")

    def tearDown(self):
        self.tmp.cleanup()

    def table(self):
        r = run("--results", str(self.dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def data_rows(self):
        return [l for l in self.table().splitlines()
                if l.startswith("| ") and not l.startswith("| Measure")]

    def test_row_per_available_file_and_missing_rows_omitted(self):
        rows = self.data_rows()
        self.assertEqual(len(rows), 9)
        self.assertFalse(any("Small model" in r for r in rows))
        (self.dir / "trained_event1_test.json").write_text(json.dumps(scores("C3")))
        (self.dir / "stock_event1_test.json").write_text(json.dumps(scores("C3")))
        rows = self.data_rows()
        self.assertEqual(len(rows), 10)
        self.assertTrue(any("Small model" in r for r in rows))

    def test_write_then_check_passes_and_fails_after_edit(self):
        readme = self.dir / "README.md"
        readme.write_text("top\n<!-- event-results:start -->\nold\n<!-- event-results:end -->\nbottom\n")
        r = run("--write", str(readme), "--results", str(self.dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run("--check", str(readme), "--results", str(self.dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        text = readme.read_text()
        self.assertIn("<!-- event-results:start -->", text)
        self.assertIn("bottom", text)
        readme.write_text(text.replace("50% (20% to 80%)", "99%"))
        r = run("--check", str(readme), "--results", str(self.dir))
        self.assertEqual(r.returncode, 1)

    def test_write_is_idempotent(self):
        readme = self.dir / "README.md"
        readme.write_text("<!-- event-results:start -->\nx\n<!-- event-results:end -->\n")
        run("--write", str(readme), "--results", str(self.dir))
        once = readme.read_text()
        run("--write", str(readme), "--results", str(self.dir))
        self.assertEqual(once, readme.read_text())

    def test_missing_markers_exit_one(self):
        readme = self.dir / "README.md"
        readme.write_text("no markers\n")
        for mode in ("--write", "--check"):
            r = run(mode, str(readme), "--results", str(self.dir))
            self.assertEqual(r.returncode, 1, mode)


if __name__ == "__main__":
    unittest.main()
