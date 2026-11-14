# Care Plan Check on Fuzzball

Checks a doctor's care plan against the patient's chart and public neighborhood data, and flags the items this patient
probably can't follow. The patient side and the web side run on separate nodes, and Fuzzball keeps them apart.

CIQ Supercomputing Hackathon, Nov 13–15, 2026. Team: Brian Chen, Lynna Yang, Kevin Yin.

The patient side and the web side run as separate Fuzzball services with separate volumes, and the only request that
crosses asks for a neighborhood, a kind of need and days. Workflows for the planner, the web reader, scoring, the leak
test and training are in `fuzzball/`.

All patients are made up. There is no real patient data in this repo.
