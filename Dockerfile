# One image for every Care Plan Check service (planner/card backend, gateway, rehearsal stand-ins).
# Multi-arch friendly: build on an arm64 Mac or the GB10 boxes as-is.
#   docker build -t careplan:dev .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY careplan ./careplan
COPY pipeline ./pipeline
COPY leaktest ./leaktest
COPY fuzzball/probes.py ./fuzzball/probes.py
COPY rehearsal ./rehearsal
COPY config ./config
COPY rules ./rules
COPY schemas ./schemas
COPY prompts ./prompts
COPY cases ./cases
COPY data/processed ./data/processed
RUN chmod -R a+rX /app && useradd --uid 10001 --no-create-home app
USER app
CMD ["python", "-m", "careplan.serve"]
