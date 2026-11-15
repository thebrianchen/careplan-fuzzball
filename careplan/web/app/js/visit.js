// One visit, the doctor's workspace. Left: what the doctor already has (visit, chart, neighborhood).
// Right: the plan under review. Findings arrive over one event stream, live or from a saved real run:
//   GET /api/app/review/<case>?mode=recorded|live   ->  start, step*, item*, [rewrite], end {ok, doc | error}
// Decisions are saved with POST /api/app/decision/<case>; nothing is kept only on the page.
import { $, $$, api, cap, clock, date, h, hours, plural, secs, setPlanner, timeOfDay } from './ui.js';
import { countyBar, linkResources, map, mount, stripCount, visitStrip } from './exhibits.js';
import { setupVisitLayout } from './visit_layout.js';

const SPEEDS = [1, 4, 8, 20];
const DIALYSIS_ACTIONS = [
  { label: 'Request covered rides', text: 'Call the health plan using the number on the insurance card. Explain that long bus trips and lack of a car have led to missed dialysis. Request covered round-trip rides to dialysis, and confirm approval and pickup times. Keep attending scheduled dialysis while rides are arranged.' },
  { label: 'Assess home dialysis', text: 'Ask the dialysis team to assess whether home dialysis is suitable. Review support at home, training, space and insurance coverage with the team. Continue scheduled in-center dialysis until the team agrees on a new plan.' },
];
const dialysisTransport = it => /\bdialysis\b/i.test(it.plan_text) && it.barriers.some(b => b.barrier === 'transport');
const srcKind = e => e.said_in_visit ? 'visit' : e.source === 'Neighborhood' ? 'hood' : e.source === 'Transit schedules' ? 'transit' : 'chart';
const target = ref => ref.startsWith('transit:') ? ref.slice(8) : ref;          // a bus-trip citation points at its appointment
const category = s => cap(s).replace(/\b(nemt|snap|wic|liheap|pcp|ed)\b/gi, m => m.toUpperCase());
function evidenceText(e, rows) {                     // appointments and screener answers read better from the chart row than from the raw citation
  const r = rows[e.ref];
  if (r && e.ref.startsWith('appt:')) return `${date(r.date)}, ${clock(r.time)}, ${cap(r.dept)}: ${(r.status || '').replace('_', ' ')}`;
  if (r && e.ref.startsWith('screener:')) return `${r.question}: ${r.answer}`;
  return cap(e.text);
}
const flaggedOf = doc => doc.items.filter(i => i.status !== 'likely_ok');
// Flagged items that raise no barrier an earlier flagged item has not already raised: they tuck under one line, so the page
// shows each kind of problem once.
export function repeats(doc) {                       // decided by the server (Clinic.tucked), so every page agrees
  return doc.tucked || [];
}
let cityData = null;                                 // the maps' backdrop (~0.5 MB), fetched once per page load and only when a map appears
const city = () => cityData || (cityData = api('city'));

function context(doc) {
  const c = doc.chart, hood = c.neighborhood;
  return h`
  <div class="tabs" role="tablist">
    <button type="button" role="tab" data-tab="visit" class="on">Visit</button>
    <button type="button" role="tab" data-tab="chart">Chart</button>
    <button type="button" role="tab" data-tab="hood">Neighborhood</button>
  </div>
  <div class="pane" data-pane="visit">
    <p class="pane-note">What was said on ${date(doc.visit.date)}</p>
    ${doc.visit.turns.map(t => h`<div class="turn ${t.speaker}" data-ref="${t.ref}"><span class="who">${t.speaker}</span><p>${t.text}</p></div>`)}
  </div>
  <div class="pane" data-pane="chart" hidden>
    ${c.appointments.length ? h`<h3>Appointments</h3><table class="mini"><tbody>${c.appointments.map(a => h`<tr data-ref="${a.ref}">
      <td>${date(a.date)}</td><td>${clock(a.time)}</td><td>${cap(a.dept)}</td><td><span class="appt ${a.status}">${(a.status || '').replace('_', ' ')}</span></td></tr>`)}</tbody></table>` : ''}
    ${c.meds.length ? h`<h3>Medications</h3>${c.meds.map(m => h`<div class="rowline" data-ref="${m.ref}"><b>${cap(m.name)}</b> ${m.dose}, ${m.frequency}
      <small>${m.monthly_copay_usd != null ? `$${m.monthly_copay_usd} a month` : ''}${m.refill_gap_days ? h` · <span class="warn">${plural(m.refill_gap_days, 'day')} late on the last refill</span>` : ''}</small></div>`)}` : ''}
    ${c.screener.answers.length ? h`<h3>Social needs screener <small>${date(c.screener.date)}</small></h3>${c.screener.answers.map(a => h`<div class="rowline kv" data-ref="${a.ref}">
      <span>${a.question}</span><b>${cap(a.answer)}</b></div>`)}` : ''}
    ${c.notes.length ? h`<h3>Notes</h3>${c.notes.map(n => h`<div class="rowline" data-ref="${n.ref}"><small>${cap((n.role || '').replace('_', ' '))}, ${date(n.date)}</small>${n.text}</div>`)}` : ''}
  </div>
  <div class="pane" data-pane="hood" hidden>
    <p class="pane-note">Census tract ${hood.tract_id || 'unknown'}. These figures describe the neighborhood, not this patient: a reason to ask, not a finding.</p>
    ${hood.facts.map(f => h`<div class="rowline kv" data-ref="${f.ref}"><span>${cap(f.label)}${f.note ? h`<small class="warn">${f.note}</small>` : ''}</span><b>${f.value}</b>${countyBar(f, { compact: true })}</div>`)}
    ${hood.vintage ? h`<p class="pane-note">Sources: ${hood.vintage}</p>` : ''}
  </div>`;
}

// Which exhibits a finding gets, decided here only, from the refs its evidence cites and what the chart holds for them:
// two or more cited appointments in one barrier -> a strip of that department's visits; a cited tract fact with a county
// scale -> a bar; a cited appointment with a place, or an alternative with a located resource -> the map.
function exhibitsOf(it, chart) {
  const appt = Object.fromEntries(chart.appointments.map(a => [a.ref, a]));
  const fact = Object.fromEntries(chart.neighborhood.facts.map(f => [f.ref, f]));
  const barriers = it.barriers.map(b => {
    const cited = [...new Set(b.evidence.map(e => e.ref).filter(r => appt[r]))];
    const depts = [...new Set(cited.map(r => appt[r].dept))];
    const strip = cited.length >= 2 && { cited: new Set(cited), depts,
      appts: chart.appointments.filter(a => depts.includes(a.dept)).sort((x, y) => `${x.date} ${x.time}`.localeCompare(`${y.date} ${y.time}`)) };
    const rows = b.evidence.flatMap(e => !strip || !appt[e.ref] ? [{ e, bar: fact[e.ref] }] : e.ref === cited[0] ? [{ e, strip }] : []);
    return { ...b, rows };
  });
  const places = new Map();
  for (const e of it.barriers.flatMap(b => b.evidence)) {
    const a = appt[target(e.ref)], p = a && a.place;
    if (p && !places.has(`${p.lat},${p.lon}`)) places.set(`${p.lat},${p.lon}`, { ...p, ref: a.ref, time: a.time });
  }
  let n = 0;
  const alts = it.alternatives.map(a => ({ ...a, n: a.resource && a.resource.lat != null ? ++n : null }));
  const resources = alts.filter(a => a.n).map(a => ({ ...a.resource, n: a.n }));
  const located = places.size || resources.length ? { id: it.item_id, status: it.status, home: chart.home, places: [...places.values()], resources } : null;
  return { barriers, alts, map: located };
}

const unsaid = e => e.said_in_visit ? '' : h`<span class="unsaid">not said in the visit</span>`;
// One piece of evidence is one line: where it comes from and what it says in a sentence. When there is more to see (the rest
// of a quote, the visits one by one, the neighborhood against the county, the trip on a map), the line opens in place.
function evRow({ ref, kind, source, summary, more, e }) {
  return h`<li class="${more ? 'more' : ''}"><button type="button" class="ev" data-ref="${ref}"${more ? h` aria-expanded="false"` : ''}>
    <span class="src ${kind}">${source}</span><span class="ev-text">${summary}</span>${unsaid(e)}${more ? h`<span class="caret" aria-hidden="true"></span>` : ''}</button>
    ${more ? h`<div class="ev-more">${more}</div>` : ''}</li>`;
}
// The first sentence of what was said, without the speaker: 'Patient: “Two buses, all the way down. That was…”' -> “Two buses, all the way down.”
function firstSentence(text) {
  const said = text.replace(/^[^:“"]{0,24}:\s*/, '').replace(/^[“"]|[”"]$/g, ''), m = said.match(/^.*?[.!?](?=\s|$)/);
  return { summary: `“${m ? m[0] : said}”`, whole: !m || m[0].length >= said.length };
}
function evidenceRow({ e, strip, bar }, rows) {
  const base = { ref: e.ref, kind: srcKind(e), source: e.source, e };
  if (strip) return evRow({ ...base, ref: 'visits', summary: stripCount(strip), more: visitStrip(strip) });
  if (e.said_in_visit) {                              // what the line tells the doctor, when the planner has written it; the line itself opens under it
    const f = firstSentence(e.text), stated = rows.said && rows.said[e.ref];
    return evRow({ ...base, summary: stated || f.summary, more: stated || !f.whole ? cap(e.text) : '' });
  }
  const text = evidenceText(e, rows), cut = bar ? text.lastIndexOf(' (') : -1;     // a neighborhood figure: the comparison goes under the line
  return evRow({ ...base, summary: cut > 0 ? text.slice(0, cut) : text, more: bar ? h`${cut > 0 ? h`<p>${cap(text.slice(cut + 2).replace(/\)$/, ''))}</p>` : ''}${countyBar(bar)}` : '' });
}
// The three strongest pieces of evidence for the item's main barrier: the visits strip, what the visit never said, the trip,
// then what was said. A screener answer of "no" does not support a flag, so it comes last. A second barrier's evidence is not listed.
const SHOWN = 3;
function evidenceRows(ex, rows, animate) {
  const seen = new Set(), out = [];
  for (const r of ex.barriers[0].rows) {
    if (seen.has(r.e.ref)) continue;
    seen.add(r.e.ref);
    const denied = r.e.ref.startsWith('screener:') && /^(no|never|false)\b/i.test(String((rows[r.e.ref] || {}).answer || ''));
    out.push({ rank: denied ? 4 : r.strip ? 0 : r.e.said_in_visit ? 3 : 1, html: evidenceRow(r, rows) });
  }
  const p = ex.map && ex.map.places[0], t = p && p.trip, n = t && t.feasible ? (t.rides || []).length : 0;
  if (p) out.push({ rank: 2, html: evRow({ ref: 'map', kind: 'transit', source: 'Transit', e: { said_in_visit: false },
    summary: t && t.feasible ? `${t.minutes} min ${n ? `on ${plural(n, 'ride')}` : 'by bus or train'} to ${p.name}, ${p.miles_from_home} mi` : `${p.name} is ${p.miles_from_home} mi from home`,
    more: map({ ...ex.map, animate }) }) });
  return out.sort((x, y) => x.rank - y.rank).slice(0, SHOWN).map(x => x.html);
}

// One thing the doctor can choose for a flagged item: a row with a name and one line under it. `key` is what the page
// remembers as chosen: 'a2' is the review's third alternative, 'c0' the first clinician action. A located place keeps its
// number on the map.
function option({ key, title, detail, far, n, long }, picks) {
  const on = picks.has(key);
  return h`<li class="opt${on ? ' picked' : ''}${long ? ' long' : ''}" data-opt="${key}"${n ? h` data-res="${n}"` : ''} tabindex="0" role="checkbox" aria-checked="${String(on)}">
    <span class="tick" aria-hidden="true"></span>
    <span class="opt-main"><span class="res"><b>${n ? h`<span class="resnum">${n}</span>` : ''}${title}</b></span><small>${detail}</small></span>
    ${far ? h`<span class="far">${far}</span>` : ''}</li>`;
}
function altOption(a, i) {
  const r = a.resource;
  if (!r) return { key: `a${i}`, title: category(a.label), detail: a.text };
  return { key: `a${i}`, n: a.n, title: r.name, far: r.miles_from_home != null ? `${r.miles_from_home} mi` : '',
    detail: h`${[category(a.label), hours(r.hours), (r.address || '').split(',')[0]].filter(Boolean).join(' · ')}${r.phone ? h` · <a href="tel:${r.phone.replace(/[^\d+]/g, '')}">${r.phone}</a>` : ''}` };
}

function finding(it, rows, chart, animate, picks = new Set()) {
  if (it.status === 'likely_ok') return h`<p class="okline">No barrier found for this patient.</p>`;
  const ex = exhibitsOf(it, chart);
  const alts = ex.alts.map((a, i) => option(altOption(a, i), picks));
  const care = dialysisTransport(it) ? DIALYSIS_ACTIONS.map((a, i) => option({ key: `c${i}`, title: a.label, detail: a.text, long: true }, picks)) : [];
  const [main, ...also] = ex.barriers;
  const places = ex.map && !ex.map.places.length;      // a map of places that could help belongs with the choices, not the evidence
  return h`<p class="why"><b>${cap(main.label)}.</b> ${cap(main.reason)}</p>
    ${also.map(b => h`<p class="why also"><b>Also ${b.label}.</b> ${cap(b.reason)}</p>`)}
    <div class="sec barrier"><h4>Evidence</h4><ul class="evidence">${evidenceRows(ex, rows, animate)}</ul></div>
    ${care.length || alts.length ? h`<div class="sec alts"><h4>What could work instead${places ? h`<button type="button" class="link altmap" data-altmap aria-expanded="false">Map</button>` : ''}</h4>
      ${places ? h`<div class="altmap-box" hidden>${map({ ...ex.map, animate })}</div>` : ''}<ul>${care.length ? care : alts}</ul>
      ${care.length && alts.length ? h`<details><summary>${plural(alts.length, 'more suggestion')} from this review</summary><ul>${alts}</ul></details>` : ''}</div>` : ''}`;
}

const DECIDED = { accept: 'Accepted', edit: 'Changed', dismiss: 'Dismissed' };
// The options a saved decision stands for, so a reopened item shows what was chosen.
function chosenIn(it, dec) {
  if (!dec || !dec.note) return new Set();
  const action = DIALYSIS_ACTIONS.findIndex(a => a.text === dec.note), lines = dec.note.split('\n');
  if (action >= 0) return new Set([`c${action}`]);
  return new Set(dec.decision === 'accept' ? it.alternatives.flatMap((a, i) => lines.some(l => l.includes(a.text)) ? [`a${i}`] : []) : []);
}
const actionOf = dec => dec && dec.decision === 'edit' && DIALYSIS_ACTIONS.find(a => a.text === dec.note);   // an edit that is one of the offered actions
// A flagged item folded shut: what stands in its way, and how much is behind the fold.
function brief(it, dec) {
  const evidence = it.barriers.reduce((n, b) => n + b.evidence.length, 0);
  return h`<b>${it.barriers.map(b => cap(b.label)).join(' · ')}</b>
    <span>${actionOf(dec) ? actionOf(dec).label : dec && dec.decision === 'edit' ? h`Changed to: “${dec.note}”` : [plural(evidence, 'piece') + ' of evidence', it.alternatives.length && plural(it.alternatives.length, 'alternative')].filter(Boolean).join(' · ')}</span>`;
}
const decided = dec => !dec ? '' : actionOf(dec) ? `Accepted: ${actionOf(dec).label}.` : dec.decision === 'accept' ? 'Accepted.' : dec.decision === 'dismiss' ? 'Dismissed: the plan stays as written.' : h`Changed to: “${dec.note}”`;

function decideBar(it, dec, saved) {
  const on = d => dec && dec.decision === d ? 'on' : '';
  return h`<div class="decide" data-item="${it.item_id}">
    <div class="decide-btns" role="group" aria-label="Your decision on this flag">
      <button type="button" data-d="accept" class="${on('accept')}" title="The flag is right. The handout carries these alternatives.">Accept</button>
      <button type="button" data-d="edit" class="${on('edit')}" title="Write what this plan item should say instead.">Edit</button>
      <button type="button" data-d="dismiss" class="${on('dismiss')}" title="The flag is wrong for this patient. The plan stays as written.">Dismiss</button>
    </div>
    <span class="decided">${decided(dec)}${saved ? h` <small>Saved ${timeOfDay(saved)}</small>` : ''}</span>
    <form class="editbox" hidden><label>What should this plan item say instead?
      <textarea rows="3" maxlength="400">${dec && dec.note || ''}</textarea></label>
      <div><button type="submit" class="primary small">Save change</button><button type="button" class="link" data-cancel>Cancel</button></div></form>
  </div>`;
}

export async function visit(root, id) {
  let doc = await api(`case/${id}`);
  setPlanner(doc.planner);
  let es = null, tick = null, poll = null, running = false, savedAt = {};
  let open = new Set();                              // the flagged items whose evidence is unfolded; the rest show one line
  const MODES = ['recorded', 'sample', 'live'];        // in the order a visit falls back to them
  // A stand-in review is offered only for a visit that has no recorded run to play.
  const canRun = m => m === 'recorded' ? doc.has_recording : m === 'sample' ? doc.can_sample && !doc.has_recording : doc.planner.answers;
  const kept = localStorage.getItem('cp.mode');
  let mode = MODES.includes(kept) && canRun(kept) ? kept : MODES.find(canRun) || 'live';
  let speed = SPEEDS.includes(+localStorage.getItem('cp.speed')) ? +localStorage.getItem('cp.speed') : 1;
  const p = doc.patient, el = sel => $(sel, root);
  const rows = Object.fromEntries([...doc.chart.appointments, ...doc.chart.screener.answers].map(r => [r.ref, r]));
  Object.defineProperty(rows, 'said', { get: () => doc.said || {} });     // read at paint time: the document is replaced when a review ends

  root.innerHTML = h`<div class="page visit">
    <section class="banner">
      <a class="back" href="#/">← Visits</a>
      <div class="who"><h1>${p.name}</h1><span>${[p.age, p.sex, p.language].filter(Boolean).join(' · ')}</span></div>
      <div class="meta">${[cap(doc.visit.setting), date(doc.visit.date), p.coverage, p.pcp && `PCP ${p.pcp}`].filter(Boolean).join(' · ')}</div>
      <div class="problems">${p.problems.map(x => h`<span>${x}</span>`)}</div>
    </section>
    <div class="cols">
      <aside class="context" aria-label="What the doctor already has">${context(doc)}</aside>
      <section class="review">
        <div class="review-head">
          <div><h2>The plan</h2><p class="muted">${plural(doc.plan.length, 'item')} written in this visit</p></div>
          <div class="actions">
            <div class="seg" role="group" aria-label="Where the review comes from">
              <button type="button" data-mode="recorded" ${doc.has_recording ? '' : 'disabled'} title="${doc.has_recording ? 'Replay a real run of this review that was saved earlier' : 'No run has been saved for this visit'}">Saved run</button>
              ${doc.can_sample && !doc.has_recording ? h`<button type="button" data-mode="sample" title="Play a stand-in built from this visit's answer key. Not a model run.">Sample</button>` : ''}
              <button type="button" data-mode="live" title="${doc.planner.answers ? `Run the planner now, on ${doc.planner.label}` : 'Run the planner now'}">Live</button>
            </div>
            <select id="speed" aria-label="Playback speed of the saved run" title="Playback speed of the saved run">${SPEEDS.map(s => h`<option value="${s}">${s === 1 ? 'Real time' : `${s}× speed`}</option>`)}</select>
            <label class="keep" id="keep" title="Keep this live run so the visit can be replayed without the planner. A saved run is never overwritten."><input type="checkbox" id="record"> Save this run</label>
            <button type="button" id="go" class="primary">Review the plan</button>
          </div>
        </div>
        <div class="notice" id="blocked" hidden></div>
        <div class="runline" id="runline" hidden><span class="runtag" id="runtag"></span><span id="runtext"></span><span class="runclock" id="runclock"></span></div>
        <ul class="steps" id="steps" hidden></ul>
        <div class="notice bad" id="fail" hidden></div>
        <div class="summary" id="summary" hidden></div>
        <ol class="plan" id="plan">${doc.plan.map((x, i) => h`<li class="pitem" id="p-${x.item_id}">
          <div class="pitem-head"><span class="num">${i + 1}</span><div class="ptext">${x.text}<small>${(x.type || '').replace('_', ' ')}</small></div><span class="slot"></span></div>
          <div class="brief"></div><div class="pitem-body"></div></li>`)}</ol>
        <button type="button" class="tucked" id="tucked" hidden></button>
        <div class="after" id="after" hidden></div>
      </section>
    </div>
  </div>`;

  // ---- painting
  const li = itemId => root.querySelector(`#p-${CSS.escape(itemId)}`);
  function paintItem(it, { animate = false, decidable = false, delay = 0 } = {}) {
    const node = li(it.item_id);
    if (!node) return;
    const dec = doc.decisions[it.item_id];
    const flagged = it.status !== 'likely_ok', shut = flagged && !open.has(it.item_id);
    node.className = `pitem done ${it.status}${animate ? ' in' : ''}${flagged ? ' folds' : ''}${shut ? ' shut' : ''}`;
    node.style.animationDelay = animate && delay ? `${delay}ms` : '';
    node.dataset.decision = dec ? dec.decision : '';
    $('.slot', node).innerHTML = h`${dec ? h`<span class="chip dec ${actionOf(dec) ? 'accept' : dec.decision}">${DECIDED[actionOf(dec) ? 'accept' : dec.decision]}</span>` : ''}<span class="chip ${it.status}">${it.status_label}</span>
      ${flagged ? h`<button type="button" class="fold" aria-expanded="${String(!shut)}" aria-label="Evidence for plan item ${it.item_id}"></button>` : ''}`;
    $('.brief', node).innerHTML = flagged ? brief(it, dec) : '';
    $('.pitem-body', node).innerHTML = h`${finding(it, rows, doc.chart, animate, picked[it.item_id] || chosenIn(it, dec))}${decidable && it.status !== 'likely_ok' ? decideBar(it, dec, savedAt[it.item_id]) : ''}`;
    mount(node, city);
    tuck();
  }
  function fold(itemId, on) {                         // the map behind a fold draws when it is first shown (it watches its own size)
    on ? open.add(itemId) : open.delete(itemId);
    const node = li(itemId);
    node.classList.toggle('shut', !on);
    $('.fold', node).setAttribute('aria-expanded', String(on));
  }
  let untucked = false;
  const picked = {};                                  // item id -> the keys of the options the doctor chose (see option())
  const tuckedNow = () => untucked ? [] : repeats(doc, doc.plan);
  const shown = () => doc.items.filter(i => !tuckedNow().includes(i.item_id));     // what the page counts is what it shows
  function tuck() {                                   // after every paint: which repeats are folded away, and the line that says so
    const ids = repeats(doc, doc.plan);
    let n = 0;                                         // the items on screen are numbered 1, 2, 3 with no gaps
    $$('.pitem', root).forEach(node => {
      const away = !untucked && ids.includes(node.id.slice(2));
      node.classList.toggle('tuck', away);
      if (!away) $('.num', node).textContent = ++n;
    });
    const b = el('#tucked');
    b.hidden = true;                                   // the page shows each kind of problem once and does not offer the repeats
  }
  const nextUndecided = () => flaggedOf(doc).find(i => !doc.decisions[i.item_id] && !tuckedNow().includes(i.item_id));
  function openNext() { const next = nextUndecided(); open = new Set(next ? [next.item_id] : []); }
  function clearItems(state) {                       // '' = as written, 'checking' = waiting for the review
    $$('.pitem', root).forEach(n => {
      n.className = `pitem ${state}`; n.dataset.decision = '';
      $('.slot', n).innerHTML = state ? h`<span class="chip wait">Checking</span>` : '';
      $('.pitem-body', n).innerHTML = ''; $('.brief', n).innerHTML = '';
    });
    untucked = false; tuck();
  }
  function paintSteps(phase) {                       // phase: a pending label while running, or null when done
    const s = el('#steps');
    s.classList.add('brief-steps');                    // only what it is doing now; nothing once the review is done
    s.hidden = !phase;
    s.innerHTML = h`${doc.steps.map(x => h`<li class="step ${x.kind}"><span class="tick" aria-hidden="true">${x.kind === 'crossing' ? '→' : '✓'}</span>${x.label}<small>${secs(x.t)}</small></li>`)}
      ${phase ? h`<li class="step pending"><span class="spin" aria-hidden="true"></span>${phase}</li>` : ''}`;
  }
  function paintRunline(state, info = {}) {
    const m = doc.meta, line = el('#runline');
    const src = info.source || m.source;
    line.hidden = src === 'none' || (src === 'recorded' && state !== 'running');   // a saved run shows only its clock while it plays
    if (line.hidden) return;
    el('#runtag').hidden = src === 'recorded';
    el('#runtag').className = `runtag ${src}`;
    el('#runtag').textContent = { recorded: 'Saved run', sample: 'Sample', live: 'Live' }[src];
    const when = info.saved || m.saved, model = info.model || m.model, profile = info.profile || m.profile;
    const where = info.where || m.where, on = where ? ` on ${where}` : '';
    el('#runtext').title = profile ? `Profile ${profile} in config/models.yaml` : '';
    el('#runtext').textContent = src === 'sample' ? 'A stand-in built from this visit\'s answer key, for work on the pages. Not a model run.' : src === 'recorded'
      ? ''                                               // the tag says it is a saved run; where and when it was recorded is its tooltip
      : (state === 'running' ? `The planner (${model})${on} is reviewing this plan now` : `Reviewed just now by the planner (${model})${on} in ${secs(m.seconds)}.`);
    if (state !== 'running') el('#runclock').textContent = '';
  }
  function paintSummary() {
    const items = shown(), flagged = items.filter(i => i.status !== 'likely_ok'), s = el('#summary');
    s.hidden = !doc.items.length;
    if (s.hidden) return;
    const unsaid = items.flatMap(i => i.barriers.flatMap(b => b.evidence)).filter(e => !e.said_in_visit).length;
    const done = flagged.filter(i => doc.decisions[i.item_id]).length;
    s.innerHTML = h`<div><b>${flagged.length ? `${flagged.length} of ${items.length} plan items may not work for this patient.` : 'No barrier found for any plan item.'}</b>
      ${unsaid ? h`<span>${plural(unsaid, 'piece')} of evidence ${unsaid === 1 ? 'was' : 'were'} never said in the visit.</span>` : ''}</div>
      ${flagged.length ? h`<span class="count ${done === flagged.length ? 'all' : ''}">${done} of ${flagged.length} decided</span>` : ''}`;
  }
  function paintAfter() {
    const a = el('#after'), flagged = shown().filter(i => i.status !== 'likely_ok');
    a.hidden = !doc.items.length;
    if (a.hidden) return;
    const open = flagged.filter(i => !doc.decisions[i.item_id]).length;
    a.innerHTML = h`<a class="btn primary" href="#/handout/${id}">Prepare the patient handout</a>
      <span class="muted">${open ? `${plural(open, 'flagged item')} still ${open === 1 ? 'has' : 'have'} no decision.` : ''}</span>
      <button type="button" class="link" id="reset" title="Forget this review on screen. The decision log keeps its rows.">Clear this review</button>`;
  }
  function markCited() {
    $$('.context .cited', root).forEach(n => n.classList.remove('cited'));
    for (const e of doc.items.flatMap(i => i.barriers.flatMap(b => b.evidence))) {
      const n = root.querySelector(`.context [data-ref="${CSS.escape(target(e.ref))}"]`);
      if (n) n.classList.add('cited');
    }
  }
  function paintAll() {
    const reviewed = doc.items.length > 0;
    layout.setResults(reviewed);
    if (reviewed) { openNext(); doc.items.forEach(it => paintItem(it, { decidable: true })); } else clearItems('');
    paintSteps(null); paintRunline('done'); paintSummary(); paintAfter(); markCited();
    el('#go').textContent = reviewed ? 'Review again' : 'Review Plan';
  }
  function paintControls() {
    $$('.seg button', root).forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
    el('#speed').hidden = mode !== 'recorded';
    const blocked = !canRun(mode);
    el('#keep').hidden = mode !== 'live' || doc.has_recording || blocked;
    el('#blocked').hidden = running || !blocked || !el('#fail').hidden;
    if (blocked) el('#blocked').innerHTML = doc.has_recording
      ? h`<b>A live review needs the planner.</b> The planner is not answering: ${doc.planner.why}. Choose Saved run to replay a real review of this visit.`
      : h`<b>This visit cannot be reviewed right now.</b> It has no saved run, and a live review needs the planner. The planner is not answering: ${doc.planner.why}.
        <a href="#/">Back to the visits</a>: the ones with a saved run are listed first.`;
    const saved = root.querySelector('.seg [data-mode=recorded]');
    saved.title = doc.has_recording ? 'Replay a real run of this review that was saved earlier' : 'No run has been saved for this visit';
    el('#speed').value = String(speed);
    el('#go').disabled = running || blocked;
    $$('.seg button, #speed, #record', root).forEach(b => { if (b.dataset.mode !== 'recorded' || doc.has_recording) b.disabled = running; });
  }

  // ---- the review
  function stop() { running = false; if (es) { es.close(); es = null; } clearInterval(tick); clearInterval(poll); paintControls(); }
  async function failed(message, usedMode) {
    try { doc = await api(`case/${id}`); } catch (e) { /* keep what we have */ }
    setPlanner(doc.planner);
    // A live review that fails goes straight to this visit's recorded run, and says so: nobody should have to find a button.
    if (usedMode === 'live' && doc.has_recording) return start('recorded', h`<b>The live review did not finish.</b> ${message} This is the recorded run of the same visit.`);
    paintAll();
    const f = el('#fail');
    f.hidden = false; f.className = 'notice bad';
    f.innerHTML = h`<b>The review did not finish.</b> ${message}`;
    paintControls();
  }
  function start(useMode, note) {
    if (running) return;
    begin(note);
    const keep = useMode === 'live' && !doc.has_recording && el('#record').checked;
    const rid = Math.random().toString(36).slice(2);
    follow(rid, useMode, Date.now() / 1000, `/api/app/review/${id}?mode=${useMode}&speed=${speed}&rid=${rid}${keep ? '&record=1' : ''}`);
  }
  function begin(note) {
    running = true; savedAt = {}; open.clear(); Object.keys(picked).forEach(k => delete picked[k]);
    doc = { ...doc, items: [], steps: [], crossings: [], decisions: {}, meta: { ...doc.meta, source: 'none' } };
    el('#fail').hidden = !note; el('#fail').className = 'notice'; el('#fail').innerHTML = note || '';
    el('#summary').hidden = true; el('#after').hidden = true;
    layout.setResults(true);
    clearItems('checking'); markCited(); paintControls();
    el('#go').textContent = 'Reviewing…';
    paintSteps('Starting');
  }
  // A review runs on the server to its end, whether or not this page stays. The page follows it by its id: over the stream
  // when it started the review here, and by asking for its events either way, so a page that left picks it up on return.
  function follow(rid, useMode, started, streamUrl) {
    let rate = 1, seen = -1, misses = 0;
    tick = setInterval(() => { el('#runclock').textContent = secs(Math.max(0, Date.now() / 1000 - started) * rate); }, 200);
    if (streamUrl) {
      es = new EventSource(streamUrl);
      es.onmessage = m => apply(JSON.parse(m.data));
      es.onerror = () => { if (es) { es.close(); es = null; } };       // never reconnect: that would ask for the review again
    }
    const ask = async () => {
      try { (await api(`progress/${id}?rid=${rid}&after=${seen}`)).events.forEach(apply); misses = 0; }
      catch (e) { if (++misses >= 8 && running) { stop(); failed('The connection to the app server dropped during the review.', useMode); } }
    };
    poll = setInterval(ask, 700);
    if (!streamUrl) ask();                             // picking a review up: show where it stands at once
    function apply(ev) {
      if (!running) return;
      if (ev.type === 'busy') { stop(); begin(); return follow(ev.rid, ev.mode, ev.started, null); }   // already under way: follow that one
      if (ev.i <= seen) return;
      seen = ev.i;
      if (ev.type === 'start') {
        rate = ev.speed || 1;
        paintRunline('running', ev); paintSteps('Reading');
      } else if (ev.type === 'step') {
        doc.steps.push(ev.step);
        if (ev.step.crossed) doc.crossings.push(ev.step.crossed);
        paintSteps('Weighing the plan against what it read');
      } else if (ev.type === 'item') {
        doc.items = [...doc.items.filter(i => i.item_id !== ev.item.item_id), ev.item];
        if (!open.size && ev.item.status !== 'likely_ok') open.add(ev.item.item_id);      // the first flag to arrive opens; the rest fold
        paintItem(ev.item, { animate: true });
        paintSteps('Writing the findings');
      } else if (ev.type === 'rewrite') {
        doc.items = []; clearItems('checking'); paintSteps('Rewriting the findings');
      } else if (ev.type === 'end') {
        const shown = new Set(doc.items.map(i => i.item_id));
        stop();
        if (!ev.ok) return failed(ev.error || 'No reason was given.', useMode);
        doc = ev.doc;
        if (![...open].some(i => doc.items.some(x => x.item_id === i && x.status !== 'likely_ok'))) openNext();
        let late = 0;                                   // items that did not stream (the planner sent the card whole) fade in one after another
        doc.items.forEach(it => paintItem(it, { decidable: true, animate: !shown.has(it.item_id), delay: shown.has(it.item_id) ? 0 : 90 * late++ }));
        paintSteps(null); paintRunline('done'); paintSummary(); paintAfter(); markCited();
        el('#go').textContent = 'Review again';
      }
    }
  }

  // ---- decisions
  async function save(bar, decision, note) {
    const itemId = bar.dataset.item, out = $('.decided', bar);
    out.textContent = 'Saving…';
    try {
      const r = await api(`decision/${id}`, { item_id: itemId, decision, note });
      doc.decisions = r.decisions; savedAt[itemId] = r.saved;
      doc.handout = null;
      open.delete(itemId);                             // a decided flag folds to one line, and the next one without a decision opens
      paintItem(doc.items.find(i => i.item_id === itemId), { decidable: true });
      const next = nextUndecided();
      if (next) fold(next.item_id, true);
      paintSummary(); paintAfter();
    } catch (err) {
      out.innerHTML = h`<span class="bad">Not saved: ${err.message}</span> Choose again to retry.`;
    }
  }
  function showSource(ref) {
    const n = root.querySelector(`.context [data-ref="${CSS.escape(target(ref))}"]`);
    if (!n) return;
    openTab(n.closest('.pane').dataset.pane);
    const box = [n.closest('[data-pane]'), el('.context')].find(x => x && x.scrollHeight > x.clientHeight + 4);   // the page itself never moves
    if (box) box.scrollTop += n.getBoundingClientRect().top - box.getBoundingClientRect().top - box.clientHeight / 2 + n.offsetHeight / 2;
    n.classList.remove('flash'); void n.offsetWidth; n.classList.add('flash');
  }
  function openTab(name) {
    $$('.tabs button', root).forEach(b => {
      const active = b.dataset.tab === name;
      b.classList.toggle('on', active); b.setAttribute('aria-selected', String(active));
    });
    $$('.pane', root).forEach(x => { x.hidden = x.dataset.pane !== name; });
  }

  const page = el('.page');                          // listeners live on this view's own node, so they go when the view goes
  page.addEventListener('click', async e => {
    const t = e.target;
    const tab = t.closest('.tabs button'), seg = t.closest('.seg button'), cites = t.closest('.review [data-ref]'), d = t.closest('.decide-btns button');
    if (tab) return openTab(tab.dataset.tab);
    if (seg) { mode = seg.dataset.mode; localStorage.setItem('cp.mode', mode); return paintControls(); }
    if (t.closest('#go')) return start(mode);
    const opt = t.closest('.alts li[data-opt]');
    if (opt && !t.closest('a')) {                       // choose what goes on the patient's plan, then Accept
      const node = opt.closest('.pitem'), set = picked[node.id.slice(2)] || (picked[node.id.slice(2)] = new Set()), key = opt.dataset.opt;
      if (set.has(key)) set.delete(key);
      else {                                            // a clinician action stands alone; the review's alternatives can be combined
        [...set].filter(k => key[0] === 'c' || k[0] === 'c').forEach(k => set.delete(k));
        set.add(key);
      }
      $$('.alts li[data-opt]', node).forEach(li => { const on = set.has(li.dataset.opt); li.classList.toggle('picked', on); li.setAttribute('aria-checked', String(on)); });
      return;
    }
    const head = t.closest('.pitem.folds .pitem-head, .pitem.folds .brief');
    if (head) { const node = head.closest('.pitem'); return fold(node.id.slice(2), node.classList.contains('shut')); }
    const altmap = t.closest('[data-altmap]');
    if (altmap) { const box = $('.altmap-box', altmap.closest('.alts')); box.hidden = !box.hidden; return altmap.setAttribute('aria-expanded', String(!box.hidden)); }
    if (cites) {
      const head = t.closest('.evidence li.more > .ev');
      if (head) head.setAttribute('aria-expanded', String(head.parentElement.classList.toggle('open')));
      return showSource(cites.dataset.ref);
    }
    if (d) {
      const bar = d.closest('.decide'), box = $('.editbox', bar);
      if (d.dataset.d !== 'edit') {
        const itemId = bar.dataset.item, it = doc.items.find(i => i.item_id === itemId), keys = [...(picked[itemId] || [])].sort();
        const action = d.dataset.d === 'accept' && keys.find(k => k[0] === 'c');
        if (action) return save(bar, 'edit', DIALYSIS_ACTIONS[+action.slice(1)].text);     // the action's full wording becomes the plan item
        const chosen = d.dataset.d === 'accept' ? keys.map(k => it.alternatives[+k.slice(1)]).filter(Boolean) : [];
        return save(bar, d.dataset.d, chosen.map(a => a.resource && !a.text.includes(a.resource.name) ? `${a.resource.name}: ${a.text}` : a.text).join('\n') || undefined);
      }
      box.hidden = false; $('textarea', box).focus();
      return;
    }
    if (t.closest('[data-cancel]')) { t.closest('.editbox').hidden = true; return; }
    if (t.closest('#reset')) { await api(`reset/${id}`, {}); doc = await api(`case/${id}`); savedAt = {}; Object.keys(picked).forEach(k => delete picked[k]); el('#fail').hidden = true; paintAll(); paintControls(); }
  });
  page.addEventListener('keydown', e => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('.alts li[data-opt]')) { e.preventDefault(); e.target.click(); }
  });
  page.addEventListener('submit', e => {
    e.preventDefault();
    const bar = e.target.closest('.decide'), note = $('textarea', e.target).value.trim();
    if (bar && note) save(bar, 'edit', note);
  });
  ['mouseover', 'mouseout', 'focusin', 'focusout'].forEach(type => page.addEventListener(type, linkResources));
  const onSpeed = e => { speed = +e.target.value; localStorage.setItem('cp.speed', String(speed)); };
  el('#speed').addEventListener('change', onSpeed);

  const layout = setupVisitLayout(root, doc);
  paintAll(); paintControls();
  if (doc.running) { begin(); follow(doc.running.rid, doc.running.mode, doc.running.started, null); }   // a review started before this page loaded
  return () => { running = false; if (es) es.close(); clearInterval(tick); clearInterval(poll); };
}
