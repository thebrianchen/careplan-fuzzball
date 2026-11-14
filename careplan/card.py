"""Render one planner result as the clinician card (static HTML): plan items with status, the
evidence behind each flag resolved to what it actually says, alternatives with real resource
details, and the boundary panel showing exactly what crossed to the public zone.

    python -m careplan card runs/<stamp>/results/D001_C2_r1.json --out reports/card_D001.html

Demo/backup artifact; the live UI is WP-D1.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from .cases import Case, load_cases
from .data import ResourceTable, TractTable, haversine_km
from .scoring import RULES, _cmp

STATUS = {"blocked": ("Blocked", "#e5534b"), "at_risk": ("At risk", "#e8b931"), "likely_ok": ("Likely OK", "#2fbf71")}
FIELD_LABEL = {"no_vehicle_share": "households with no vehicle", "places_lacktrpt": "adults lacking reliable transportation (%)",
               "places_foodinsecu": "adults with food insecurity (%)", "places_shututility": "adults with utility shutoff threat (%)",
               "places_housinsecu": "adults with housing insecurity (%)", "lila_half_10": "USDA low income + low food access (1/2 mile)",
               "lila_1_10": "USDA low income + low food access (1 mile)", "early_service_stops": "bus/rail stops with service before 6 AM"}
e = html.escape


def _evidence(case: Case, ref: str, tract: dict | None) -> tuple[str, str]:
    kind, _, key = ref.partition(":")
    obj = case.refs.get(ref)
    if kind == "turn" and obj:
        return "Visit", f"{obj['speaker'].title()}: “{obj['text']}”"
    if kind == "note" and obj:
        return "Chart note", f"{obj.get('role', '').replace('_', ' ')} ({obj.get('date', '')}): {obj['text']}"
    if kind == "screener" and obj:
        return "Screener", f"{obj['question'].replace('_', ' ')}: {obj['answer']}"
    if kind == "appt" and obj:
        return "Appointments", f"{obj['date']} {obj.get('time', '')} {obj['dept']}: {obj['status'].replace('_', ' ')}"
    if kind == "med" and obj:
        return "Medications", f"{obj['name']} {obj.get('dose', '')}: ${obj.get('monthly_copay_usd', '?')}/month, tier {obj.get('cost_tier', '?')}, refill gap {obj.get('refill_gap_days', 0)} days"
    if kind == "prob" and obj:
        return "Problem list", obj["display"]
    if kind == "transit" and tract:
        from .transit_tool import trip
        appt = case.refs.get(key) or {}
        if appt.get("facility_ccn") and appt.get("time"):
            r = trip(tract["intpt_lat"], tract["intpt_lon"], appt["facility_ccn"], appt["time"])
            if "error" not in r:
                return "Transit schedules", (f"to {r['facility']} by {r['arrive_by']}: leave by {r['latest_departure']}, "
                                             f"{r['trip_minutes']} min ride" if r["feasible"] else
                                             f"no CTA/Pace trip reaches {r['facility']} by {r['arrive_by']}")
    if kind == "tract" and tract and key in tract:
        rule = RULES["tract_fields"].get(key, {}).get("rule", "")
        val = tract[key]
        shown = f"{val:.0%}" if key == "no_vehicle_share" and isinstance(val, float) else val
        flag = " (above the Cook County 75th percentile)" if rule and _cmp(val, rule) and rule.startswith(">=") else ""
        return "Neighborhood", f"{FIELD_LABEL.get(key, key)}: {shown}{flag}"
    return "Unresolved", f"{ref} (not found in the record)"


def render(case: Case, rec: dict, resources: ResourceTable, tracts: TractTable) -> str:
    tract = tracts.get(case.tract_geoid)
    a = rec.get("assessment") or {"items": []}
    plan = {i["item_id"]: i for i in case.plan}
    rows = []
    for it in a["items"]:
        label, color = STATUS.get(it["status"], (it["status"], "#888"))
        body = []
        for b in it.get("barriers", []):
            ev = "".join(f"<li><span class='src'>{e(s)}</span> {e(t)}</li>" for s, t in (_evidence(case, r, tract) for r in b["evidence"]))
            body.append(f"<div class='barrier'><b>{e(b['barrier'].replace('_', ' '))}</b>: {e(b['reason'])}<ul class='ev'>{ev}</ul></div>")
        alts = []
        for alt in it.get("alternatives", []) or []:
            r = resources.by_id.get(alt.get("resource_record_id") or "")
            extra = ""
            if r:
                dist = f" · {haversine_km(tract['intpt_lat'], tract['intpt_lon'], r.lat, r.lon) * 0.621371:.1f} mi from home tract" if tract else ""
                extra = (f"<div class='res'><b>{e(r.name)}</b> · {e(r.address)}{dist}<br>{e(r.hours_text())}"
                         f"{' · ' + e(r.phone) if r.phone else ''} · <a href='{e(r.source_url)}'>source</a></div>")
            cls = " class='clin'" if alt.get("category") == "clinician_edit" else ""
            alts.append(f"<li{cls}><span class='cat'>{e(alt['category'].replace('_', ' '))}</span> {e(alt['text'])}{extra}</li>")
        dec = (rec.get("decisions") or {}).get(it["item_id"]) or {}
        fb = (f"<div class='fb' data-item='{e(it['item_id'])}'><button data-d='accept' class='{'on' if dec.get('decision') == 'accept' else ''}'>Accept</button>"
              f"<button data-d='edit' class='{'on' if dec.get('decision') == 'edit' else ''}'>Edit…</button>"
              f"<button data-d='dismiss' class='{'on' if dec.get('decision') == 'dismiss' else ''}'>Dismiss</button>"
              f"<span class='fbs muted'>{e(dec['decision'] + 'ed' + (': ' + dec['note'] if dec.get('note') else '')) if dec else ''}</span></div>")
        rows.append(f"""<div class='item' id='item-{e(it['item_id'])}'><div class='head'><span class='pill' style='background:{color}22;color:{color}'>{label}</span>
          <span class='plan'>{e(plan.get(it['item_id'], {}).get('text', it['item_id']))}</span></div>
          {''.join(body)}{'<ul class="alts">' + ''.join(alts) + '</ul>' if alts else ''}{fb}</div>""")
    crossings = [t for t in rec.get("tool_calls", []) if t["tool"] == "find_resources"]
    cross_rows = "".join(f"<tr><td>{e(c['args'].get('tract_id', ''))}</td><td>{e(c['args'].get('need', ''))}</td>"
                         f"<td>{e(','.join(c['args'].get('days') or []) or 'any')}</td>"
                         f"<td>{e((c['args'].get('start') or '') + ('–' + c['args']['end'] if c['args'].get('end') else '')) or 'any'}</td></tr>"
                         for c in crossings)
    local = sorted({t["tool"] for t in rec.get("tool_calls", []) if t["tool"] != "find_resources"})
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>Care Plan Check: {e(case.case_id)}</title>
<style>body{{margin:0;font:15px/1.5 -apple-system,Segoe UI,sans-serif;background:#0f1115;color:#e6e8ee}}main{{max-width:1180px;margin:0 auto;padding:28px;display:grid;grid-template-columns:2fr 1fr;gap:20px}}
h1{{font-size:22px;margin:0 0 4px}}.muted{{color:#9aa3b2}}.item{{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:14px 16px;margin-bottom:12px}}
.head{{display:flex;gap:10px;align-items:baseline}}.pill{{font-size:12px;font-weight:700;padding:2px 9px;border-radius:999px;white-space:nowrap}}.plan{{font-weight:600}}
.barrier{{margin:10px 0 0}}.ev{{margin:6px 0 0;padding-left:18px;color:#c9ced8;font-size:14px}}.src{{display:inline-block;min-width:96px;color:#4c9aff;font-size:12px;font-weight:600}}
.alts{{margin:10px 0 0;padding-left:18px}}.cat{{font-size:12px;color:#2fbf71;font-weight:600;margin-right:6px}}.res{{font-size:13px;color:#c9ced8;margin:4px 0 6px;padding:6px 10px;border-left:3px solid #2fbf71;background:#12161d}}
a{{color:#4c9aff}}aside .box{{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:14px 16px;margin-bottom:12px}}table{{width:100%;border-collapse:collapse;font-size:13px}}td,th{{text-align:left;padding:4px 6px;border-bottom:1px solid #262b36}}th{{color:#9aa3b2;font-size:11px;text-transform:uppercase}}</style></head>
<body><main><section><h1>Care plan check · {e(case.data.get('title', case.case_id))}</h1>
<p class='muted'>{e(case.case_id)} · synthetic patient · visit {e(case.visit.get('date', ''))} · {e(rec.get('profile', ''))} · {len(rec.get('tool_calls', []))} tool calls</p>
{''.join(rows)}</section>
<aside><div class='box'><h3 style='margin-top:0'>The boundary</h3><p class='muted'>Everything this card used stayed on the private box, except these requests to the public zone. Each is a census tract, a need and a time window; nothing else can fit.</p>
<table><tr><th>Tract</th><th>Need</th><th>Days</th><th>Time</th></tr>{cross_rows or "<tr><td colspan=4>none</td></tr>"}</table></div>
<div class='box'><h3 style='margin-top:0'>Read locally (the private box)</h3><p class='muted'>{e(', '.join(local)) or 'none'}</p></div>
<div class='box'><h3 style='margin-top:0'>Home tract</h3><p class='muted'>{e(case.tract_geoid or 'unknown')}</p></div>
<div class='box'><h3 style='margin-top:0'>Your decisions train the model</h3><p class='muted'>Accept, edit or dismiss each item. Decisions are written to a log on the private box and become tonight's training examples for the fast local model. Nothing leaves this box.</p><p class='muted' id='fbcount'></p></div></aside></main>
<style>.fb{{margin-top:10px;display:flex;gap:6px;align-items:center}}.fb button{{font:inherit;font-size:13px;border:1px solid #2a3140;background:#12161d;color:#e6e8ee;border-radius:7px;padding:3px 10px;cursor:pointer}}
.fb button.on{{border-color:#2fbf71;color:#2fbf71}}li.clin{{border-left:3px solid #4c9aff;padding-left:8px;list-style:none;margin-left:-11px}}li.clin .cat{{color:#4c9aff}}.fb button[data-d=dismiss].on{{border-color:#e5534b;color:#e5534b}}.fbs{{font-size:13px;margin-left:6px}}</style>
<script>
const CASE={json.dumps(case.case_id)}; let nfb=0;
document.querySelectorAll('.fb button').forEach(b=>b.onclick=async()=>{{const box=b.parentElement, item=box.dataset.item, d=b.dataset.d;
  let note=null; if(d==='edit'){{note=prompt('What should this item say instead?'); if(note===null) return;}}
  const r=await fetch('../feedback/'+CASE,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{item_id:item,decision:d,note:note}})}});
  if(!r.ok){{box.querySelector('.fbs').textContent='not saved ('+r.status+')';return;}}
  box.querySelectorAll('button').forEach(x=>x.classList.remove('on')); b.classList.add('on'); box.querySelector('.fbs').textContent=d==='edit'?'edited: '+note:d+'ed';
  nfb++; document.getElementById('fbcount').textContent=nfb+' decision(s) logged on the private box this session'; if(d!=='accept') setTimeout(()=>location.reload(),400);}});
</script></body></html>"""


def main(argv=None) -> int:
    import argparse
    from .runner import apply_case_tracts
    ap = argparse.ArgumentParser(prog="careplan card")
    ap.add_argument("result")
    ap.add_argument("--cases", default="cases")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    rec = json.loads(Path(a.result).read_text())
    case = next(c for c in load_cases(Path(a.cases)) if c.case_id == rec["case_id"])
    apply_case_tracts([case])
    out = Path(a.out or f"reports/card_{rec['case_id']}.html")
    out.write_text(render(case, rec, ResourceTable.load(), TractTable.load()))
    print(f"wrote {out}")
    return 0
