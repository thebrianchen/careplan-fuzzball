"""Build the event-results table for README.md from results/event/.

Usage: python scripts/event_results.py [--write|--check README.md] [--results DIR]
"""
import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / "results" / "event"


def pct(x):
    return f"{int(x * 100 + 0.5)}%"


def recall(entry):
    lo, hi = entry["ci95"]
    return f"{pct(entry['supported_recall'])} ({pct(lo)} to {pct(hi)})"


def cell_scores(measure):
    def cell(d):
        c2, c0 = d["by_condition"]["C2"], d["by_condition"]["C0"]
        return f"{recall(c2[measure])} against {pct(c0[measure]['supported_recall'])}"
    return cell


def flags(d, cond="C2"):
    return f"{d['by_condition'][cond]['false_flags_per_case']:.2f}"


def leak_row(d, side):
    s = d["summary"][side]["ALL"]
    return f"{s['leaks']} of {s['runs']} ({s['hijacked_runs']} hijacked)"


def trained(d, name):
    c3 = d[name]["by_condition"]["C3"]
    return recall(c3["all"]) + f" ({c3['failed_runs']} failed)"


def attack_size(d):
    times = d["leaktest_event1.times"]
    errs = sum(d["leaktest_event1.json"]["summary"][s]["errored_runs"] for s in "AB")
    mins = (max(l[2] for l in times) - min(l[1] for l in times)) / 60
    return f"{d['leaktest_event1.json']['runs']} runs, {errs} errors, " \
           f"{mins:.1f} min as a {len(times)}-task array"


ROWS = [
    ("Barriers found with evidence, 20 held-out patients", "scores-1008",
     ["scores_event1_test.json"], cell_scores("all")),
    ("Barriers that never came up in the visit", "scores-1008",
     ["scores_event1_test.json"], cell_scores("record_only")),
    ("Flags the physician would not raise, per patient", "scores-1008",
     ["scores_event1_test.json"],
     lambda d: f"{flags(d)} against {flags(d, 'C0')}"),
    ("Reviews run, failed", "scores-1008", ["scores_event1_test.json"],
     lambda d: f"{sum(c['runs'] for c in d['by_condition'].values())} reviews, "
               f"{sum(c['failed_runs'] for c in d['by_condition'].values())} failed"),
    ("Barriers found, 12 development patients", "scores-1008",
     ["scores_event1_dev.json"], cell_scores("all")),
    ("Same development patients, every lookup through the gateway", "splitcost-1336",
     ["split_event1_dev.json", "scores_event1_dev.json"],
     lambda d: f"{recall(d['split_event1_dev.json']['by_condition']['C2']['all'])}, "
               f"{flags(d['split_event1_dev.json'])} flags against "
               f"{pct(d['scores_event1_dev.json']['by_condition']['C2']['all']['supported_recall'])}, "
               f"{flags(d['scores_event1_dev.json'])} flags"),
    ("Patient identifiers leaked, one agent with chart and web", "leaktest-1127",
     ["leaktest_event1.json"], lambda d: leak_row(d, "A")),
    ("Same attacks, two-sided design", "leaktest-1127",
     ["leaktest_event1.json"], lambda d: leak_row(d, "B")),
    ("Attack test size", "leaktest-1127",
     ["leaktest_event1.json", "leaktest_event1.times"], attack_size),
    ("Small model after one training pass, against stock", "score-trained-1624",
     ["trained_event1_test.json", "stock_event1_test.json"],
     lambda d: f"{trained(d, 'trained_event1_test.json')} against "
               f"{trained(d, 'stock_event1_test.json')}"),
]


def load(name):
    path = RESULTS / name
    if not path.exists():
        return None
    if name.endswith(".times"):
        return [[float(x) for x in line.split()] for line in
                path.read_text().splitlines() if line.split()]
    return json.loads(path.read_text())


def table():
    lines = ["| Measure | Result | Workflow | File(s) |", "|---|---|---|---|"]
    for label, workflow, files, fn in ROWS:
        data = {f: load(f) for f in files}
        if any(v is None for v in data.values()):
            continue
        payload = data if len(files) > 1 else data[files[0]]
        cells = "`, `".join("results/event/" + f for f in files)
        lines.append(f"| {label} | {fn(payload)} | `{workflow}` | `{cells}` |")
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    global RESULTS
    if "--results" in args:
        i = args.index("--results")
        RESULTS = Path(args[i + 1])
        del args[i:i + 2]
    t = table()
    if not args:
        print(t)
        return 0
    mode, path = args[0], Path(args[1])
    text = path.read_text()
    start = "<!-- event-results:start -->"
    end = "<!-- event-results:end -->"
    if start not in text or end not in text or text.index(end) < text.index(start):
        print(f"marker lines {start} / {end} not found in {path}")
        return 1
    block = text[text.index(start) + len(start):text.index(end)].strip()
    if mode == "--check":
        if block == t:
            return 0
        print("event-results block differs from generated table")
        return 1
    if mode == "--write":
        head = text[:text.index(start) + len(start)]
        path.write_text(head + "\n" + t + "\n" + text[text.index(end):])
        return 0
    print("usage: event_results.py [--write|--check README.md] [--results DIR]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
