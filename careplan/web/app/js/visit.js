// One visit, the doctor's workspace. Left: what the doctor already has (visit, chart, neighborhood).
// Right: the plan under review. Findings arrive over one event stream, live or from a saved real run:
//   GET /api/app/review/<case>?mode=recorded|live   ->  start, step*, item*, [rewrite], end {ok, doc | error}
// Decisions are saved with POST /api/app/decision/<case>; nothing is kept only on the page.
import { $, $$, api, cap, clock, date, h, hours, plural, secs, setPlanner, timeOfDay } from './ui.js';
import { countyBar, linkResources, map, mount, visitStrip } from './exhibits.js';

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

function finding(it, rows, chart, animate) {
  if (it.status === 'likely_ok') return h`<p class="okline">No barrier found for this patient.</p>`;
  const ex = exhibitsOf(it, chart);
  return h`${ex.barriers.map(b => h`<div class="barrier">
      <p class="why"><b>${cap(b.label)}.</b> ${cap(b.reason)}</p>
      <ul class="evidence">${b.rows.map(r => evidenceRow(r, rows))}</ul>
    </div>`)}
    ${ex.map ? map({ ...ex.map, animate }) : ''}
    ${ex.alts.length ? h`<div class="alts"><h4>What could work instead</h4><ul>${ex.alts.map(a => {
      const r = a.resource;
      return h`<li${a.n ? h` data-res="${a.n}" tabindex="0"` : ''}><span class="cat">${category(a.label)}</span><span>${a.text}${r ? h`<span class="res">
        <b>${a.n ? h`<span class="resnum">${a.n}</span>` : ''}${r.name}</b>${r.miles_from_home != null ? h`<span class="far">${r.miles_from_home} mi from home</span>` : ''}
        <small>${[r.address, hours(r.hours)].filter(Boolean).join(' · ')}${r.phone ? h` · <a href="tel:${r.phone.replace(/[^\d+]/g, '')}">${r.phone}</a>` : ''}</small></span>` : ''}</span></li>`;
    })}</ul></div>` : ''}`;
}

const decided = dec => !dec ? 'The doctor decides.' : dec.decision === 'accept' ? 'Accepted.' : dec.decision === 'dismiss' ? 'Dismissed: the plan stays as written.' : h`Changed to: “${dec.note}”`;

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

function boundary(doc, state) {
  const n = doc.crossings.length;
  if (state === 'before') return h`<h3>What leaves the private side</h3>
    <p class="muted">Nothing yet. To find help near the patient's home the review may ask the public side, and such a request can carry only a census tract, a kind of need, days and a time window.</p>`;
  return h`<h3>What left the private side${state === 'running' ? ' so far' : ''}</h3>
    ${n ? h`<p class="muted">To find help near home, the review asked the public side ${plural(n, 'time')}. This is everything those requests carried:</p>
      <table class="mini cross"><thead><tr><th>Census tract</th><th>Kind of need</th><th>Days</th></tr></thead><tbody>${doc.crossings.map(c => h`<tr class="in">
        <td class="mono">${c.tract_id}</td><td>${c.need}</td><td>${c.days.length === 7 ? 'Every day' : c.days.join(' ') || 'Any day'}</td></tr>`)}</tbody></table>
      <p class="muted small">A request has no field for a name, a date of birth, a record number or free text. <a href="#/system">System view</a></p>`
      : h`<p class="muted">${state === 'running' ? 'Nothing yet.' : 'Nothing. This review did not ask the public side for anything.'}</p>`}`;
}

export async function visit(root, id) {
  let doc = await api(`case/${id}`);
  setPlanner(doc.planner);
  let es = null, tick = null, running = false, savedAt = {}, onlyUnsaid = false;
  const canRun = m => m === 'recorded' ? doc.has_recording : doc.planner.answers;
  let mode = doc.has_recording && !(localStorage.getItem('cp.mode') === 'live' && canRun('live')) ? 'recorded' : 'live';
  let speed = SPEEDS.includes(+localStorage.getItem('cp.speed')) ? +localStorage.getItem('cp.speed') : 8;
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
              <button type="button" data-mode="live" title="Run the planner now">Live</button>
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
          <div class="pitem-body"></div></li>`)}</ol>
        <section class="boundary" id="boundary"></section>
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
    node.className = `pitem done ${it.status}${animate ? ' in' : ''}`;
    node.style.animationDelay = animate && delay ? `${delay}ms` : '';
    node.dataset.decision = dec ? dec.decision : '';
    $('.slot', node).innerHTML = h`<span class="chip ${it.status}">${it.status_label}</span>`;
    $('.pitem-body', node).innerHTML = h`${finding(it, rows, doc.chart, animate)}${decidable && it.status !== 'likely_ok' ? decideBar(it, dec, savedAt[it.item_id]) : ''}`;
    mount(node, city);
  }
  function clearItems(state) {                       // '' = as written, 'checking' = waiting for the review
    $$('.pitem', root).forEach(n => {
      n.className = `pitem ${state}`; n.dataset.decision = '';
      $('.slot', n).innerHTML = state ? h`<span class="chip wait">Checking</span>` : '';
      $('.pitem-body', n).innerHTML = '';
    });
  }
  function paintSteps(phase) {                       // phase: a pending label while running, or null when done
    const s = el('#steps');
    s.hidden = !doc.steps.length && !phase;
    s.innerHTML = h`${doc.steps.map(x => h`<li class="step ${x.kind}"><span class="tick" aria-hidden="true">${x.kind === 'crossing' ? '→' : '✓'}</span>${x.label}<small>${secs(x.t)}</small></li>`)}
      ${phase ? h`<li class="step pending"><span class="spin" aria-hidden="true"></span>${phase}</li>` : ''}`;
  }
  function paintRunline(state, info = {}) {
    const m = doc.meta, line = el('#runline');
    const src = info.source || m.source;
    line.hidden = src === 'none';
    if (line.hidden) return;
    el('#runtag').className = `runtag ${src}`;
    el('#runtag').textContent = src === 'recorded' ? 'Saved run' : 'Live';
    const when = info.saved || m.saved, model = info.model || m.model, profile = info.profile || m.profile;
    el('#runtext').title = profile ? `Profile ${profile} in config/models.yaml` : '';
    el('#runtext').textContent = src === 'recorded'
      ? (state === 'running' ? `A real run saved on ${date(when)}, shown ${info.speed > 1 ? `at ${info.speed}× speed` : 'in real time'}` : `A real run saved on ${date(when)}. It took ${secs(m.seconds)} on ${model}.`)
      : (state === 'running' ? `The planner (${model}) is reviewing this plan now` : `Reviewed just now by the planner (${model}) in ${secs(m.seconds)}.`);
    if (state !== 'running') el('#runclock').textContent = '';
  }
  function paintSummary() {
    const flagged = flaggedOf(doc), s = el('#summary');
    s.hidden = !doc.items.length;
    if (s.hidden) return;
    const unsaid = doc.items.flatMap(i => i.barriers.flatMap(b => b.evidence)).filter(e => !e.said_in_visit).length;
    const done = flagged.filter(i => doc.decisions[i.item_id]).length;
    s.innerHTML = h`<div><b>${flagged.length ? `${flagged.length} of ${doc.items.length} plan items may not work for this patient.` : 'No barrier found for any plan item.'}</b>
      ${unsaid ? h`<span>${plural(unsaid, 'piece')} of evidence ${unsaid === 1 ? 'was' : 'were'} never said in the visit.
        <button type="button" id="unsaid" aria-pressed="${onlyUnsaid}" title="Dim what was said in the visit. What stays came from the chart, the neighborhood and the schedules.">${onlyUnsaid ? 'Show everything' : `Show only ${unsaid === 1 ? 'that' : 'those'}`}</button></span>` : ''}</div>
      ${flagged.length ? h`<span class="count ${done === flagged.length ? 'all' : ''}">${done} of ${flagged.length} decided</span>` : ''}`;
  }
  function showUnsaid(on) { onlyUnsaid = on; el('.review').classList.toggle('only-unsaid', on); }
  function paintAfter() {
    const a = el('#after'), flagged = flaggedOf(doc);
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
    if (reviewed) doc.items.forEach(it => paintItem(it, { decidable: true })); else clearItems('');
    paintSteps(null); paintRunline('done'); paintSummary(); paintAfter(); markCited();
    el('#boundary').innerHTML = boundary(doc, reviewed ? 'done' : 'before');
    el('#go').textContent = reviewed ? 'Review again' : 'Review the plan';
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
  function stop() { running = false; if (es) { es.close(); es = null; } clearInterval(tick); paintControls(); }
  async function failed(message, usedMode) {
    try { doc = await api(`case/${id}`); } catch (e) { /* keep what we have */ }
    paintAll();
    const f = el('#fail');
    f.hidden = false;
    f.innerHTML = h`<b>The review did not finish.</b> ${message}
      ${usedMode === 'live' && doc.has_recording ? h`<button type="button" class="link" id="fallback">Play the saved run instead</button>` : ''}`;
    setPlanner(doc.planner); paintControls();
  }
  function start(useMode) {
    if (running) return;
    running = true; savedAt = {}; showUnsaid(false);
    doc = { ...doc, items: [], steps: [], crossings: [], decisions: {}, meta: { ...doc.meta, source: 'none' } };
    el('#fail').hidden = true; el('#summary').hidden = true; el('#after').hidden = true;
    clearItems('checking'); markCited(); paintControls();
    el('#go').textContent = 'Reviewing…';
    el('#boundary').innerHTML = boundary(doc, 'running');
    paintSteps('Starting');
    let rate = 1;
    const t0 = performance.now();
    tick = setInterval(() => { el('#runclock').textContent = secs((performance.now() - t0) / 1000 * rate); }, 200);
    const keep = useMode === 'live' && !doc.has_recording && el('#record').checked;
    es = new EventSource(`/api/app/review/${id}?mode=${useMode}&speed=${speed}${keep ? '&record=1' : ''}`);
    es.onmessage = m => {
      const ev = JSON.parse(m.data);
      if (ev.type === 'start') {
        rate = ev.speed || 1;
        paintRunline('running', ev); paintSteps('Reading');
      } else if (ev.type === 'step') {
        doc.steps.push(ev.step);
        if (ev.step.crossed) { doc.crossings.push(ev.step.crossed); el('#boundary').innerHTML = boundary(doc, 'running'); }
        paintSteps('Weighing the plan against what it read');
      } else if (ev.type === 'item') {
        doc.items = [...doc.items.filter(i => i.item_id !== ev.item.item_id), ev.item];
        paintItem(ev.item, { animate: true });
        paintSteps('Writing the findings');
      } else if (ev.type === 'rewrite') {
        doc.items = []; clearItems('checking'); paintSteps('Rewriting the findings');
      } else if (ev.type === 'end') {
        const shown = new Set(doc.items.map(i => i.item_id));
        stop();
        if (!ev.ok) return failed(ev.error || 'No reason was given.', useMode);
        doc = ev.doc;
        let late = 0;                                   // items that did not stream (the planner sent the card whole) fade in one after another
        doc.items.forEach(it => paintItem(it, { decidable: true, animate: !shown.has(it.item_id), delay: shown.has(it.item_id) ? 0 : 90 * late++ }));
        paintSteps(null); paintRunline('done'); paintSummary(); paintAfter(); markCited();
        el('#boundary').innerHTML = boundary(doc, 'done');
        el('#go').textContent = 'Review again';
      }
    };
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
      paintItem(doc.items.find(i => i.item_id === itemId), { decidable: true });
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
    $$('.tabs button', root).forEach(b => b.classList.toggle('on', b.dataset.tab === name));
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
    if (t.closest('#fallback')) return start('recorded');
    if (cites) return showSource(cites.dataset.ref);
    if (d) {
      const bar = d.closest('.decide'), box = $('.editbox', bar);
      if (d.dataset.d !== 'edit') return save(bar, d.dataset.d);
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

  paintAll(); paintControls();
  return () => { running = false; if (es) es.close(); clearInterval(tick); };
}
