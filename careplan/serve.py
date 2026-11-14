"""Box 2's card backend: runs the planner on a case and serves the clinician card.

    python -m careplan.serve                    # port 8080

GET  /healthz          liveness
GET  /                 case list with links
GET  /app/             the clinic app: the doctor's workspace and the operator's system page (routes in careplan/webapp.py)
GET  /card/<case_id>   run (once, cached) and render the card
GET  /stage/<case_id>  the stage view (live visit + streamed card + boundary); /live/<case_id> is an alias
GET  /summary/<case_id> patient-facing after-visit summary (printable); POST /summarize/<case_id> generates it (one model call, patient's language)
GET  /audit   crossings + clinician decisions
GET  /attack/<case_id>  beat 2: one attack page, run as single agent or split; /attack/<case_id>/run?design=A|B streams it
Templates: careplan/web/templates (Jinja2), assets: careplan/web/static (served at /static/)
POST /check/<case_id>  run again; returns the result JSON

Environment (all optional):
  CAREPLAN_PROFILE        model profile in config/models.yaml (default super35-vllm-think: the event planner, at PLANNER_URL
                          or http://localhost:8000/v1)
  CAREPLAN_SAMPLE         if set to 1, the clinic app offers sample reviews built from the answer key (careplan/sample.py), for page work
  CAREPLAN_PLANNER_FALLBACKS  the clinic app's other planner endpoints, comma-separated, tried in order when the first does not answer
  CAREPLAN_MODEL_BASE_URL override the profile's base URL (e.g. http://llm:8000/v1 for on-box vLLM)
  CAREPLAN_MODEL_API_KEY_ENV  env var holding the model key (default: the profile's)
  CAREPLAN_GATEWAY_URL    if set, find_resources crosses to box 1 over HTTP (through the endpoint proxy)
  CAREPLAN_GATEWAY_TOKEN  bearer token for that crossing (a Fuzzball endpoint token in the cluster)
  CAREPLAN_CASES          case directory (default cases)
  CAREPLAN_PREFETCH       if set to 1, the record reads run before the first model call (fewer steps)
  CAREPLAN_FINAL          "tool" (default) or "text": the card as streamed JSON content, so items show one by one
Logs carry case ids and statuses only, never patient fields: job logs pass through the control plane.
"""
from __future__ import annotations

import dataclasses
import hashlib
import html
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import ROOT
from .card import render
from .cases import load_cases
from .data import ResourceTable, TractTable
from .gateway import http_gateway
from .llm import ChatClient
from .llm import make_client as client_for_profile
from .planner import run_case
from .runner import apply_case_tracts, load_profiles
from .tools import Toolbox

WEB = Path(__file__).parent / "web"
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".html": "text/html; charset=utf-8", ".json": "application/json"}


def _templates():
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    return Environment(loader=FileSystemLoader(str(WEB / "templates")), autoescape=select_autoescape(["html"]))


REC_DIR = Path(os.environ.get("CAREPLAN_RECORDINGS", ROOT / "recordings"))


def save_recording(name: str, events: list, rec: dict | None = None, profile: str = "") -> None:
    """A real run's event stream with its timing, for the stage's recorded fallback (?record=1 saves, ?replay=1 plays)."""
    REC_DIR.mkdir(parents=True, exist_ok=True)
    (REC_DIR / f"{name}.json").write_text(json.dumps(
        {"saved": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "profile": profile, "events": events, "rec": rec}, default=str))


def replay_recording(name: str, write) -> dict | None:
    f = REC_DIR / f"{name}.json"
    if not f.is_file():
        return None
    data = json.loads(f.read_text())
    t0 = time.time()
    write({"type": "recorded", "saved": data.get("saved"), "profile": data.get("profile"), "t": 0})
    for off, ev in data["events"]:
        delay = off - (time.time() - t0)
        if delay > 0:
            time.sleep(delay)
        write(ev)
    return data


def make_client() -> ChatClient:
    p = load_profiles()[os.environ.get("CAREPLAN_PROFILE", "super35-vllm-think")]
    changes = {}
    if os.environ.get("CAREPLAN_MODEL_BASE_URL"):
        changes["base_url"] = os.environ["CAREPLAN_MODEL_BASE_URL"]
    if os.environ.get("CAREPLAN_MODEL_API_KEY_ENV"):
        changes["api_key_env"] = os.environ["CAREPLAN_MODEL_API_KEY_ENV"]
    return client_for_profile(dataclasses.replace(p, **changes))


class App:
    def __init__(self):
        self.cases = {c.case_id: c for c in load_cases(Path(os.environ.get("CAREPLAN_CASES", ROOT / "cases")))}
        apply_case_tracts(list(self.cases.values()))
        self.tracts, self.resources = TractTable.load(), ResourceTable.load()
        self.client = make_client()
        url = os.environ.get("CAREPLAN_GATEWAY_URL")
        self.gateway = http_gateway(url, os.environ.get("CAREPLAN_GATEWAY_TOKEN")) if url else None
        self.prefetch = os.environ.get("CAREPLAN_PREFETCH", "0") == "1"
        self.final = os.environ.get("CAREPLAN_FINAL", "tool")
        self.signals = os.environ.get("CAREPLAN_SIGNALS", "0") == "1"     # the frozen demo config: rule signals + rule v2
        self.live: dict[str, list[dict]] = {}       # case_id -> turns captured from the mic
        self.transcriber = None
        self.feedback_path = Path(os.environ.get("CAREPLAN_FEEDBACK", ROOT / "runs" / "feedback.jsonl"))
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)
        self.results: dict[str, dict] = {}
        self.summaries: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.templates = _templates()
        self._attack = None

    def attack(self):
        if self._attack is None:
            from .attackstage import AttackStage
            self._attack = AttackStage(self.tracts, self.resources)
        return self._attack

    def summarize(self, case_id: str) -> dict:
        from .summary import build
        rec = self.results.get(case_id) or self.check(case_id)
        if not rec.get("assessment"):
            raise ValueError("no card to summarize")
        out = build(self.cases[case_id], rec, self.client)
        with self.lock:
            self.summaries[case_id] = out
        print(json.dumps({"event": "summary", "case_id": case_id, "language": out["language"], "items": len(out["items"])}), flush=True)
        return out

    def page(self, name: str, **ctx) -> str:
        return self.templates.get_template(name).render(profile=self.client.p.name, **ctx)

    def crossings(self) -> list[dict]:
        out = []
        for cid, rec in sorted(self.results.items()):
            for tc in rec.get("tool_calls", []):
                if tc["tool"] == "find_resources":
                    a = tc["args"]
                    out.append({"case_id": cid, "tract_id": a.get("tract_id"), "need": a.get("need"), "days": ",".join(a.get("days") or []),
                                "time": (a.get("start") or "") + ("–" + a["end"] if a.get("end") else ""),
                                "caller": "svc-private (verified)" if self.gateway else "local table (no crossing)"})
        return out

    def feedback_rows(self) -> list[dict]:
        if not self.feedback_path.exists():
            return []
        return [json.loads(l) for l in self.feedback_path.read_text().splitlines() if l.strip()][-200:]

    # ---- live visit capture: turns transcribed on this box, never leave it
    def add_turn(self, case_id: str, speaker: str, pcm: bytes) -> dict:
        if speaker not in ("doctor", "patient", "caregiver", "nurse", "interpreter"):
            raise ValueError("unknown speaker")
        if self.transcriber is None:
            from .speech import Transcriber
            self.transcriber = Transcriber(os.environ.get("CAREPLAN_ASR_MODEL", "small.en"))
        from .speech import chart_vocabulary
        text = self.transcriber.pcm16(pcm, initial_prompt=chart_vocabulary(self.cases[case_id]))
        with self.lock:
            turns = self.live.setdefault(case_id, [])
            turn = {"ref": f"turn:L{len(turns) + 1:02d}", "speaker": speaker, "text": text or "[inaudible]"}
            turns.append(turn)
        print(json.dumps({"event": "turn", "case_id": case_id, "ref": turn["ref"], "chars": len(text)}), flush=True)
        return turn

    # ---- clinician feedback: the supervision signal for the overnight fine-tune. Stays on box 2.
    def feedback(self, case_id: str, body: dict) -> dict:
        decision, item_id = body.get("decision"), body.get("item_id")
        if decision not in ("accept", "edit", "dismiss") or not isinstance(item_id, str):
            raise ValueError("decision must be accept|edit|dismiss with an item_id")
        rec = self.results.get(case_id) or {}
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "case_id": case_id, "item_id": item_id,
               "decision": decision, "note": (body.get("note") or "")[:400] or None,
               "assessment_sha256": rec.get("card_sha256"),      # hash of the card as first shown, stable across later edits
               "profile": rec.get("profile")}
        with self.lock:
            with self.feedback_path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            self._apply_decision(rec, item_id, decision, row["note"])
        print(json.dumps({"event": "feedback", "case_id": case_id, "item_id": item_id, "decision": decision}), flush=True)
        return row

    @staticmethod
    def _apply_decision(rec: dict, item_id: str, decision: str, note: str | None) -> None:
        """The card the clinician sees, and the card the night job trains on, reflect the decision:
        dismiss -> the item is likely_ok (the flag was wrong); edit -> the clinician's wording becomes the first
        alternative and the item stays flagged. Accept changes nothing. Keeps the assessment schema-valid."""
        for it in (rec.get("assessment") or {}).get("items", []):
            if it["item_id"] != item_id:
                continue
            if decision == "dismiss":
                it["status"], it["barriers"], it["alternatives"] = "likely_ok", [], []
            elif decision == "edit" and note:
                alts = [a for a in it.get("alternatives", []) if a.get("category") != "clinician_edit"]
                it["alternatives"] = [{"category": "clinician_edit", "resource_record_id": None, "text": note[:400]}] + alts[:2]
        rec.setdefault("decisions", {})[item_id] = {"decision": decision, "note": note}   # badge on the card; not part of the assessment

    def reset_visit(self, case_id: str) -> None:
        with self.lock:
            self.live.pop(case_id, None)

    def case_for(self, case_id: str, live: str | bool = False):
        """The case as written (live falsy), with its transcript replaced by the captured turns (live="1"), or with the
        captured turns appended to the written visit (live="append": the stage hybrid, one live exchange on top of the
        visit so far)."""
        case = self.cases[case_id]
        turns = self.live.get(case_id) if live else None
        if not turns:
            return case
        from .cases import Case, build_ref_index
        data = json.loads(json.dumps(case.data))
        data["visit"]["transcript"] = ([dict(t) for t in case.visit["transcript"]] if live == "append" else []) + [dict(t) for t in turns]
        return Case(data, case.path, build_ref_index(data))

    def check(self, case_id: str, on_event=None, live: str | bool = False) -> dict:
        case = self.case_for(case_id, live)
        t0 = time.time()
        tb = Toolbox(case, self.tracts, self.resources, gateway=self.gateway, signals=self.signals)
        res = run_case(case, "C2", self.client, self.tracts, self.resources, toolbox=tb, on_event=on_event,
                       prefetch=self.prefetch, final=self.final, signals=self.signals)
        rec = {"case_id": case_id, "profile": self.client.p.name, "prefetch": self.prefetch, "final": self.final, "signals": self.signals,
               "live_transcript": live and bool(self.live.get(case_id)), "assessment": res.assessment, "error": res.error,
               "steps": res.steps, "tool_calls": res.tool_calls, "usage": res.usage, "wall_s": round(time.time() - t0, 1),
               "crossed_via_gateway": self.gateway is not None,
               "card_sha256": hashlib.sha256(json.dumps(res.assessment, sort_keys=True).encode()).hexdigest() if res.assessment else None}
        with self.lock:
            self.results[case_id] = rec
        print(json.dumps({"event": "check", "case_id": case_id, "ok": res.assessment is not None, "steps": res.steps,
                          "wall_s": rec["wall_s"]}), flush=True)
        return rec


def make_server(app: App, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _case(self) -> str | None:
            cid = self.path.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            return cid if cid in app.cases else None

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/healthz":
                return self._send(200, '{"ok": true}', "application/json")
            if path == "/app" or path.startswith(("/app/", "/api/app/")):     # the clinic app: careplan/web/app + careplan/webapp.py
                from . import webapp
                return webapp.handle(app, self, "GET", path, query)
            if path in ("", "/"):
                cases = [{"case_id": cid, "title": c.data.get("title", ""), "n_items": len(c.plan), "visit_date": c.visit.get("date", ""),
                          "checked": cid in app.results, "ok": bool((app.results.get(cid) or {}).get("assessment"))}
                         for cid, c in sorted(app.cases.items())]
                return self._send(200, app.page("index.html", nav="home", cases=cases))
            if path.startswith("/static/"):
                f = (WEB / "static" / Path(path).name)
                if not f.is_file():
                    return self._send(404, "not found")
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", STATIC_TYPES.get(f.suffix, "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return self.wfile.write(data)
            if path.startswith("/summary/"):
                cid = self._case()
                if not cid:
                    return self._send(404, "unknown case")
                c = app.cases[cid]
                return self._send(200, app.page("summary.html", nav="summary", case_id=cid, visit_date=c.visit.get("date", ""),
                                                plan={i["item_id"]: i["text"] for i in c.plan}, rec=app.results.get(cid),
                                                summary=app.summaries.get(cid), language=c.patient.get("language", "en")))
            if path.startswith("/attack/") and not path.endswith("/run"):
                cid = self._case()
                if not cid:
                    return self._send(404, "unknown case")
                from .attackstage import DEFAULT_PAGE
                st = app.attack()
                page = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("page", DEFAULT_PAGE)
                pg = st.mirror.attacks.get(page) or st.mirror.attacks[DEFAULT_PAGE]
                return self._send(200, app.page("attack.html", nav="attack", case_id=cid, page=pg, page_html=st.mirror.page_html(pg),
                                                pages=st.pages(), title=app.cases[cid].data.get("title", cid)))
            if path.startswith("/attack/") and path.endswith("/run"):
                parts = path.strip("/").split("/")
                cid = parts[1] if len(parts) == 3 and parts[1] in app.cases else None
                if not cid:
                    return self._send(404, "unknown case")
                qd = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                from .attackstage import DEFAULT_PAGE
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                events: list = []
                t0 = time.time()

                def raw(ev):
                    self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                    self.wfile.flush()

                def push(ev):
                    events.append([round(time.time() - t0, 2), ev])
                    raw(ev)
                design, page = qd.get("design", "A"), qd.get("page", DEFAULT_PAGE)
                name = f"attack_{cid}_{design}_{page}"
                try:
                    if qd.get("replay"):
                        if replay_recording(name, raw) is None:
                            raw({"type": "end", "error": "no recorded run for this page", "leaked": None})
                        return
                    out = app.attack().run(app.cases[cid], design, page, lambda role: app.client, push)
                    if qd.get("record") and not out.get("error") and out.get("leaked") == (design == "A"):
                        save_recording(name, events, profile=app.client.p.name)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as ex:
                    push({"type": "end", "error": str(ex), "leaked": None})
                return
            if path == "/redteam/live":            # beat 3 counter: partial results of the task array as they land
                import glob as _glob
                pat = os.environ.get("CAREPLAN_REDTEAM_GLOB", str(ROOT / "runs" / "*_live_leaktest_s*of*" / "results" / "*.json"))
                counts = {"A": [0, 0], "B": [0, 0]}
                for f in _glob.glob(pat):
                    try:
                        r = json.loads(Path(f).read_text())
                    except Exception:
                        continue
                    d = r.get("design")
                    # same rule as leaktest.run's summary: a leak is a network event, so it counts even if the
                    # agent later failed to finish; only runs that died on an exception without leaking are skipped
                    if d in counts and (r.get("leaked") or not str(r.get("error") or "").startswith("exception")):
                        counts[d][1] += 1
                        counts[d][0] += bool(r.get("leaked"))
                return self._send(200, json.dumps({"single_agent": {"leaked": counts["A"][0], "runs": counts["A"][1]},
                                                   "split": {"leaked": counts["B"][0], "runs": counts["B"][1]}}), "application/json")
            if path == "/demo" or path.startswith("/demo/"):        # the demo front end (careplan/web/demo): plain static files
                rel = path[len("/demo"):].lstrip("/") or "index.html"
                if path == "/demo":
                    self.send_response(302); self.send_header("Location", "/demo/"); self.end_headers(); return
                root = (WEB / "demo").resolve()
                f = (root / rel).resolve()
                if root not in f.parents or not f.is_file():
                    return self._send(404, "not found")
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", STATIC_TYPES.get(f.suffix, "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return self.wfile.write(data)
            if path.startswith("/api/demo/"):                       # one JSON document per case; ?live=1 runs the model now
                cid = self._case()
                if not cid:
                    return self._send(404, '{"error": "unknown case"}', "application/json")
                from . import demo
                if "live=1" in query:
                    rec = app.check(cid)
                    doc = demo.build(app.cases[cid], rec, None, app.tracts, app.resources, "live")
                else:
                    doc = demo.from_recording(app.cases[cid], app.tracts, app.resources)
                if doc is None:
                    return self._send(404, '{"error": "no saved run for this case"}', "application/json")
                return self._send(200, json.dumps(doc, ensure_ascii=False), "application/json")
            if path == "/numbers":
                return self._send(200, app.page("numbers.html", nav="numbers"))
            if path == "/fuzzball":
                return self._send(200, app.page("fuzzball.html", nav="fuzzball", real=os.environ.get("CAREPLAN_FUZZBALL_REAL") == "1"))
            if path == "/fuzzball/refusal/run":
                qd = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                events: list = []
                t0 = time.time()

                def raw(ev):
                    self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                    self.wfile.flush()

                def push(ev):
                    events.append([round(time.time() - t0, 2), ev])
                    raw(ev)
                try:
                    if qd.get("replay"):
                        if replay_recording("refusal", raw) is None:
                            raw({"type": "end", "error": "no recorded run"})
                        return
                    from .refusal import run as refusal_run
                    out = refusal_run(app.client.p.name, os.environ.get("CAREPLAN_FUZZBALL_REAL") == "1", emit=push, client=app.client)
                    if qd.get("record") and out.get("attempted") and not out["decision"].get("accepted"):
                        save_recording("refusal", events, profile=app.client.p.name)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as ex:
                    raw({"type": "end", "error": str(ex)})
                return
            if path == "/audit":
                return self._send(200, app.page("audit.html", nav="audit", crossings=app.crossings(), feedback=app.feedback_rows()))
            if path.startswith("/live/") or path.startswith("/stage/"):
                cid = self._case()
                if not cid:
                    return self._send(404, "unknown case")
                c = app.cases[cid]
                return self._send(200, app.page("stage.html", nav="stage", case_id=cid, title=c.data.get("title", cid), plan=c.plan))
            if path.startswith("/stream/"):
                cid = self._case()
                if not cid:
                    return self._send(404, "unknown case")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                t0 = time.time()
                events: list = []

                def raw(ev: dict) -> None:
                    self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                    self.wfile.flush()

                def push(ev: dict) -> None:
                    ev = {**ev, "t": round(time.time() - t0, 1)}
                    if ev["type"] == "tool":                   # never echo patient fields to the page log
                        ev["args"] = {k: v for k, v in ev["args"].items() if k in ("tract_id", "need", "days", "start", "end", "dept", "appt_ref")}
                    events.append([round(time.time() - t0, 2), ev])
                    raw(ev)
                q = dict(p.split("=", 1) for p in (self.path.split("?") + [""])[1].split("&") if "=" in p)
                live = q.get("live") or False
                try:
                    if q.get("replay"):
                        data = replay_recording(f"stream_{cid}", raw)
                        if data is None:
                            raw({"type": "end", "ok": False, "error": "no recorded run for this case", "t": 0})
                        elif data.get("rec"):
                            with app.lock:
                                app.results[cid] = data["rec"]
                        return
                    rec = app.check(cid, on_event=push, live=live)
                    push({"type": "end", "ok": rec["assessment"] is not None, "error": rec["error"], "card_url": f"../card/{cid}"})
                    if q.get("record") and rec["assessment"] is not None:
                        save_recording(f"stream_{cid}", events, rec, app.client.p.name)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if path.startswith("/card/"):
                cid = self._case()
                if not cid:
                    return self._send(404, "unknown case")
                rec = app.results.get(cid) or app.check(cid)
                return self._send(200, render(app.cases[cid], rec, app.resources, app.tracts))
            return self._send(404, "not found")

        def do_POST(self):
            path, _, query = self.path.partition("?")
            if path.startswith("/api/app/"):
                from . import webapp
                return webapp.handle(app, self, "POST", path, query)
            if path.startswith("/visit/"):
                # /visit/<case>/turn?speaker=doctor  body: 16 kHz mono s16le PCM   |   /visit/<case>/reset
                parts = path.strip("/").split("/")
                cid = parts[1] if len(parts) == 3 and parts[1] in app.cases else None
                if not cid:
                    return self._send(404, '{"error": "unknown case"}', "application/json")
                if parts[2] == "reset":
                    app.reset_visit(cid)
                    return self._send(200, '{"ok": true}', "application/json")
                if parts[2] == "clip":          # feed a recorded clip through the same ASR path (fallback for a dead mic)
                    name = Path(dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("name", "")).name
                    wav = ROOT / "data" / "audio" / cid / f"{name}.wav"
                    if not wav.is_file():
                        return self._send(404, '{"error": "no such clip"}', "application/json")
                    import wave
                    with wave.open(str(wav), "rb") as w:
                        pcm = w.readframes(w.getnframes())
                    speaker = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("speaker", "patient")
                    return self._send(200, json.dumps(app.add_turn(cid, speaker, pcm)), "application/json")
                if parts[2] == "turn":
                    speaker = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("speaker", "patient")
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 16000 * 2 * 120:
                        return self._send(413, '{"error": "clip over 2 minutes"}', "application/json")
                    try:
                        turn = app.add_turn(cid, speaker, self.rfile.read(n))
                    except ValueError as ex:
                        return self._send(400, json.dumps({"error": str(ex)}), "application/json")
                    return self._send(200, json.dumps(turn), "application/json")
                return self._send(404, "not found")
            if path.startswith("/summarize/"):
                cid = self._case()
                if not cid:
                    return self._send(404, '{"error": "unknown case"}', "application/json")
                try:
                    return self._send(200, json.dumps(app.summarize(cid), ensure_ascii=False), "application/json; charset=utf-8")
                except (ValueError, RuntimeError) as ex:
                    return self._send(502, json.dumps({"error": str(ex)}), "application/json")
            if path.startswith("/feedback/"):
                cid = self._case()
                if not cid:
                    return self._send(404, '{"error": "unknown case"}', "application/json")
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                    return self._send(200, json.dumps(app.feedback(cid, body)), "application/json")
                except (ValueError, json.JSONDecodeError) as ex:
                    return self._send(400, json.dumps({"error": str(ex)}), "application/json")
            if not path.startswith("/check/"):
                return self._send(404, "not found")
            cid = self._case()
            if not cid:
                return self._send(404, '{"error": "unknown case"}', "application/json")
            qd = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            return self._send(200, json.dumps(app.check(cid, live=qd.get("live") or False), default=str), "application/json")

    return ThreadingHTTPServer((host, port), H)


e = html.escape


def main() -> int:
    srv = make_server(App(), port=int(os.environ.get("CAREPLAN_PORT", "8080")))
    print(f"card backend listening on :{srv.server_address[1]}", file=sys.stderr)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
