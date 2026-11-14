"""The clinic app's backend (pages in careplan/web/app, served at /app/): the doctor's workspace and the operator's
system page. JSON routes and one event stream, all riding on the App in careplan.serve.

GET  /app/                    the pages (plain static files, no build step)
GET  /api/app/cases           the day's visits: who, what setting, whether a review exists
GET  /api/app/case/<id>       one visit: patient, chart, visit, plan, and the review if there is one
GET  /api/app/review/<id>     event stream of a review. ?mode=recorded replays a saved real run (&speed=N); ?mode=live runs the planner now
                              (&record=1 keeps a finished live run as this visit's saved run, if it has none; a saved run is never overwritten)
POST /api/app/decision/<id>   {item_id, decision: accept|edit|dismiss, note}: the clinician's call on one flagged item
POST /api/app/reset/<id>      forget this visit's review and its decisions on screen (the decision log keeps its rows)
GET  /api/app/system          the operator's view: planner, reviews, crossings, decisions, activity. Case ids only, no patient fields.

A review is shown the same way whether it is live or a saved run: start, step*, item*, end (with the whole document).
The document is careplan.demo's shape plus `chart`, `decisions` and `handout`.
"""
from __future__ import annotations

import collections
import copy
import ipaddress
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import demo
from .card import FIELD_LABEL, _evidence
from .summary import LANG

WEB_APP = Path(__file__).parent / "web" / "app"
LANGUAGE = {**LANG, "yue": "Cantonese"}
DECISIONS = ("accept", "edit", "dismiss")
SHOWN_ARGS = ("tract_id", "need", "days", "start", "end", "dept", "appt_ref")     # tool arguments that may reach a page or a saved run
SCREENER = {"living_situation": "Living situation", "housing_problems": "Problems with the home",          # CMS AHC questions (docs/case-guide.md)
            "food_worry": "Worried food would run out", "food_ran_out": "Food ran out before there was money for more",
            "transportation": "Lack of transport kept them from appointments or necessities", "utilities_shutoff": "Utility shutoff threatened"}


def _age(p: dict, visit: dict) -> int | None:
    if not (p.get("dob") and visit.get("date")):
        return None
    return int(visit["date"][:4]) - int(p["dob"][:4]) - (visit["date"][5:] < p["dob"][5:])


def _hosted(host: str) -> bool:
    """True when the planner is someone else's endpoint on the internet, i.e. the chart leaves our machines to reach it."""
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host and not host.endswith((".local", ".internal", ".svc", ".lan", ".ts.net"))


def chart(case, tract: dict | None) -> dict:
    """What the doctor already has on the private side, each row carrying the ref the review cites."""
    p = case.patient
    scr = p.get("screener") or {}
    hood = []
    for key, label in FIELD_LABEL.items():
        if tract and key in tract:
            value, _, note = _evidence(case, f"tract:{key}", tract)[1].partition(": ")[2].partition(" (")
            if key.startswith("lila_"):
                value = "yes" if tract[key] else "no"
            hood.append({"ref": f"tract:{key}", "label": label, "value": value, "note": note.rstrip(")")})
    return {
        "problems": [{"ref": x["ref"], "display": x["display"]} for x in p.get("problems", [])],
        "meds": [{k: m.get(k) for k in ("ref", "name", "dose", "frequency", "monthly_copay_usd", "refill_gap_days")} for m in p.get("meds", [])],
        "appointments": [{k: a.get(k) for k in ("ref", "date", "time", "dept", "location", "status")} for a in p.get("appointments", [])],
        "screener": {"date": scr.get("date"), "answers": [{"ref": f"screener:{q}", "question": SCREENER.get(q, q.replace("_", " ")), "answer": str(a).replace("_", " ")}
                                                          for q, a in (scr.get("answers") or {}).items()]},
        "notes": [{k: n.get(k) for k in ("ref", "date", "role", "text")} for n in p.get("notes", [])],
        "neighborhood": {"tract_id": case.tract_geoid, "facts": hood, "vintage": (tract or {}).get("data_vintage")},
    }


class Clinic:
    def __init__(self, app):
        self.app = app
        self.started = time.time()
        self.shown: dict[str, dict] = {}                     # case_id -> the review as first shown: doc, assessment, source
        self.runs: collections.deque = collections.deque(maxlen=200)
        self.activity: collections.deque = collections.deque(maxlen=200)
        self._probe: tuple[float, bool | None] = (0.0, None)

    def note(self, event: str, case_id: str | None = None, **detail) -> None:
        self.activity.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "case_id": case_id, **detail})

    def recording(self, cid: str) -> Path:
        from .serve import REC_DIR
        return REC_DIR / f"stream_{cid}.json"

    def planner(self, fresh: bool = False) -> dict:
        """Does the planner's endpoint answer? Reachability only: a serving engine can answer this and still not generate."""
        p = self.app.client.p
        at, ok = self._probe
        if fresh or time.time() - at > 15:
            try:
                import requests
                key = getattr(self.app.client, "key", "")
                ok = requests.get(p.base_url.rstrip("/") + "/models", timeout=2.5,
                                  headers={"Authorization": f"Bearer {key}"} if key else {}).ok
            except Exception:
                ok = False
            self._probe = (time.time(), ok)
        url = urlparse(p.base_url)
        return {"profile": p.name, "model": p.model, "host": url.netloc or p.base_url or "none", "answers": ok,
                "hosted": _hosted(url.hostname or "")}

    # ---- the doctor's side
    def state(self, cid: str) -> dict | None:
        s = self.shown.get(cid)
        if not s:
            return None
        flagged = [i["item_id"] for i in s["doc"]["items"] if i["status"] != "likely_ok"]
        decided = (self.app.results.get(cid) or {}).get("decisions") or {}
        return {"source": s["source"], "seconds": s["doc"]["meta"].get("seconds"), "items": len(s["doc"]["items"]),
                "flagged": len(flagged), "decided": sum(i in decided for i in flagged)}

    def cases(self) -> dict:
        out = []
        for cid, c in sorted(self.app.cases.items()):
            p, v = c.patient, c.visit
            out.append({"case_id": cid, "name": p.get("name"), "age": _age(p, v), "sex": p.get("sex"),
                        "language": LANGUAGE.get((p.get("language") or "en").lower(), p.get("language")),
                        "setting": v.get("setting"), "date": v.get("date"), "problems": [x.get("display") for x in p.get("problems", [])][:2],
                        "plan_items": len(c.plan), "has_recording": self.recording(cid).is_file(), "review": self.state(cid)})
        return {"cases": out, "planner": self.planner()}

    def case(self, cid: str) -> dict:
        app, case = self.app, self.app.cases[cid]
        s = self.shown.get(cid)
        doc = dict(s["doc"]) if s else demo.build(case, {}, None, app.tracts, app.resources, "none")
        code = (case.patient.get("language") or "en").lower()
        doc["patient"] = {**doc["patient"], "language": LANGUAGE.get(code, code), "handout_language": LANG.get(code, "English"),
                          "pcp": case.patient.get("pcp")}
        doc["chart"] = chart(case, app.tracts.get(case.tract_geoid))
        doc["decisions"] = dict((app.results.get(cid) or {}).get("decisions") or {}) if s else {}
        doc["handout"] = app.summaries.get(cid) if s else None
        doc["has_recording"] = self.recording(cid).is_file()
        doc["planner"] = self.planner()
        return doc

    def install(self, cid: str, rec: dict, doc: dict, source: str) -> None:
        with self.app.lock:
            self.app.results[cid] = rec
            self.app.summaries.pop(cid, None)
            self.shown[cid] = {"doc": doc, "assessment": copy.deepcopy(rec["assessment"]), "source": source}
        usage = rec.get("usage") or {}
        self.runs.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "case_id": cid, "source": source, "ok": True,
                          "profile": rec.get("profile"), "seconds": rec.get("wall_s"), "steps": rec.get("steps"),
                          "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                          "tool_calls": len(rec.get("tool_calls") or []), "crossings": len(doc["crossings"]),
                          "items": len(doc["items"]), "flagged": sum(i["status"] != "likely_ok" for i in doc["items"])})
        self.note("review", cid, source=source, seconds=rec.get("wall_s"))

    def fail(self, cid: str, source: str, error: str) -> None:
        self.runs.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "case_id": cid, "source": source, "ok": False,
                          "profile": self.app.client.p.name, "error": error[:200]})
        self.note("review failed", cid, source=source)

    def review(self, h, cid: str, mode: str, speed: float, record: bool = False) -> None:
        app, case = self.app, self.app.cases[cid]
        tract = app.tracts.get(case.tract_geoid)
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.end_headers()
        steps: list[dict] = []

        def send(ev: dict) -> None:
            h.wfile.write(f"data: {json.dumps(ev, default=str, ensure_ascii=False)}\n\n".encode())
            h.wfile.flush()

        def relay(t: float, ev: dict) -> None:
            if ev.get("type") == "tool":
                step = demo.step_for(t, ev)
                if not steps or (steps[-1]["kind"], steps[-1]["label"]) != (step["kind"], step["label"]):
                    steps.append(step)
                    send({"type": "step", "step": step})
            elif ev.get("type") == "item":
                try:
                    send({"type": "item", "item": demo.resolve_item(case, ev["item"], t, tract, app.resources)})
                except (KeyError, TypeError):            # a half-formed item from the stream; the finished card has the real one
                    pass
            elif ev.get("type") == "rejected":           # the card failed its checks and is being rewritten
                send({"type": "rewrite", "t": t})

        try:
            if mode == "recorded":
                f = self.recording(cid)
                if not f.is_file():
                    return send({"type": "end", "ok": False, "error": "There is no saved run for this visit."})
                data = json.loads(f.read_text())
                rec = data["rec"]
                send({"type": "start", "source": "recorded", "saved": data.get("saved"), "model": data.get("profile"),
                      "seconds": rec.get("wall_s"), "speed": speed})
                t0 = time.time()
                for off, ev in data["events"]:
                    delay = off / speed - (time.time() - t0)
                    if delay > 0:
                        time.sleep(delay)
                    relay(off, ev)
                doc = demo.build(case, rec, data["events"], app.tracts, app.resources, "recorded", data.get("saved"))
            else:
                pl = self.planner(fresh=True)
                if not pl["answers"]:
                    self.fail(cid, "live", "planner not answering")
                    return send({"type": "end", "ok": False, "error": f"The planner ({pl['profile']}) is not answering at {pl['host']}."})
                send({"type": "start", "source": "live", "model": pl["profile"]})
                t0, events = time.time(), []

                def push(ev: dict) -> None:
                    t = round(time.time() - t0, 2)
                    if ev.get("type") == "tool":
                        ev = {**ev, "args": {k: v for k, v in (ev.get("args") or {}).items() if k in SHOWN_ARGS}}
                    events.append([t, ev])
                    relay(t, ev)
                try:
                    rec = app.check(cid, on_event=push)
                except (BrokenPipeError, ConnectionResetError):
                    raise
                except Exception as ex:
                    self.fail(cid, "live", f"{type(ex).__name__}: {ex}")
                    return send({"type": "end", "ok": False, "error": f"The planner call failed ({type(ex).__name__})."})
                if not rec.get("assessment"):
                    self.fail(cid, "live", rec.get("error") or "no finished review")
                    return send({"type": "end", "ok": False, "error": "The planner did not finish a review: " + (rec.get("error") or "no result") + "."})
                doc = demo.build(case, rec, events, app.tracts, app.resources, "live")
                if record and not self.recording(cid).is_file():
                    from .serve import save_recording
                    save_recording(f"stream_{cid}", events, rec, app.client.p.name)
                    self.note("run saved", cid)
            self.install(cid, rec, doc, doc["meta"]["source"])
            send({"type": "end", "ok": True, "doc": self.case(cid)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def decide(self, cid: str, body: dict) -> dict:
        s, rec = self.shown.get(cid), self.app.results.get(cid)
        item_id, decision = body.get("item_id"), body.get("decision")
        if not s or not rec:
            raise ValueError("review this visit before deciding")
        if decision not in DECISIONS or (decision == "edit" and not (body.get("note") or "").strip()):
            raise ValueError("decision must be accept, edit (with the new wording) or dismiss")
        first = next((copy.deepcopy(i) for i in s["assessment"]["items"] if i["item_id"] == item_id), None)
        if first is None:
            raise ValueError("unknown plan item")
        with self.app.lock:                       # a changed mind starts again from the finding as first shown
            rec["assessment"]["items"] = [first if i["item_id"] == item_id else i for i in rec["assessment"]["items"]]
            self.app.summaries.pop(cid, None)     # a handout written before this decision no longer matches the plan
        row = self.app.feedback(cid, body)
        self.note("decision", cid, item_id=item_id, decision=decision)
        return {"saved": row["ts"], "decisions": rec.get("decisions") or {}}

    def reset(self, cid: str) -> dict:
        with self.app.lock:
            for d in (self.shown, self.app.results, self.app.summaries):
                d.pop(cid, None)
        self.note("reset", cid)
        return {"ok": True}

    # ---- the operator's side: case ids and counts, never patient fields
    def system(self) -> dict:
        app = self.app
        crossings = []
        for cid, rec in sorted(app.results.items()):
            for tc in rec.get("tool_calls", []):
                if tc["tool"] == "find_resources":
                    a = tc["args"]
                    crossings.append({"case_id": cid, "tract_id": a.get("tract_id"), "need": a.get("need"), "days": a.get("days") or [],
                                      "start": a.get("start"), "end": a.get("end"), "via_gateway": bool(rec.get("crossed_via_gateway")),
                                      "source": (self.shown.get(cid) or {}).get("source")})
        return {"now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "uptime_s": round(time.time() - self.started),
                "planner": self.planner(),
                "config": {"cases": len(app.cases), "gateway": app.gateway is not None, "signals": app.signals, "prefetch": app.prefetch,
                           "final": app.final},
                "runs": list(self.runs)[::-1], "crossings": crossings, "decisions": app.feedback_rows()[::-1][:50],
                "activity": list(self.activity)[::-1][:60]}


def clinic(app) -> Clinic:
    if not hasattr(app, "clinic"):
        with app.lock:
            if not hasattr(app, "clinic"):
                app.clinic = Clinic(app)
    return app.clinic


def handle(app, h, method: str, path: str, query: str) -> None:
    """Called by careplan.serve for /app... and /api/app/...; `h` is the request handler."""
    from .serve import STATIC_TYPES

    def js(code: int, obj) -> None:
        h._send(code, json.dumps(obj, ensure_ascii=False, default=str), "application/json; charset=utf-8")

    if not path.startswith("/api/"):
        if method != "GET":
            return h._send(404, "not found")
        if path == "/app":
            h.send_response(302)
            h.send_header("Location", "/app/")
            h.end_headers()
            return
        root = WEB_APP.resolve()
        f = (root / (path[len("/app/"):] or "index.html")).resolve()
        if root not in f.parents or not f.is_file():
            return h._send(404, "not found")
        data = f.read_bytes()
        h.send_response(200)
        h.send_header("Content-Type", STATIC_TYPES.get(f.suffix, "application/octet-stream"))
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-cache")
        h.end_headers()
        h.wfile.write(data)
        return

    c = clinic(app)
    q = {k: v[0] for k, v in parse_qs(query).items()}
    route, _, cid = path[len("/api/app/"):].strip("/").partition("/")
    if cid and cid not in app.cases:
        return js(404, {"error": "unknown visit"})
    if method == "GET" and route == "cases":
        return js(200, c.cases())
    if method == "GET" and route == "system":
        return js(200, c.system())
    if method == "GET" and route == "case" and cid:
        return js(200, c.case(cid))
    if method == "GET" and route == "review" and cid:
        try:
            speed = min(1000.0, max(1.0, float(q.get("speed", 1))))
        except ValueError:
            speed = 1.0
        return c.review(h, cid, "live" if q.get("mode") == "live" else "recorded", speed, record=q.get("record") == "1")
    if method == "POST" and route == "decision" and cid:
        try:
            body = json.loads(h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"{}")
            return js(200, c.decide(cid, body))
        except (ValueError, json.JSONDecodeError) as ex:
            return js(400, {"error": str(ex)})
    if method == "POST" and route == "reset" and cid:
        return js(200, c.reset(cid))
    return js(404, {"error": "not found"})
