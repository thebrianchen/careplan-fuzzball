"""The clinic app's backend (pages in careplan/web/app, served at /app/): the doctor's workspace and the operator's
system page. JSON routes and one event stream, all riding on the App in careplan.serve.

GET  /app/                    the pages (plain static files, no build step)
GET  /api/app/cases           the day's visits: who, what setting, whether a review exists
GET  /api/app/case/<id>       one visit: patient, chart, visit, plan, and the review if there is one
GET  /api/app/review/<id>     event stream of a review. ?mode=recorded replays a saved real run (&speed=N); ?mode=live runs the planner now;
                              ?mode=sample plays a stand-in built from the answer key, only when the server runs with CAREPLAN_SAMPLE=1
                              (&record=1 keeps a finished live run as this visit's saved run, if it has none; a saved run is never overwritten)
POST /api/app/decision/<id>   {item_id, decision: accept|edit|dismiss, note}: the clinician's call on one flagged item
POST /api/app/reset/<id>      forget this visit's review and its decisions on screen (the decision log keeps its rows)
GET  /api/app/progress/<id>   ?rid=R&after=N: the events of review R after the Nth, for a page behind a proxy that holds an event
                              stream back until it ends (a Cloudflare quick tunnel does)
GET  /api/app/system          the operator's view: planner, reviews, crossings, decisions, activity. Case ids only, no patient fields.
GET  /api/app/panel           the clinic manager's view: counts by neighborhood from the reviews of a whole panel (careplan.aggregates).
                              Counts only; neighborhoods with fewer than k patients are not in it.
GET  /api/app/basemap         the clinic page's map: tract outlines, streets, rail and water around the clinic, and `wide`, the city
                              around that in less detail (pipeline/basemap.py). Public data.
GET  /api/app/city            the backdrop of the visit page's maps: tract centers and transit stops. Public data, no patient fields.

A review is shown the same way whether it is live or a saved run: start, step*, item*, end (with the whole document).
The document is careplan.demo's shape plus `chart`, `decisions` and `handout`.
"""
from __future__ import annotations

import collections
import copy
import functools
import ipaddress
import json
import os
import statistics
import time
import types

import yaml
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import ROOT, demo, sample, transit_tool
from .card import FIELD_LABEL, _evidence
from .planner import PROGRAMS_PATH
from .scoring import RULES, _cmp
from .data import haversine_km
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


def _shown(key: str, value: float) -> str:
    return f"{value:.0%}" if key == "no_vehicle_share" else f"{value:g}"


def county(tracts) -> dict[str, dict]:
    """Where the county's tracts stand on each neighborhood figure, so one tract's value can be drawn against them."""
    out = {}
    for key in FIELD_LABEL:
        if key.startswith("lila_"):                          # yes/no flags have no scale
            continue
        values = sorted(v for r in tracts.rows.values() if isinstance(v := r.get(key), (int, float)))
        if len(values) >= 4:
            _, median, p75 = statistics.quantiles(values, n=4)
            out[key] = {"median": median, "p75": p75, "max": values[-1], "median_text": _shown(key, median)}
    return out


def _place(appt: dict, tract: dict | None) -> dict | None:
    """Where an appointment happens and how far it is from the home tract. `trip` is the last transit trip that arrives in
    time, with the stops its rides pass, or None when the schedule cache is not on this machine."""
    fac = transit_tool._facilities().get(appt.get("facility_ccn") or "")
    if not (fac and tract):
        return None
    lat, lon = float(fac["lat"]), float(fac["lon"])
    home = (tract["intpt_lat"], tract["intpt_lon"], appt["facility_ccn"], appt.get("time"))
    trip = transit_tool.trip(*home) if appt.get("time") else {"error": "no time"}
    return {"name": fac["name"], "address": fac["address"], "lat": lat, "lon": lon,
            "miles_from_home": round(haversine_km(tract["intpt_lat"], tract["intpt_lon"], lat, lon) * 0.621371, 1),
            "trip": None if "error" in trip else {"feasible": trip["feasible"], "leave_by": trip["latest_departure"], "arrive": trip["arrival"],
                                                  "minutes": trip["trip_minutes"], "assumes": trip["assumes"],
                                                  "rides": transit_tool.rides(*home)}}


NO_CASE = types.SimpleNamespace(refs={})          # a tract figure reads the same for every patient


def hood_facts(tract: dict | None, county: dict[str, dict]) -> list[dict]:
    """A neighborhood's public figures, each with where the county stands on it."""
    facts = []
    for key, label in FIELD_LABEL.items():
        if tract and key in tract:
            value, _, note = _evidence(NO_CASE, f"tract:{key}", tract)[1].partition(": ")[2].partition(" (")
            if key.startswith("lila_"):
                value = "yes" if tract[key] else "no"
            facts.append({"ref": f"tract:{key}", "label": label, "value": value, "note": note.rstrip(")"),
                          "number": tract[key] if key in county else None, "county": county.get(key)})
    return facts


PANEL = {"reviews": ROOT / "reports" / "panel_aggregates.json", "sample": ROOT / "reports" / "panel_aggregates_sample.json"}
PANEL_COUNTS = ("patients", "reviewed", "flagged_patients", "flagged_items", "appointments", "no_shows", "barriers")
BASEMAP = ROOT / "data" / "processed" / "basemap_westside.json"
BASEMAP_WIDE = ROOT / "data" / "processed" / "basemap_chicago.json"            # the city around it, coarser, for zooming out
PANEL_CLINIC = "17031260300"                      # scripts/panel.py draws the panel from the tracts nearest this one
HELP_KM = 1.6                                     # "near" a neighborhood: within a mile of its center
PANEL_EDGE = 0.012                                # degrees beyond the outermost neighborhood centers that the map still shows


@functools.cache
def _areas() -> list[dict]:
    """Chicago's community areas (the neighborhoods people name), from the city-wide basemap; empty without that file."""
    return json.loads(BASEMAP_WIDE.read_text()).get("areas", []) if BASEMAP_WIDE.is_file() else []


def area_of(lat: float, lon: float) -> str | None:
    """The neighborhood a point is in. The outlines are simplified, so a point that falls in a sliver between two of them
    gets the one whose outline comes nearest."""
    def inside(ring: list) -> bool:
        hit = False
        for (y1, x1), (y2, x2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                hit = not hit
        return hit
    areas = _areas()
    for a in areas:
        if any(inside(r) for r in a["rings"]):
            return a["name"]
    near = min(areas, key=lambda a: min((y - lat) ** 2 + (x - lon) ** 2 for r in a["rings"] for y, x in r), default=None)
    return near and near["name"]


def _nearest_of_each_kind(found: list, each: int = 2) -> list:
    """The nearest places of every kind, nearest first: a pantry is not crowded out by three closer groceries."""
    kept, seen = [], collections.Counter()
    for d, x in sorted(found, key=lambda f: f[0]):
        if seen[x.kind] < each:
            seen[x.kind] += 1
            kept.append((d, x))
    return kept


def panel(tracts, resources, county: dict[str, dict], samples: bool) -> dict | None:
    """The published counts by neighborhood, each joined with its place on the map, its public figures and how many places
    that could help are near. The reviews' file wins; the answer-key stand-in is used only when samples are on."""
    agg = None
    for f in [PANEL["reviews"]] + ([PANEL["sample"]] if samples else []):
        if f.is_file():
            agg = json.loads(f.read_text())
            if all(k in r for r in agg["tracts"] for k in PANEL_COUNTS):
                break
            agg = None                            # counts written by an older careplan.aggregates: not shown
    if agg is None:
        return None
    need_of = {need: barrier for barrier, (need, _) in sample.PLACES.items()}
    helpers = [x for x in resources.resources if x.need in need_of]
    rows, totals = [], collections.Counter()
    for r in agg["tracts"]:
        t = tracts.get(r["tract_geoid"])
        if not t:
            continue
        totals.update(r["barriers"])
        near: dict[str, list] = {barrier: [] for barrier in sample.PLACES}
        for x in helpers:
            d = haversine_km(t["intpt_lat"], t["intpt_lon"], x.lat, x.lon)
            if d <= HELP_KM:
                near[need_of[x.need]].append((d, x))
        rows.append({"tract_id": r["tract_geoid"], "area": area_of(t["intpt_lat"], t["intpt_lon"]),
                     "lat": t["intpt_lat"], "lon": t["intpt_lon"], **{k: r[k] for k in PANEL_COUNTS},
                     "places_near": {barrier: len(found) for barrier, found in near.items()},
                     "kinds_near": {barrier: dict(collections.Counter(x.kind for _, x in found)) for barrier, found in near.items()},
                     "nearest": {barrier: [{"name": x.name, "kind": x.kind, "miles": round(d * 0.621371, 1), "address": x.address,
                                            "hours": x.hours_text(), "phone": x.phone} for d, x in _nearest_of_each_kind(found)]
                                 for barrier, found in near.items()},
                     "public_points_to": sorted({b for key, rule in RULES["tract_fields"].items() if _cmp(t.get(key), rule["rule"]) for b in rule["supports"]}),
                     "facts": hood_facts(t, county)})
    clinic = tracts.get(PANEL_CLINIC)
    lats, lons = [r["lat"] for r in rows], [r["lon"] for r in rows]
    inside = lambda x: min(lats) - PANEL_EDGE <= x.lat <= max(lats) + PANEL_EDGE and min(lons) - PANEL_EDGE <= x.lon <= max(lons) + PANEL_EDGE
    places = {barrier: [{"id": x.record_id, "name": x.name, "kind": x.kind, "lat": x.lat, "lon": x.lon, "address": x.address,
                         "hours": x.hours_text(), "phone": x.phone, "eligibility": x.eligibility}
                        for x in resources.resources if x.need == need and inside(x)]
              for barrier, (need, _) in sample.PLACES.items()} if rows else {}
    programs: dict[str, list] = collections.defaultdict(list)
    # A clinic reads these for its whole panel, so the programs that apply to any patient come before the dialysis-only ones.
    for prog in sorted(yaml.safe_load(PROGRAMS_PATH.read_text())["programs"], key=lambda g: "dialysis" in g["eligible_when"]):
        for barrier in prog["needs"]:
            programs[barrier].append({k: prog.get(k) for k in ("id", "category", "eligible_when", "how_to_start", "caveats", "source_url", "confidence")})
    by_area: dict[str, dict] = {}
    for r in rows:
        if r["area"]:
            a = by_area.setdefault(r["area"], {"name": r["area"], "tracts": 0, "patients": 0, "reviewed": 0, "flagged_patients": 0, "barriers": collections.Counter()})
            a["tracts"] += 1
            for k in ("patients", "reviewed", "flagged_patients"):
                a[k] += r[k]
            a["barriers"].update(r["barriers"])
    return {"places": places, "programs": programs,
            "areas": sorted(({**a, "barriers": dict(a["barriers"])} for a in by_area.values()), key=lambda a: -a["patients"]),
            "public_kinds": sorted({b for rule in RULES["tract_fields"].values() for b in rule["supports"]}),
            "public_refs": {b: [f"tract:{key}" for key, rule in RULES["tract_fields"].items() if b in rule["supports"] and key in FIELD_LABEL]
                            for b in sorted({b for rule in RULES["tract_fields"].values() for b in rule["supports"]})},
            "source": "sample" if agg.get("source") == "answer_key" else "reviews", "built": agg["built"], "k": agg["k"],
            "patients": agg["patients"], "reviewed": agg["reviewed"], "hidden_tracts": agg["tracts_suppressed_under_k"],
            "clinic": {"tract_id": PANEL_CLINIC, "lat": clinic["intpt_lat"], "lon": clinic["intpt_lon"]} if clinic else None,
            "barriers": [{"barrier": b, "label": b.replace("_", " "), "flags": n, "places": b in sample.PLACES} for b, n in totals.most_common()],
            "tracts": rows}


def chart(case, tract: dict | None, county: dict[str, dict]) -> dict:
    """What the doctor already has on the private side, each row carrying the ref the review cites."""
    p = case.patient
    scr = p.get("screener") or {}
    hood = hood_facts(tract, county)
    return {
        "home": {"tract_id": case.tract_geoid, "lat": tract["intpt_lat"], "lon": tract["intpt_lon"]} if tract else None,
        "problems": [{"ref": x["ref"], "display": x["display"]} for x in p.get("problems", [])],
        "meds": [{k: m.get(k) for k in ("ref", "name", "dose", "frequency", "monthly_copay_usd", "refill_gap_days")} for m in p.get("meds", [])],
        "appointments": [{**{k: a.get(k) for k in ("ref", "date", "time", "dept", "location", "status")}, "place": _place(a, tract)}
                         for a in p.get("appointments", [])],
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
        self._probe: tuple[float, int, list] = (0.0, 0, [])   # when the endpoints were last probed, for which profile, and what was passed over
        self._own_url: dict[int, str] = {}
        self.samples = os.environ.get("CAREPLAN_SAMPLE") == "1"       # stand-in reviews for page work; off unless asked for
        self.labels = [u.strip() for u in os.environ.get("CAREPLAN_PLANNER_LABELS", "").split(",") if u.strip()]   # names for the endpoints, in order
        self.stall_s = float(os.environ.get("CAREPLAN_STALL_S", "30"))    # a live review that hears nothing from the planner for this long has failed
        self.fallbacks = [u.strip() for u in os.environ.get("CAREPLAN_PLANNER_FALLBACKS", "").split(",") if u.strip()]
        self.county = county(app.tracts)
        self.progress: collections.OrderedDict[str, list] = collections.OrderedDict()   # review id -> its events so far, for pages that poll

    @functools.cached_property
    def city(self) -> dict:
        """The backdrop of the visit page's maps: every tract's center and, when the schedule cache is on this machine, every
        transit stop (they trace the street grid). Public tables only."""
        def points(lats, lons) -> dict:
            return {"lat": [round(float(x), 4) for x in lats], "lon": [round(float(x), 4) for x in lons]}
        rows = [r for r in self.app.tracts.rows.values() if isinstance(r.get("intpt_lat"), float)]
        router = transit_tool._router()
        return {"tracts": points([r["intpt_lat"] for r in rows], [r["intpt_lon"] for r in rows]),
                "stops": points(router.lat, router.lon) if router else None}

    def note(self, event: str, case_id: str | None = None, **detail) -> None:
        self.activity.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "case_id": case_id, **detail})

    def recording(self, cid: str) -> Path:
        from .serve import REC_DIR
        return REC_DIR / f"stream_{cid}.json"

    def _unusable(self, base_url: str) -> str | None:
        """Why this endpoint cannot be asked for a review, or None when it answers. Reachability only: a serving engine can
        answer the probe and still not generate."""
        p, key = self.app.client.p, getattr(self.app.client, "key", "")
        url = urlparse(base_url)
        if _hosted(url.hostname or "") and not key:   # a hosted /models listing answers without a key, so a probe would say "answering"
            env = getattr(p, "api_key_env", "")
            return f"no API key is set ({env})" if env else "no API key is set"
        try:
            import requests
            ok = requests.get(base_url.rstrip("/") + "/models", timeout=2.5, headers={"Authorization": f"Bearer {key}"} if key else {}).ok
        except Exception:
            ok = False
        return None if ok else f"{url.netloc or base_url or 'none'} did not answer"

    def planner(self, fresh: bool = False) -> dict:
        """Which planner the next review uses. The endpoints are tried in order: the profile's own (PLANNER_URL on the cluster),
        then CAREPLAN_PLANNER_FALLBACKS. The first that answers is used, and `skipped` says why each earlier one was passed
        over. When none answers, `why` says so in words that follow "The planner is not answering: "."""
        p = self.app.client.p
        own = self._own_url.setdefault(id(p), p.base_url)    # kept because base_url is repointed at whichever endpoint answers
        at, whose, skipped = self._probe
        if fresh or whose != id(p) or time.time() - at > 15:
            skipped, p.base_url = [], own
            for base_url in (own, *self.fallbacks):
                why = self._unusable(base_url)
                if why is None:
                    p.base_url = base_url
                    break
                skipped.append(why)
            else:
                skipped = [None, *skipped]                     # nothing answered: no endpoint was chosen
            self._probe = (time.time(), id(p), skipped)
        answers = not skipped or skipped[0] is not None
        url = urlparse(p.base_url)
        host, nth = url.netloc or p.base_url or "none", len(skipped) if answers else 0
        return {"profile": p.name, "model": p.model, "host": host, "hosted": _hosted(url.hostname or ""),
                "label": self.labels[nth] if answers and nth < len(self.labels) else host, "fallback": answers and nth > 0,
                "answers": answers, "why": None if answers else "; ".join(skipped[1:]), "skipped": skipped if answers else []}

    # ---- the doctor's side
    def tucked(self, cid: str) -> list[str]:
        """The flagged plan items that raise no barrier an earlier flagged item has not raised. Every page leaves them out,
        so the counts on the visits list, the visit and the handout agree. Known from the review shown, or the saved run."""
        s, f = self.shown.get(cid), self.recording(cid)
        items = s["doc"]["items"] if s else (json.loads(f.read_text())["rec"].get("assessment") or {}).get("items", []) if f.is_file() else []
        by, seen, out = {i["item_id"]: i for i in items}, set(), []
        for p in self.app.cases[cid].plan:
            it = by.get(p["item_id"])
            if it and it["status"] != "likely_ok":
                kinds = [b["barrier"] for b in it.get("barriers", [])]
                if kinds and all(k in seen for k in kinds):
                    out.append(it["item_id"])
                seen.update(kinds)
        return out

    def state(self, cid: str) -> dict | None:
        s = self.shown.get(cid)
        if not s:
            return None
        away = self.tucked(cid)
        flagged = [i["item_id"] for i in s["doc"]["items"] if i["status"] != "likely_ok" and i["item_id"] not in away]
        decided = (self.app.results.get(cid) or {}).get("decisions") or {}
        return {"source": s["source"], "seconds": s["doc"]["meta"].get("seconds"), "items": len(s["doc"]["items"]) - len(away),
                "flagged": len(flagged), "decided": sum(i in decided for i in flagged)}

    def cases(self) -> dict:
        out = []
        for cid, c in sorted(self.app.cases.items()):
            p, v = c.patient, c.visit
            out.append({"case_id": cid, "name": p.get("name"), "age": _age(p, v), "sex": p.get("sex"),
                        "language": LANGUAGE.get((p.get("language") or "en").lower(), p.get("language")),
                        "setting": v.get("setting"), "date": v.get("date"), "problems": [x.get("display") for x in p.get("problems", [])][:2],
                        "plan_items": len(c.plan) - len(self.tucked(cid)), "has_recording": self.recording(cid).is_file(), "review": self.state(cid)})
        return {"cases": out, "planner": self.planner(), "samples": self.samples}

    def case(self, cid: str) -> dict:
        app, case = self.app, self.app.cases[cid]
        s = self.shown.get(cid)
        doc = dict(s["doc"]) if s else demo.build(case, {}, None, app.tracts, app.resources, "none")
        code = (case.patient.get("language") or "en").lower()
        doc["patient"] = {**doc["patient"], "language": LANGUAGE.get(code, code), "handout_language": LANG.get(code, "English"),
                          "pcp": case.patient.get("pcp")}
        doc["chart"] = chart(case, app.tracts.get(case.tract_geoid), self.county)
        doc["decisions"] = dict((app.results.get(cid) or {}).get("decisions") or {}) if s else {}
        doc["handout"] = app.summaries.get(cid) if s else None
        doc["has_recording"] = self.recording(cid).is_file()
        doc["tucked"] = self.tucked(cid)
        doc["can_sample"] = self.samples
        doc["planner"] = self.planner()
        return doc

    def prefill(self) -> None:
        """With samples on, the day starts with most visits already reviewed: every visit without a saved run gets its sample
        review. Visits with a saved run stay unreviewed, to be played. These do not count as reviews on the System page."""
        app = self.app
        for cid, case in app.cases.items():
            if cid in self.shown or self.recording(cid).is_file():
                continue
            data = sample.run(case, app.tracts.get(case.tract_geoid), app.resources)
            doc = demo.build(case, data["rec"], data["events"], app.tracts, app.resources, "sample")
            self.install(cid, data["rec"], doc, "sample", log=False)

    def install(self, cid: str, rec: dict, doc: dict, source: str, log: bool = True) -> None:
        with self.app.lock:
            self.app.results[cid] = rec
            self.app.summaries.pop(cid, None)
            self.shown[cid] = {"doc": doc, "assessment": copy.deepcopy(rec["assessment"]), "source": source}
        if not log:
            return
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

    def review(self, h, cid: str, mode: str, speed: float, record: bool = False, rid: str = "") -> None:
        app, case = self.app, self.app.cases[cid]
        tract = app.tracts.get(case.tract_geoid)
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.end_headers()
        steps: list[dict] = []
        log = self.progress[rid] = []
        while len(self.progress) > 40:
            self.progress.popitem(last=False)

        def send(ev: dict) -> None:
            ev = {**ev, "i": len(log)}               # numbered, so a page that hears an event twice (stream and poll) applies it once
            log.append(ev)
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
            if mode in ("recorded", "sample"):
                f = self.recording(cid)
                if mode == "sample" and not self.samples:
                    return send({"type": "end", "ok": False, "error": "Sample reviews are off on this server."})
                if mode == "recorded" and not f.is_file():
                    return send({"type": "end", "ok": False, "error": "There is no saved run for this visit."})
                data = sample.run(case, tract, app.resources) if mode == "sample" else json.loads(f.read_text())
                rec, speed = data["rec"], 1.0 if mode == "sample" else speed
                send({"type": "start", "source": mode, "saved": data.get("saved"), "where": data.get("where"), "profile": data.get("profile"),
                      "model": demo.model_name(data.get("profile")), "seconds": rec.get("wall_s"), "speed": speed})
                t0 = time.time()
                for off, ev in data["events"]:
                    delay = off / speed - (time.time() - t0)
                    if delay > 0:
                        time.sleep(delay)
                    relay(off, ev)
                doc = demo.build(case, rec, data["events"], app.tracts, app.resources, mode, data.get("saved"), data.get("where"))
            else:
                pl = self.planner(fresh=True)
                if not pl["answers"]:
                    self.fail(cid, "live", f"planner not answering: {pl['why']}")
                    return send({"type": "end", "ok": False, "error": f"The planner is not answering: {pl['why']}."})
                send({"type": "start", "source": "live", "where": pl["label"], "profile": pl["profile"], "model": demo.model_name(pl["profile"])})
                t0, events = time.time(), []

                def push(ev: dict) -> None:
                    t = round(time.time() - t0, 2)
                    if ev.get("type") == "tool":
                        ev = {**ev, "args": {k: v for k, v in (ev.get("args") or {}).items() if k in SHOWN_ARGS}}
                    events.append([t, ev])
                    relay(t, ev)
                # A hung engine accepts the request and then sends nothing. The planner streams, so a read timeout between
                # bytes catches that; one attempt only, because a retry on stage is a silence of minutes.
                client = app.client
                patience = getattr(client, "timeout", None), getattr(client, "retries", None)
                client.timeout, client.retries = (10, self.stall_s), 1
                try:
                    rec = app.check(cid, on_event=push)
                except (BrokenPipeError, ConnectionResetError):
                    raise
                except Exception as ex:
                    self.fail(cid, "live", f"{type(ex).__name__}: {ex}")
                    stalled = "timed out" in str(ex).lower()
                    return send({"type": "end", "ok": False, "error": f"The planner on {pl['label']} sent nothing for {self.stall_s:g} seconds."
                                 if stalled else f"The planner call failed ({type(ex).__name__})."})
                finally:
                    client.timeout, client.retries = patience
                if not rec.get("assessment"):
                    self.fail(cid, "live", rec.get("error") or "no finished review")
                    return send({"type": "end", "ok": False, "error": "The planner did not finish a review: " + (rec.get("error") or "no result") + "."})
                doc = demo.build(case, rec, events, app.tracts, app.resources, "live", where=pl["label"])
                if record and not self.recording(cid).is_file():
                    from .serve import save_recording
                    save_recording(f"stream_{cid}", events, rec, app.client.p.name, where=pl["label"])
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
            new = not hasattr(app, "clinic")
            if new:
                app.clinic = Clinic(app)
        if new and app.clinic.samples:            # after the lock is released: installing a review takes it
            app.clinic.prefill()
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
    if method == "GET" and route == "city":
        return js(200, c.city)
    if method == "GET" and route == "basemap":
        part = lambda f: f.read_text() if f.is_file() else "null"       # the files are JSON already: no parse and re-dump of megabytes
        return h._send(200, '{"basemap":' + part(BASEMAP) + ',"wide":' + part(BASEMAP_WIDE) + "}", "application/json; charset=utf-8")
    if method == "GET" and route == "panel":
        return js(200, {"panel": panel(app.tracts, app.resources, c.county, c.samples)})
    if method == "GET" and route == "case" and cid:
        return js(200, c.case(cid))
    if method == "GET" and route == "review" and cid:
        try:
            speed = min(1000.0, max(1.0, float(q.get("speed", 1))))
        except ValueError:
            speed = 1.0
        return c.review(h, cid, q.get("mode") if q.get("mode") in ("live", "sample") else "recorded", speed, record=q.get("record") == "1",
                        rid=q.get("rid", ""))
    if method == "GET" and route == "progress" and cid:
        try:
            after = int(q.get("after", -1))
        except ValueError:
            after = -1
        return js(200, {"events": c.progress.get(q.get("rid", ""), [])[after + 1:]})
    if method == "POST" and route == "decision" and cid:
        try:
            body = json.loads(h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"{}")
            return js(200, c.decide(cid, body))
        except (ValueError, json.JSONDecodeError) as ex:
            return js(400, {"error": str(ex)})
    if method == "POST" and route == "reset" and cid:
        return js(200, c.reset(cid))
    return js(404, {"error": "not found"})
