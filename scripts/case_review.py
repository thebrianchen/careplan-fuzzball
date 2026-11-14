"""Case review packet for Lynna: one readable HTML page per case (record, visit, plan, the planted answer key with its
evidence resolved to text), with a review form per planted conflict and decoy. No server: "Copy review" assembles a
Markdown block she can paste into a message. Opens on a phone.

    python scripts/case_review.py cases/dev --out reports/case_review
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from careplan.card import FIELD_LABEL, _evidence  # noqa: E402
from careplan.cases import load_cases  # noqa: E402
from careplan.data import TractTable  # noqa: E402
from careplan.runner import apply_case_tracts  # noqa: E402

e = html.escape
CSS = """body{margin:0;font:16px/1.5 -apple-system,Segoe UI,sans-serif;background:#fff;color:#111;max-width:900px;margin:0 auto;padding:20px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;color:#555;margin:22px 0 8px;text-transform:uppercase;letter-spacing:.05em}
.muted{color:#666}table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:5px 6px;border-bottom:1px solid #e5e5e5;vertical-align:top}
th{color:#666;font-size:11px;text-transform:uppercase}.turn{margin:4px 0}.turn b{color:#555;margin-right:6px}.topic{font-size:11px;background:#eef;color:#336;border-radius:4px;padding:0 5px;margin-left:6px}
.conf{border:1px solid #ddd;border-left:5px solid #e8b931;border-radius:8px;padding:12px 14px;margin:10px 0}.conf.blocked{border-left-color:#e5534b}.decoy{border-left-color:#999}
.ev{margin:6px 0 0;padding-left:18px;font-size:14px;color:#333}.ev .src{color:#4c6aff;font-size:12px;font-weight:600;min-width:110px;display:inline-block}
.form{margin-top:8px;display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:14px}.form textarea,.form input{font:inherit;width:100%;margin-top:6px;padding:6px;border:1px solid #ccc;border-radius:6px}
button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid #333;background:#111;color:#fff;cursor:pointer}
.sticky{position:sticky;bottom:0;background:#fff;border-top:1px solid #ddd;padding:12px 0;margin-top:30px}
pre{background:#f6f6f6;padding:10px;border-radius:8px;font-size:13px;white-space:pre-wrap}.plan li{margin:4px 0}.nav a{margin-right:14px}
"""


def disputes(case, run_dirs: list[Path], tract) -> dict:
    """Where the planner (Super, C2) and our key disagree, across reps. Returns
    {"raised_not_in_key": [(item, barrier, n_reps, reasons, evidence_texts)], "in_key_not_raised": [(conflict, n_reps_missed)]}."""
    import glob, json as _json
    from collections import defaultdict
    recs = []
    for rd in run_dirs:
        for f in glob.glob(str(rd / "results" / f"{case.case_id}_C2_*.json")):
            r = _json.loads(Path(f).read_text())
            if r.get("assessment"):
                recs.append(r)
    if not recs:
        return {}
    key = {(c["item_id"], c["barrier"]) for c in case.data["planted_conflicts"]}
    raised = defaultdict(lambda: {"n": 0, "reasons": [], "evidence": set()})
    for r in recs:
        for it in r["assessment"]["items"]:
            for b in it.get("barriers", []) or []:
                k = (it["item_id"], b["barrier"]); raised[k]["n"] += 1
                raised[k]["reasons"].append(b.get("reason", "")); raised[k]["evidence"].update(b.get("evidence", []))
    out = {"n_runs": len(recs), "raised_not_in_key": [], "in_key_not_raised": []}
    for k, v in sorted(raised.items()):
        if k not in key and v["n"] >= max(1, len(recs) // 2):
            ev = [_evidence(case, ref, tract) for ref in sorted(v["evidence"])[:4]]
            out["raised_not_in_key"].append((k[0], k[1], v["n"], v["reasons"][0], ev))
    for c in case.data["planted_conflicts"]:
        n = raised.get((c["item_id"], c["barrier"]), {"n": 0})["n"]
        if n <= len(recs) // 2:
            out["in_key_not_raised"].append((c, len(recs) - n))
    return out


def page(case, tracts: TractTable, prev: str | None, nxt: str | None, run_dirs: list[Path] | None = None) -> str:
    p, v = case.patient, case.visit
    tract = tracts.get(case.tract_geoid)
    conf_by_item = {}
    for c in case.data["planted_conflicts"]:
        conf_by_item.setdefault(c["item_id"], []).append(c)
    out = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Review {e(case.case_id)}</title><style>{CSS}</style></head><body>"
           f"<div class='nav'><a href='index.html'>All cases</a>{f'<a href={prev}.html>← {prev}</a>' if prev else ''}{f'<a href={nxt}.html>{nxt} →</a>' if nxt else ''}</div>"
           f"<h1>{e(case.case_id)} · {e(case.data.get('title', ''))}</h1>"
           f"<p class='muted'>Made-up patient, real Chicago neighborhood data · {'reviewed by ' + e(str(case.data['reviewed_by'])) if case.data.get('reviewed_by') else 'not yet reviewed'}</p>"
           "<div style='background:#f3f6fb;border-radius:8px;padding:12px 14px;margin:10px 0'>"
           "<b>Your job on this page: break ties.</b> Our system read this case and in a few places it disagrees with the answer key we wrote: it raised a barrier we didn't list, or it missed one we did. "
           "Each red or grey box below is one such disagreement. Read the plan item, the barrier, and the evidence, then pick a side. There is no 'unsure'. "
           "<p style='margin:8px 0 0'><b>The bar:</b> count a barrier only if (1) this plan item actually needs the thing the barrier blocks, (2) something in <i>this</i> patient's visit, chart or neighborhood shows it, and (3) you'd act on it: change the plan, refer, or at least ask before they left. "
           "The margin test: if a colleague wrote this plan, would you write <i>'this won't work because X'</i> next to it? If yes, the barrier is real. If you'd write nothing, it isn't. 'Could be a problem' is not enough; a system that raises everything gets ignored.</p>"
           "<p style='margin:8px 0 0' class='muted'>Order on this page: the case (patient, visit, plan), then <a href='#decisions'>your decisions</a>, then Submit. You're deciding whether the barrier is real, not what to prescribe. Notes optional.</p></div>"]
    # record (the case first, in reading order; the decisions follow it)
    out.append("<h2>Patient</h2><table>")
    out.append(f"<tr><th>Age / sex / language</th><td>DOB {e(p['dob'])} · {e(p.get('sex', ''))} · {e(p.get('language', 'en'))}</td></tr>")
    cov = p.get("coverage", {})
    out.append(f"<tr><th>Coverage</th><td>{e(cov.get('type', ''))} — {e(cov.get('plan', ''))}; transport benefit: {e(str(cov.get('transport_benefit', '?')))}</td></tr>")
    out.append(f"<tr><th>Home</th><td>{e(p.get('address', ''))} · tract {e(case.tract_geoid or '?')}</td></tr>")
    out.append(f"<tr><th>Problems</th><td>{'; '.join(e(x['display']) for x in p.get('problems', []))}</td></tr>")
    out.append("<tr><th>Medications</th><td>" + "<br>".join(f"{e(m['ref'])} {e(m['name'])} {e(m.get('dose', ''))} {e(m.get('frequency', ''))} — ${m.get('monthly_copay_usd', '?')}/mo, tier {m.get('cost_tier', '?')}, refill gap {m.get('refill_gap_days', 0)} d" for m in p.get("meds", [])) + "</td></tr>")
    out.append("<tr><th>Appointments</th><td>" + "<br>".join(f"{e(a['ref'])} {e(a['date'])} {e(a.get('time', ''))} {e(a['dept'])} {e(a.get('location', ''))} — <b>{e(a['status'].replace('_', ' '))}</b>" for a in p.get("appointments", [])) + "</td></tr>")
    out.append("<tr><th>Notes</th><td>" + "<br>".join(f"{e(n['ref'])} ({e(n.get('date', ''))}, {e(n.get('role', '').replace('_', ' '))}): {e(n['text'])}" for n in p.get("notes", [])) + "</td></tr>")
    scr = (p.get("screener") or {}).get("answers") or {}
    out.append(f"<tr><th>Screener ({e((p.get('screener') or {}).get('date', ''))})</th><td>" + ", ".join(f"{e(k.replace('_', ' '))}: <b>{e(str(val))}</b>" for k, val in scr.items()) + "</td></tr>")
    if tract:
        sig = [f"{e(FIELD_LABEL.get(k, k))}: {tract[k]:.0%}" if k == "no_vehicle_share" else f"{e(FIELD_LABEL.get(k, k))}: {tract[k]}"
               for k in ("no_vehicle_share", "places_lacktrpt", "places_foodinsecu", "places_shututility", "places_housinsecu", "early_service_stops") if tract.get(k) is not None]
        out.append(f"<tr><th>Neighborhood (tract data)</th><td>{' · '.join(sig)}</td></tr>")
    out.append("</table>")
    # visit
    out.append(f"<h2>Visit ({e(v.get('date', ''))}, {e(v.get('setting', ''))})</h2>")
    for t in v["transcript"]:
        topics = "".join(f"<span class='topic'>{e(tp)}</span>" for tp in t.get("topics", []))
        out.append(f"<div class='turn'><b>{e(t['ref'])} {e(t['speaker'])}:</b>{e(t['text'])}{topics}</div>")
    # plan
    out.append("<h2>Draft plan</h2><ul class='plan'>")
    for it in case.plan:
        flag = f" <span class='muted'>(we claim a barrier here, see below)</span>" if it["item_id"] in conf_by_item else " <span class='muted'>(we claim no barrier)</span>"
        out.append(f"<li><b>{e(it['item_id'])}</b> ({e(it['type'])}): {e(it['text'])}{' [' + e(it['schedule']) + ']' if it.get('schedule') else ''}{flag}</li>")
    out.append("</ul>")
    plan_text = {it["item_id"]: it["text"] for it in case.plan}
    d = disputes(case, run_dirs or [], tract)
    if d:
        out.append(f"<h2 style='color:#b00' id='decisions'>Your decisions: where the system and our key disagree ({d['n_runs']} runs)</h2>"
                   "<p class='muted' style='margin-top:0'>The system read this case and raised barriers our key doesn't have, or missed ones it does. You're the tie-break: who's right?</p>")
        for item, barrier, n, reason, ev in d["raised_not_in_key"]:
            evh = "".join(f"<li><span class='src'>{e(s)}</span> {e(tx)}</li>" for s, tx in ev)
            out.append(f"<div class='conf' style='border-left-color:#b00' data-id='dispute:{e(item)}:{e(barrier)}'>"
                       f"<b>{e(item)}</b> {e(plan_text.get(item, ''))}<br>"
                       f"The system says: <b>{e(barrier.replace('_', ' '))}</b> is a barrier ({n} of {d['n_runs']} runs). Its reason: <i>{e(reason)}</i>"
                       f"<ul class='ev'>{evh}</ul>"
                       f"<div class='muted' style='font-size:14px'>Our key says: no barrier here.</div>"
                       f"<div class='form'><label><input type='radio' name='dispute:{e(item)}:{e(barrier)}' value='system'> The system is right (add it to the key)</label>"
                       f"<label><input type='radio' name='dispute:{e(item)}:{e(barrier)}' value='key'> Our key is right (not a barrier)</label>"
                       f"<textarea name='dispute:{e(item)}:{e(barrier)}_note' rows='1' placeholder='optional'></textarea></div></div>")
        for c, missed in d["in_key_not_raised"]:
            evh = "".join(f"<li><span class='src'>{e(s)}</span> {e(tx)}</li>" for s, tx in (_evidence(case, x["ref"], tract) for x in c["evidence"]))
            out.append(f"<div class='conf' style='border-left-color:#888' data-id='missed:{e(c['conflict_id'])}'>"
                       f"<b>{e(c['item_id'])}</b> {e(plan_text.get(c['item_id'], ''))}<br>"
                       f"Our key says: <b>{e(c['barrier'].replace('_', ' '))}</b> is a barrier. The system did not raise it in {missed} of {d['n_runs']} runs."
                       f"<ul class='ev'>{evh}</ul>"
                       f"<div class='form'><label><input type='radio' name='missed:{e(c['conflict_id'])}' value='key'> Our key is right (the system missed a real barrier)</label>"
                       f"<label><input type='radio' name='missed:{e(c['conflict_id'])}' value='system'> The system is right (not really a barrier; drop it from the key)</label>"
                       f"<textarea name='missed:{e(c['conflict_id'])}_note' rows='1' placeholder='optional'></textarea></div></div>")
        if not d["raised_not_in_key"] and not d["in_key_not_raised"]:
            out.append("<p class='muted'>None: the system and our key agree on every item here. The rest of this page is optional.</p>")
        out.append("<p class='muted'>These are the only decisions we need. Pick a side on each (no 'unsure': if it's a close call, the margin test decides). The full case follows for reference.</p>")
    out.append("<h2>Anything we missed?</h2><p class='muted' style='margin-top:0'>Optional. A barrier to a plan item that we didn't list; a visit that doesn't sound like a real one; a suggestion of ours you'd never say to a patient.</p><textarea id='general' rows='3' style='width:100%;font:inherit;padding:6px' placeholder='optional'></textarea>")
    # answer key
    ref_only = bool(d)
    out.append("<details" + (" open" if not ref_only else "") + "><summary><h2 style='display:inline'>Reference: everything our key says about this plan</h2>" + (" <span class='muted'>(the system agreed with all of these unless listed above; nothing to answer)</span>" if ref_only else "") + "</summary>")
    out.append("<h3 style='font-size:14px;color:#555;margin:14px 0 6px'>Where we say the plan won't work</h3>" + ("" if ref_only else "<p class='muted' style='margin-top:0'>Each box: would you agree this is a real barrier to this part of the plan for this patient?</p>"))
    for c in case.data["planted_conflicts"]:
        ev = "".join(f"<li><span class='src'>{e(s)}</span> {e(t)}</li>" for s, t in (_evidence(case, x["ref"], tract) for x in c["evidence"]))
        out.append(f"<div class='conf {e(c['expected_status'])}' data-id='{e(c['conflict_id'])}'>"
                   f"<b>{e(c['conflict_id'])} · {e(c['item_id'])}</b> {e(plan_text.get(c['item_id'], ''))}<br>"
                   f"The barrier: <b>{e(c['barrier'].replace('_', ' '))}</b> · how much it gets in the way: <b>{'probably can\'t do it' if c['expected_status'] == 'blocked' else 'may not manage it'}</b> · the evidence:"
                   f"<ul class='ev'>{ev}</ul>"
                   f"<div class='muted' style='font-size:14px'>What we'd suggest instead: {e(', '.join(x.replace('_', ' ') for x in c.get('acceptable_alternative_categories', [])))}{' · Note: ' + e(c['note']) if c.get('note') else ''}</div>"
                   + ("" if ref_only else f"<div class='form'><label><input type='radio' name='{e(c['conflict_id'])}' value='confirm'> Agree: a real barrier to this item</label>"
                   f"<label><input type='radio' name='{e(c['conflict_id'])}' value='change'> Partly: real problem, wrong reason or evidence</label>"
                   f"<label><input type='radio' name='{e(c['conflict_id'])}' value='reject'> Disagree: not a barrier, the item is fine</label>"
                   f"<textarea name='{e(c['conflict_id'])}_note' rows='1' placeholder='optional: what would you change, or why not'></textarea></div>") + "</div>")
    if not case.data["planted_conflicts"]:
        out.append("<p class='muted'>None: we think this whole plan is realistic as written. If you'd change something, say so in the box at the bottom.</p>")
    out.append("<h3 style='font-size:14px;color:#555;margin:14px 0 6px'>Hardships we say don't change this plan</h3>" + ("" if ref_only else "<p class='muted' style='margin-top:0'>Each box: do you agree this does not affect any part of the plan?</p>"))
    for i, dd in enumerate(case.data.get("decoys", []), start=1):
        s, tx = _evidence(case, dd["ref"], tract)
        out.append(f"<div class='conf decoy' data-id='decoy{i}'><b>{e(dd['barrier'].replace('_', ' '))}</b> — <span class='src'>{e(s)}</span> {e(tx)}<br>"
                   f"<span class='muted' style='font-size:14px'>Why we think it doesn't change the plan: {e(dd.get('why_irrelevant', ''))}</span>"
                   + ("" if ref_only else f"<div class='form'><label><input type='radio' name='decoy{i}' value='confirm'> Agree: doesn't affect the plan</label>"
                   f"<label><input type='radio' name='decoy{i}' value='reject'> Disagree: it is a barrier to a plan item (say which)</label>"
                   f"<textarea name='decoy{i}_note' rows='1' placeholder='note'></textarea></div>") + "</div>")
    if not case.data.get("decoys"):
        out.append("<p class='muted'>None.</p>")
    out.append("</details>")
    out.append(f"""<div class='sticky'><button onclick='submitReview()'>Submit review</button> <button onclick='copyReview()' style='background:#fff;color:#111'>Copy instead</button> <span id='copied' class='muted'></span>
<pre id='out' style='display:none'></pre></div>
<script>
const CASE={json.dumps(case.case_id)};
function collect(){{const lines=['## Review '+CASE+' ('+new Date().toISOString().slice(0,10)+')']; const answers={{}};
 document.querySelectorAll('.conf').forEach(c=>{{const id=c.dataset.id; if(!c.querySelector('input[type=radio]')) return; const r=document.querySelector(`input[name="${{id}}"]:checked`);const ta=document.querySelector(`textarea[name="${{id}}_note"]`);const n=ta?ta.value.trim():'';
   answers[id]={{decision:r?r.value:null,note:n||null}}; lines.push(`- ${{id}}: ${{r?r.value:'(no answer)'}}${{n?' — '+n:''}}`)}});
 const g=document.getElementById('general').value.trim(); if(g){{lines.push('- general: '+g); answers.general=g;}}
 return {{text:lines.join('\\n'), answers}};}}
async function submitReview(){{const {{text,answers}}=collect(); const st=document.getElementById('copied');
 const open=[...document.querySelectorAll('.conf')].filter(c=>c.querySelector('input[type=radio]')&&!c.querySelector('input[type=radio]:checked'));
 if(open.length){{st.textContent=open.length+' box(es) still need a decision'; open[0].scrollIntoView({{behavior:'smooth',block:'center'}}); open[0].style.outline='3px solid #b00'; return;}}
 try{{const r=await fetch('/submit',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{case_id:CASE,answers,text}})}});
   if(r.ok){{st.textContent='saved (you can come back and change it any time). Next case →'; localStorage.removeItem('draft:'+CASE); return;}} }}catch(e){{}}
 st.textContent='could not save (offline?). Use Copy instead and paste it to Brian.';}}
// prefill from the last submission for this case (server), falling back to this browser's draft
async function prefill(){{let prev=null;
 try{{const r=await fetch('/answers/'+CASE,{{cache:'no-store'}}); if(r.ok){{const j=await r.json(); if(j&&j.answers) prev=j;}}}}catch(e){{}}
 const draft=localStorage.getItem('draft:'+CASE); const answers=(prev&&prev.answers)||(draft?JSON.parse(draft):null); if(!answers) return;
 for(const [id,v] of Object.entries(answers)){{ if(id==='general'){{document.getElementById('general').value=v||''; continue;}}
   if(v&&v.decision){{const r=document.querySelector(`input[name="${{id}}"][value="${{v.decision}}"]`); if(r) r.checked=true;}}
   const ta=document.querySelector(`textarea[name="${{id}}_note"]`); if(ta&&v&&v.note) ta.value=v.note; }}
 if(prev){{const st=document.getElementById('copied'); st.textContent='showing your answers from '+prev.ts.slice(0,16).replace('T',' ')+'Z; change anything and Submit again to overwrite'; }} }}
prefill();
document.addEventListener('change',()=>localStorage.setItem('draft:'+CASE, JSON.stringify(collect().answers)));
function copyReview(){{const {{text}}=collect(); const pre=document.getElementById('out'); pre.textContent=text; pre.style.display='block';
 navigator.clipboard&&navigator.clipboard.writeText(text).then(()=>document.getElementById('copied').textContent='copied to clipboard; paste it to Brian',()=>{{}});}}
</script></body></html>""")
    return "".join(out)


START_HERE = {"D001", "D003", "D006", "D007", "D009", "T010", "T015", "T019"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="?", default="cases/dev")
    ap.add_argument("--out", type=Path, default=Path("reports/case_review"))
    ap.add_argument("--runs", type=Path, nargs="*", default=[], help="planner run dirs (C2) to show disputes against the key")
    a = ap.parse_args()
    cases = load_cases(Path(a.cases))
    apply_case_tracts(cases)
    tracts = TractTable.load()
    a.out.mkdir(parents=True, exist_ok=True)
    ids = [c.case_id for c in cases]
    counts = {}
    for i, c in enumerate(cases):
        (a.out / f"{c.case_id}.html").write_text(page(c, tracts, ids[i - 1] if i else None, ids[i + 1] if i + 1 < len(ids) else None, a.runs))
        dd = disputes(c, a.runs, tracts.get(c.tract_geoid)) if a.runs else {}
        counts[c.case_id] = len(dd.get("raised_not_in_key", [])) + len(dd.get("in_key_not_raised", [])) if dd else None
    cases = sorted(cases, key=lambda c: -(counts.get(c.case_id) or 0))
    rows = "".join(f"<li><a href='{e(c.case_id)}.html'><b>{e(c.case_id)}</b></a> {e(c.data.get('title', ''))} "
                   f"<span class='muted'>· " + (f"<b>{counts[c.case_id]} decision(s) needed</b>" if counts.get(c.case_id) else ("no disagreements, nothing to answer" if counts.get(c.case_id) == 0 else f"{len(c.data['planted_conflicts'])} conflicts"))
                   + f"{' · reviewed by ' + e(str(c.data['reviewed_by'])) if c.data.get('reviewed_by') else ''}</span></li>" for c in cases)
    (a.out / "index.html").write_text(f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Case review</title><style>{CSS}</style></head><body>"
                                      f"<h1>Care Plan Check: case review</h1>"
                                      f"<div style='background:#f3f6fb;border-radius:8px;padding:12px 14px;margin:10px 0'><b>Lynna, here's the whole job:</b> we wrote {len(cases)} made-up patients, each with a care plan. For each one we listed the barriers we think get in the way of specific parts of the plan "
                                      f"(no car for 6 AM dialysis, no grocery store for a renal diet, a $47 copay…). <b>We only need you where our system and our key disagree.</b> Each case page opens with those disagreements (the system raised a barrier we didn't list, or missed one we did); you pick a side on each, no 'unsure'. The bar: a barrier counts only if you'd act on it (change the plan, refer, or ask); 'could be a problem' is not enough. Cases are sorted by how many decisions they need; the ones with none need nothing. Press Submit when the boxes are answered. 1–3 minutes a case.</div>"
                                      f"<p class='muted'>If Submit fails, press 'Copy instead' and text the result to Brian. The agent pre-review (<a href='../case_prereview.md'>here</a>) is our own list of suspected mistakes; read it only if curious.</p>"
                                      f"<ul>{rows}</ul>"
                                      f"<p class='muted'>Generated from {e(a.cases)}; thresholds: rules/barrier_rules.yaml (Cook County 75th percentile for tract signals).</p></body></html>")
    print(f"{len(cases)} pages -> {a.out}/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
