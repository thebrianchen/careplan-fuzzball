# Care Plan Check on Fuzzball

Checks a doctor's care plan against the patient's chart and public neighborhood data, and flags the items this patient
probably can't follow. The patient side and the web side run on separate nodes, and Fuzzball keeps them apart.

CIQ Supercomputing Hackathon, Nov 13–15, 2026. Team: Brian Chen, Lynna Yang, Kevin Yin.

The patient side and the web side run as separate Fuzzball services with separate volumes, and the only request that
crosses asks for a neighborhood, a kind of need and days. Workflows for the planner, the web reader, scoring, the leak
test and training are in `fuzzball/`.

All patients are made up. There is no real patient data in this repo.

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

The other 31 visits have no saved run. To review them, start the server with a planner profile from `config/models.yaml`:

```sh
CAREPLAN_PROFILE=super35-vllm-think VLLM_API_KEY=none PLANNER_URL=http://<planner host>:8000/v1 .venv/bin/python -m careplan.serve
```

The map draws the bus and train trip only when the transit schedule cache is on the machine. To build the cache, run
`python -m pipeline.download --only gtfs_cta`, then the same command with `gtfs_pace`, then `python -m pipeline.transit build`.
Without the cache, the map shows the straight-line distance.

To see the recorded demo page with no install, run `python3 -m http.server 8000 -d careplan/web/demo` and open
http://localhost:8000/.

To run the tests, run `.venv/bin/python -m unittest discover -s tests`.
