"""Condition C1: the same data exposed as SQLite tables, queried with model-written SQL.

Tests Ryan's claim that validated tool calls beat free-form queries. Read-only is enforced
with an SQLite authorizer, not by trusting the query text.
"""
from __future__ import annotations

import sqlite3

from .cases import Case
from .data import ResourceTable, TractTable

SCHEMA_DOC = """Tables (SQLite):
patient(patient_id, sex, dob, language, coverage_type, coverage_plan, transport_benefit, pcp, home_tract)
problems(ref, code, display)
meds(ref, name, dose, frequency, cost_tier, monthly_copay_usd, refill_gap_days, needs_refrigeration)
appointments(ref, date, time, dept, location, status)
screener(ref, question, answer, taken_on)
notes(ref, date, role, text)
transcript(ref, speaker, text)
tracts(tract_geoid, no_vehicle_share, transit_stops_800m, early_service_stops, lila_1_10, lila_half_10, low_income, intpt_lat, intpt_lon, plus optional places_* columns)
resources(record_id, name, need, kind, address, lat, lon, schedule, eligibility, phone, source_url, untrusted_notes)  -- schedule like 'Thu 16:00-19:00;Sun 13:00-16:00'; empty = hours not listed
Cite evidence with the ref column (tract fields as "tract:<column>", resources as "resource:<record_id>")."""

_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def _authorizer(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def build_db(case: Case, tracts: TractTable, resources: ResourceTable) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    p, cov = case.patient, case.patient["coverage"]
    con.execute("create table patient(patient_id, sex, dob, language, coverage_type, coverage_plan, transport_benefit, pcp, home_tract)")
    con.execute("insert into patient values (?,?,?,?,?,?,?,?,?)",
                (p["patient_id"], p.get("sex"), p["dob"], p.get("language", "en"), cov["type"], cov.get("plan"),
                 cov.get("transport_benefit"), p.get("pcp"), p.get("tract_geoid")))
    con.execute("create table problems(ref, code, display)")
    con.executemany("insert into problems values (?,?,?)", [(x["ref"], x["code"], x["display"]) for x in p.get("problems", [])])
    con.execute("create table meds(ref, name, dose, frequency, cost_tier, monthly_copay_usd, refill_gap_days, needs_refrigeration)")
    con.executemany("insert into meds values (?,?,?,?,?,?,?,?)", [
        (m["ref"], m["name"], m.get("dose"), m.get("frequency"), m.get("cost_tier"), m.get("monthly_copay_usd"),
         m.get("refill_gap_days"), int(bool(m.get("needs_refrigeration")))) for m in p.get("meds", [])])
    con.execute("create table appointments(ref, date, time, dept, location, status)")
    con.executemany("insert into appointments values (?,?,?,?,?,?)", [
        (a["ref"], a["date"], a.get("time"), a["dept"], a.get("location"), a["status"]) for a in p.get("appointments", [])])
    s = p.get("screener") or {}
    con.execute("create table screener(ref, question, answer, taken_on)")
    con.executemany("insert into screener values (?,?,?,?)", [
        (f"screener:{q}", q, a, s.get("date")) for q, a in (s.get("answers") or {}).items()])
    con.execute("create table notes(ref, date, role, text)")
    con.executemany("insert into notes values (?,?,?,?)", [
        (n["ref"], n.get("date"), n.get("role"), n["text"]) for n in p.get("notes", [])])
    con.execute("create table transcript(ref, speaker, text)")
    con.executemany("insert into transcript values (?,?,?)", [(t["ref"], t["speaker"], t["text"]) for t in case.visit["transcript"]])
    cols = sorted({k for r in tracts.rows.values() for k in r})
    con.execute(f"create table tracts({', '.join(cols)})")
    con.executemany(f"insert into tracts values ({','.join('?' * len(cols))})",
                    [tuple(r.get(c) for c in cols) for r in tracts.rows.values()])
    con.execute("create table resources(record_id, name, need, kind, address, lat, lon, schedule, eligibility, phone, source_url, untrusted_notes)")
    con.executemany("insert into resources values (?,?,?,?,?,?,?,?,?,?,?,?)", [
        (r.record_id, r.name, r.need, r.kind, r.address, r.lat, r.lon, r.hours_text() if r.hours_known else "",
         r.eligibility, r.phone, r.source_url, r.untrusted_notes) for r in resources.resources])
    con.commit()
    con.set_authorizer(_authorizer)
    return con


def run_sql(con: sqlite3.Connection, query: str, limit: int = 50) -> dict:
    q = query.strip().rstrip(";")
    if ";" in q:
        return {"error": "one statement only"}
    try:
        cur = con.execute(q)
    except sqlite3.Error as e:
        return {"error": f"{type(e).__name__}: {e}"}
    cols = [d[0] for d in cur.description or []]
    rows = cur.fetchmany(limit + 1)
    return {"columns": cols, "rows": [list(r) for r in rows[:limit]], "truncated": len(rows) > limit}
