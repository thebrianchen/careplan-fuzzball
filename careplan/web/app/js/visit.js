// One visit, the doctor's workspace. Left: what the doctor already has (visit, chart, neighborhood).
// Right: the plan under review. Findings arrive over one event stream, live or from a saved real run:
//   GET /api/app/review/<case>?mode=recorded|live   ->  start, step*, item*, [rewrite], end {ok, doc | error}
// Decisions are saved with POST /api/app/decision/<case>; nothing is kept only on the page.
import { $, $$, api, cap, clock, date, h, hours, plural, secs, setPlanner, timeOfDay } from './ui.js';
import { countyBar, linkResources, map, mount, visitStrip } from './exhibits.js';
import { setupVisitLayout } from './visit_layout.js';

const SPEEDS = [1, 4, 8, 20];
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
export function repeats(doc, plan) {
  const seen = new Set(), out = [];
  for (const it of plan.map(p => doc.items.find(i => i.item_id === p.item_id)).filter(i => i && i.status !== 'likely_ok')) {
    const kinds = it.barriers.map(b => b.barrier);
    if (kinds.length && kinds.every(k => seen.has(k))) out.push(it.item_id);
    kinds.forEach(k => seen.add(k));
  }
  return out;
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
function evidenceRow({ e, strip, bar }, rows) {
  if (strip) return h`<li class="${e.said_in_visit ? 'said' : ''}"><div class="ev strip-row"><span class="src ${srcKind(e)}">${e.source}</span><span class="ev-text">${visitStrip(strip)}</span>${unsaid(e)}</div></li>`;
  return h`<li class="${e.said_in_visit ? 'said' : ''}"><button type="button" class="ev" data-ref="${e.ref}" title="Show where this comes from">
    <span class="src ${srcKind(e)}">${e.source}</span><span class="ev-text">${evidenceText(e, rows)}${bar ? countyBar(bar) : ''}</span>${unsaid(e)}</button></li>`;
}

// An open finding leads with its first barrier and the three strongest pieces of evidence, what the visit never said first.
// The rest of the evidence and any further barrier open on request.
const KEEP = 3;
function barrierBlock(b, i, rows) {
  const ordered = [...b.rows].sort((x, y) => x.e.said_in_visit - y.e.said_in_visit), rest = ordered.slice(KEEP);
  const body = h`<ul class="evidence">${ordered.slice(0, KEEP).map(r => evidenceRow(r, rows))}</ul>
    ${rest.length ? h`<details class="more-ev"><summary>${plural(rest.length, 'more piece')} of evidence</summary><ul class="evidence">${rest.map(r => evidenceRow(r, rows))}</ul></details>` : ''}`;
  return i ? h`<details class="barrier also"><summary><b>Also: ${b.label}.</b> ${cap(b.reason)}</summary>${body}</details>`
    : h`<div class="barrier"><p class="why"><b>${cap(b.label)}.</b> ${cap(b.reason)}</p>${body}</div>`;
}

function finding(it, rows, chart, animate) {
  if (it.status === 'likely_ok') return h`<p class="okline">No barrier found for this patient.</p>`;
  const ex = exhibitsOf(it, chart);
  return h`${ex.barriers.map((b, i) => barrierBlock(b, i, rows))}
    ${ex.map ? map({ ...ex.map, animate }) : ''}
    ${ex.alts.length ? h`<div class="alts"><h4>What could work instead</h4><ul>${ex.alts.map(a => {
      const r = a.resource;
      return h`<li data-alt="${ex.alts.indexOf(a)}"${a.n ? h` data-res="${a.n}" tabindex="0"` : ''} title="Choose this for the patient's plan"><span class="cat">${category(a.label)}</span><span>${a.text}${r ? h`<span class="res">
        <b>${a.n ? h`<span class="resnum">${a.n}</span>` : ''}${r.name}</b>${r.miles_from_home != null ? h`<span class="far">${r.miles_from_home} mi from home</span>` : ''}
        <small>${[r.address, hours(r.hours)].filter(Boolean).join(' · ')}${r.phone ? h` · <a href="tel:${r.phone.replace(/[^\d+]/g, '')}">${r.phone}</a>` : ''}</small></span>` : ''}</span></li>`;
    })}</ul></div>` : ''}`;
}

const DECIDED = { accept: 'Accepted', edit: 'Changed', dismiss: 'Dismissed' };
// A flagged item folded shut: what stands in its way, and how much is behind the fold.
function brief(it, dec) {
  const evidence = it.barriers.reduce((n, b) => n + b.evidence.length, 0);
  return h`<b>${it.barriers.map(b => cap(b.label)).join(' · ')}</b>
    <span>${dec && dec.decision === 'edit' ? h`Changed to: “${dec.note}”` : [plural(evidence, 'piece') + ' of evidence', it.alternatives.length && plural(it.alternatives.length, 'alternative')].filter(Boolean).join(' · ')}</span>`;
}
const decided = dec => !dec ? '' : dec.decision === 'accept' ? 'Accepted.' : dec.decision === 'dismiss' ? 'Dismissed: the plan stays as written.' : h`Changed to: “${dec.note}”`;

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
      <textarea rows="2" maxlength="400">${dec && dec.note || ''}</textarea></label>
      <div><button type="submit" class="primary small">Save change</button><button type="button" class="link" data-cancel>Cancel</button></div></form>
  </div>`;
}

export async function visit(root, id) {
  let doc = await api(`case/${id}`);
  setPlanner(doc.planner);
  let es = null, tick = null, poll = null, running = false, savedAt = {}, onlyUnsaid = false;
  let open = new Set();                              // the flagged items whose evidence is unfolded; the rest show one line
  const MODES = ['recorded', 'sample', 'live'];        // in the order a visit falls back to them
  // A stand-in review is offered only for a visit that has no recorded run to play.
  const canRun = m => m === 'recorded' ? doc.has_recording : m === 'sample' ? doc.can_sample && !doc.has_recording : doc.planner.answers;
  const kept = localStorage.getItem('cp.mode');
  let mode = MODES.includes(kept) && canRun(kept) ? kept : MODES.find(canRun) || 'live';
  let speed = SPEEDS.includes(+localStorage.getItem('cp.speed')) ? +localStorage.getItem('cp.speed') : 20;
  const p = doc.patient, el = sel => $(sel, root);
  const rows = Object.fromEntries([...doc.chart.appointments, ...doc.chart.screener.answers].map(r => [r.ref, r]));

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
        <button type="button" class="link steps-toggle" id="steps-toggle" hidden></button>
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
    $('.slot', node).innerHTML = h`${dec ? h`<span class="chip dec ${dec.decision}">${DECIDED[dec.decision]}</span>` : ''}<span class="chip ${it.status}">${it.status_label}</span>
      ${flagged ? h`<button type="button" class="fold" aria-expanded="${String(!shut)}" aria-label="Evidence for plan item ${it.item_id}"></button>` : ''}`;
    $('.brief', node).innerHTML = flagged ? brief(it, dec) : '';
    $('.pitem-body', node).innerHTML = h`${finding(it, rows, doc.chart, animate)}${decidable && it.status !== 'likely_ok' ? decideBar(it, dec, savedAt[it.item_id]) : ''}`;
    mount(node, city);
    tuck();
  }
  function fold(itemId, on) {                         // the map behind a fold draws when it is first shown (it watches its own size)
    on ? open.add(itemId) : open.delete(itemId);
    const node = li(itemId);
    node.classList.toggle('shut', !on);
    $('.fold', node).setAttribute('aria-expanded', String(on));
  }
  let untucked = false, stepsOpen = false;
  const picked = {};                                  // item id -> the alternatives the doctor chose, by position
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
    const s = el('#steps'), t = el('#steps-toggle');
    t.hidden = !doc.steps.length;
    t.textContent = `${stepsOpen ? 'Hide' : 'Show'} the ${plural(doc.steps.length, 'check')}`;
    s.classList.toggle('brief-steps', !stepsOpen);     // closed: only what it is doing now
    s.hidden = !phase && !(stepsOpen && doc.steps.length);
    s.innerHTML = h`${doc.steps.map(x => h`<li class="step ${x.kind}"><span class="tick" aria-hidden="true">${x.kind === 'crossing' ? '→' : '✓'}</span>${x.label}<small>${secs(x.t)}</small></li>`)}
      ${phase ? h`<li class="step pending"><span class="spin" aria-hidden="true"></span>${phase}</li>` : ''}`;
  }
  function paintRunline(state, info = {}) {
    const m = doc.meta, line = el('#runline');
    const src = info.source || m.source;
    line.hidden = src === 'none';
    if (line.hidden) return;
    el('#runtag').className = `runtag ${src}`;
    el('#runtag').textContent = { recorded: 'Saved run', sample: 'Sample', live: 'Live' }[src];
    const when = info.saved || m.saved, model = info.model || m.model, profile = info.profile || m.profile;
    const where = info.where || m.where, on = where ? ` on ${where}` : '';
    el('#runtext').title = profile ? `Profile ${profile} in config/models.yaml` : '';
    el('#runtext').textContent = src === 'sample' ? 'A stand-in built from this visit\'s answer key, for work on the pages. Not a model run.' : src === 'recorded'
      ? (state === 'running' ? `A real run recorded${on}, ${date(when)}, shown ${info.speed > 1 ? `at ${info.speed}× speed` : 'in real time'}` : `A real run recorded${on}, ${date(when)}. It took ${secs(m.seconds)} with ${model}.`)
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
      ${unsaid ? h`<span>${plural(unsaid, 'piece')} of evidence ${unsaid === 1 ? 'was' : 'were'} never said in the visit.
        <button type="button" id="unsaid" aria-pressed="${onlyUnsaid}" title="Dim what was said in the visit. What stays came from the chart, the neighborhood and the schedules.">${onlyUnsaid ? 'Show everything' : `Show only ${unsaid === 1 ? 'that' : 'those'}`}</button></span>` : ''}</div>
      ${flagged.length ? h`<span class="count ${done === flagged.length ? 'all' : ''}">${done} of ${flagged.length} decided</span>` : ''}`;
  }
  function showUnsaid(on) { onlyUnsaid = on; el('.review').classList.toggle('only-unsaid', on); }
  function paintAfter() {
    const a = el('#after'), flagged = shown().filter(i => i.status !== 'likely_ok');
    a.hidden = !doc.items.length;
    if (a.hidden) return;
    const open = flagged.filter(i => !doc.decisions[i.item_id]).length;
    a.innerHTML = h`<a class="btn primary" href="#/handout/${id}">Prepare the patient handout</a>
      <span class="muted">${open ? `${plural(open, 'flagged item')} still ${open === 1 ? 'has' : 'have'} no decision.` : flagged.length ? 'Every flag has a decision.' : ''}</span>
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
    running = true; savedAt = {}; open.clear(); showUnsaid(false);
    doc = { ...doc, items: [], steps: [], crossings: [], decisions: {}, meta: { ...doc.meta, source: 'none' } };
    el('#fail').hidden = !note; el('#fail').className = 'notice'; el('#fail').innerHTML = note || '';
    el('#summary').hidden = true; el('#after').hidden = true;
    layout.setResults(true);
    clearItems('checking'); markCited(); paintControls();
    el('#go').textContent = 'Reviewing…';
    paintSteps('Starting');
    let rate = 1;
    const t0 = performance.now();
    tick = setInterval(() => { el('#runclock').textContent = secs((performance.now() - t0) / 1000 * rate); }, 200);
    const keep = useMode === 'live' && !doc.has_recording && el('#record').checked;
    const rid = Math.random().toString(36).slice(2);
    let seen = -1;
    es = new EventSource(`/api/app/review/${id}?mode=${useMode}&speed=${speed}&rid=${rid}${keep ? '&record=1' : ''}`);
    es.onmessage = m => apply(JSON.parse(m.data));
    // Some proxies hold an event stream back until it ends, so the same events are also asked for; each is applied once.
    poll = setInterval(async () => {
      try { (await api(`progress/${id}?rid=${rid}&after=${seen}`)).events.forEach(apply); } catch (e) { /* the stream may still deliver */ }
    }, 700);
    function apply(ev) {
      if (!running || ev.i <= seen) return;
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
    es.onerror = () => { if (running) { stop(); failed('The connection to the app server dropped during the review.', useMode); } };
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
    const box = el('.context');
    if (box.scrollHeight > box.clientHeight + 4) box.scrollTop += n.getBoundingClientRect().top - box.getBoundingClientRect().top - box.clientHeight / 2 + n.offsetHeight / 2;
    else n.scrollIntoView({ block: 'center', behavior: 'smooth' });
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
    if (t.closest('#unsaid')) { showUnsaid(!onlyUnsaid); return paintSummary(); }
    if (t.closest('#steps-toggle')) { stepsOpen = !stepsOpen; return paintSteps(running ? 'Reviewing' : null); }
    const alt = t.closest('.alts li[data-alt]');
    if (alt && !t.closest('a')) {                       // choose which alternatives go on the patient's plan, then Accept
      const itemId = alt.closest('.pitem').id.slice(2), set = picked[itemId] || (picked[itemId] = new Set()), n = +alt.dataset.alt;
      set.has(n) ? set.delete(n) : set.add(n);
      return alt.classList.toggle('picked', set.has(n));
    }
    const head = t.closest('.pitem.folds .pitem-head, .pitem.folds .brief');
    if (head) { const node = head.closest('.pitem'); return fold(node.id.slice(2), node.classList.contains('shut')); }
    if (cites) return showSource(cites.dataset.ref);
    if (d) {
      const bar = d.closest('.decide'), box = $('.editbox', bar);
      if (d.dataset.d !== 'edit') {
        const itemId = bar.dataset.item, it = doc.items.find(i => i.item_id === itemId);
        const chosen = d.dataset.d === 'accept' ? [...(picked[itemId] || [])].sort().map(n => it.alternatives[n]).filter(Boolean) : [];
        return save(bar, d.dataset.d, chosen.map(a => a.resource && !a.text.includes(a.resource.name) ? `${a.resource.name}: ${a.text}` : a.text).join('\n') || undefined);
      }
      box.hidden = false; $('textarea', box).focus();
      return;
    }
    if (t.closest('[data-cancel]')) { t.closest('.editbox').hidden = true; return; }
    if (t.closest('#reset')) { await api(`reset/${id}`, {}); doc = await api(`case/${id}`); savedAt = {}; el('#fail').hidden = true; showUnsaid(false); paintAll(); paintControls(); }
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
  return () => { running = false; if (es) es.close(); clearInterval(tick); clearInterval(poll); };
}
