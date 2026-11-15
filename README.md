# Care Plan Check on Fuzzball

Checks a doctor's care plan against the patient's chart and public neighborhood data, and flags the items this patient
probably can't follow. The patient side and the web side run on separate nodes, and Fuzzball keeps them apart.

CIQ Supercomputing Hackathon, Nov 13–15, 2026. Team: Brian Chen, Lynna Yang, Kevin Yin.

The patient side and the web side run as separate Fuzzball services with separate volumes, and the only request that
crosses asks for a neighborhood, a kind of need and days. The workflow files that ran at the event are in `fuzzball/event/`.

All patients are made up. There is no real patient data in this repo.

## Results from the event cluster

These numbers come from the two GB10 nodes that the event's Fuzzball cluster gave our team, on November 14, 2026. The planner is
Nemotron 3.5 Super (120B, NVFP4) on vLLM with medium-effort reasoning and three requests in flight. A physician on the team
reviewed the answer key of every test patient; every score uses her key. "Barriers found with evidence" counts a barrier
only when the review cites evidence that can support it.

`scripts/event_results.py` builds this table from the files in `results/event/`. The Workflow column names the Fuzzball
workflow that produced each file.

<!-- event-results:start -->
| Measure | Result | Workflow | File(s) |
|---|---|---|---|
| Barriers found with evidence, 20 held-out patients | 62% (50% to 74%) against 47% | `scores-1008` | `results/event/scores_event1_test.json` |
| Barriers that never came up in the visit | 43% (24% to 61%) against 0% | `scores-1008` | `results/event/scores_event1_test.json` |
| Flags the physician would not raise, per patient | 0.45 against 0.33 | `scores-1008` | `results/event/scores_event1_test.json` |
| Reviews run, failed | 120 reviews, 0 failed | `scores-1008` | `results/event/scores_event1_test.json` |
| Barriers found, 12 development patients | 58% (43% to 77%) against 49% | `scores-1008` | `results/event/scores_event1_dev.json` |
| Same development patients, every lookup through the gateway | 62% (46% to 80%), 0.08 flags against 58%, 0.22 flags | `splitcost-1336` | `results/event/split_event1_dev.json`, `results/event/scores_event1_dev.json` |
| Patient identifiers leaked, one agent with chart and web | 17 of 114 (19 hijacked) | `leaktest-1127` | `results/event/leaktest_event1.json` |
| Same attacks, two-sided design | 0 of 114 (0 hijacked) | `leaktest-1127` | `results/event/leaktest_event1.json` |
| Attack test size | 228 runs, 0 errors, 123.5 min as a 6-task array | `leaktest-1127` | `results/event/leaktest_event1.json`, `results/event/leaktest_event1.times` |
| Small model after one training pass, against stock | 35% (19% to 52%) (4 failed) against 12% (4% to 21%) (7 failed) | `score-trained-1624` | `results/event/trained_event1_test.json`, `results/event/stock_event1_test.json` |
<!-- event-results:end -->

The comparison in the first rows is against the same model with only the visit transcript. On what the patient said aloud,
the transcript-only review does as well or better. Care Plan Check's gain comes from the chart and the neighborhood.
On the event cluster both sides ran under one team login, so the cluster did not refuse the public side access to patient
files. The separation there is the workflow files (the gateway mounts only the public volume) and the gateway's checks.

### Check a number

To rebuild the table, or to confirm that this README matches the files:

```sh
python scripts/event_results.py
python scripts/event_results.py --check README.md
```

To score the saved reviews again, with the same code the cluster ran:

```sh
python scripts/h1_report.py results/event/by-run/*_event1_test_? --cases cases/test --out /tmp/test.json
python scripts/h1_report.py results/event/by-run/*_event1_split_? --cases cases/dev --out /tmp/split.json
python -m leaktest.run --merge results/event/by-run/*_leaktest_s?of6 --out /tmp/leaktest.json
```

Each command prints the same figures as the table. `results/event/by-run/` holds every review and every attack run as the
cluster wrote it: the model's tool calls, the review it submitted, and for an attack run the web requests the agent made.
The result files name these folders as `runs/...`, their path on the cluster.

### What ran, and how to run it again

| Workflow | What it did | Fuzzfile | As submitted |
|---|---|---|---|
| `scores-1008` | Served the planner, then reviewed the 20 held-out and 12 development patients three times each, with and without the chart and neighborhood tools | `fuzzball/event/scores.fz.yaml` | `results/event/workflows/scores-1008.yaml` |
| `leaktest-1127` | Served a second planner on the other node and ran 38 attack pages, three passes, against both designs, as a six-task array | `fuzzball/event/leaktest.fz.yaml` | `results/event/workflows/leaktest-1127.yaml` |
| `zones-1208` | Put the patient cases on their own volume and built the public side's copy of the code with no patient files | `fuzzball/event/zones.fz.yaml` | `results/event/workflows/zones-1208.yaml` |
| `gateway-1210` | Ran the gateway behind a Fuzzball endpoint. It admits a caller only when the endpoint's signed identity checks out | `fuzzball/event/gateway.fz.yaml` | `results/event/workflows/gateway-1210.yaml` |
| `splitcost-1336` | Reviewed the development patients again with every resource lookup crossing the gateway | `fuzzball/event/splitcost.fz.yaml` | `results/event/workflows/splitcost-1336.yaml` |
| `train-1344` | Trained a LoRA add-on for Nemotron 3 Nano on one GB10 | `fuzzball/event/train.fz.yaml` | `results/event/workflows/train-1344.yaml` |
| `score-trained-1624` | Reviewed the 20 held-out patients with Nemotron 3 Nano, with and without the trained add-on | `fuzzball/event/score-trained.fz.yaml` | `results/event/workflows/score-trained-1624.yaml` |

The files under `results/event/workflows/` are the output of `fuzzball workflow get` for each run.

To run the measurements on another Fuzzball cluster with two GPU nodes:

1. Create the volumes `models`, `code`, `patient` and `public` on your storage provisioner, and replace `team4` in the Fuzzfiles with its name.
2. Start `fetch-models.fz.yaml` to download the models, then `code.fz.yaml` to put this repo and its Python packages on the `code` volume.
3. Start `scores.fz.yaml` on one node and `leaktest.fz.yaml` on the other.
4. Start `zones.fz.yaml`, then `gateway.fz.yaml`. Put the gateway's endpoint address and your own account id in `gateway.fz.yaml` and `splitcost.fz.yaml`, then start `splitcost.fz.yaml`.
5. Start `train.fz.yaml`, then `score-trained.fz.yaml` when it finishes.

## Try it

You need Python 3.12. You do not need a GPU, an API key or the cluster.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m careplan.serve
```

Open http://localhost:8080/app/ and choose the first visit, Raymond Ellis. Press **Review the plan**. The page replays a
real review that was saved earlier, and labels it as a saved run. Each flag shows its evidence: a map of the trip to
dialysis, the visits he missed, and how his neighborhood compares with the county. Click a line of evidence to see where
it comes from. Accept, edit or dismiss each flag, then prepare the patient handout.

The other 31 visits have no saved run, so they need the planner. The server looks for the event planner (Nemotron 3.5
Super on vLLM) at `PLANNER_URL`, or at `http://localhost:8000/v1` when that is not set. On the cluster, `PLANNER_URL` is the
planner service or its endpoint. `CAREPLAN_PLANNER_FALLBACKS` lists other planner endpoints, separated by commas. The
server uses the first endpoint that answers, and the System page says which one. When no planner answers, the page says
so and offers the saved run.

```sh
PLANNER_URL=http://<cluster planner>:8000/v1 CAREPLAN_PLANNER_FALLBACKS=http://<our GB10>:8000/v1 .venv/bin/python -m careplan.serve
```

The map draws the bus and train trip from `data/processed/transit_wed.npz`, one weekday of CTA and Pace schedules. To
rebuild it from the current feeds, run `python -m pipeline.download --only gtfs_cta`, then the same command with
`gtfs_pace`, then `python -m pipeline.transit build`.

To see the recorded demo page with no install, run `python3 -m http.server 8000 -d careplan/web/demo` and open
http://localhost:8000/.

To run the tests, run `.venv/bin/python -m unittest discover -s tests`.
