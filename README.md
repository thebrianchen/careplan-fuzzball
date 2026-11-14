# Care Plan Check on Fuzzball

Checks a doctor's care plan against the patient's chart and public neighborhood data, and flags the items this patient
probably can't follow. The patient side and the web side run on separate nodes, and Fuzzball keeps them apart.

CIQ Supercomputing Hackathon, Nov 13–15, 2026. Team: Brian Chen, Lynna Yang, Kevin Yin.

## Where this code comes from
We prepared before the event, with the organizers' OK to start early: the planner and its tools, the made-up patient
cases and their physician review, the neighborhood data pipeline, and the leak test. Those pieces are brought into this
repo as they are put to work on the event cluster, and each such commit says so.

Work for the event itself: everything on Fuzzball, all model training, and every number we report, re-run on the event
cluster.

All patients are made up. There is no real patient data in this repo.
