// Front end for the Care Plan Check recorded demo. No framework, no build step.
// It loads ONE JSON document (shape: see docs/frontend-starter.md) and plays it back.
//   ?case=D001   which case            ?speed=8   playback speed for a recorded run (1 = real time)
//   ?live=1      ask the backend to run the model now instead of using the saved run (needs the GPU box)
const q = new URLSearchParams(location.search);
const CASE = q.get('case') || 'D001', LIVE = q.get('live') === '1', SPEED = Math.max(1, +q.get('speed') || 8);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
let doc = null, viaApi = false;

// Display text for a raw value from the JSON (dates, 24-hour times, snake_case, lower-case starts). Not for anyone's own words.
const MONTHS = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ');
const clock = (h, m) => `${h % 12 || 12}:${m}`, half = h => (h < 12 ? 'AM' : 'PM');
function tidy(s) {
  s = String(s ?? '')
    .replace(/\b(\d{4})-(\d\d)-(\d\d)\b/g, (_, y, m, d) => `${MONTHS[m - 1]} ${+d}, ${y}`)
    // A time already followed by AM/PM (or the start of a range that is) is left alone.
    .replace(/\b([01]?\d|2[0-3]):([0-5]\d)(?:-([01]?\d|2[0-3]):([0-5]\d))?\b(?!\s*[AP]M|-\d)/gi, (_, h1, m1, h2, m2) => {
      if (h2 === undefined) return `${clock(+h1, m1)} ${half(+h1)}`;
      return half(+h1) === half(+h2) ? `${clock(+h1, m1)}–${clock(+h2, m2)} ${half(+h2)}`
        : `${clock(+h1, m1)} ${half(+h1)}–${clock(+h2, m2)} ${half(+h2)}`;
    })
    .replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

async function load(live) {
  // With our Python server running, /api/demo/<case> answers. Opened as plain static files, the saved JSON is used.
  try { const r = await fetch(`/api/demo/${CASE}${live ? '?live=1' : ''}`); if (r.ok) { viaApi = true; return await r.json(); } } catch (e) {}
  return await (await fetch(`data/${CASE}.json`)).json();
}

function renderCase() {
  const p = doc.patient;
  $('patient').innerHTML = `<h2>Patient</h2><div class="name">${esc(p.name)}, ${esc(p.age)}</div>
    <div class="muted">${esc(tidy(doc.visit.setting))} · ${esc(tidy(doc.visit.date))} · ${esc(p.coverage)}</div>
    <div class="problems">${p.problems.map(esc).join(' · ')}</div>`;
  $('visit').innerHTML = doc.visit.turns.map(t =>
    `<div class="turn ${esc(t.speaker)}"><span class="who">${esc(t.speaker)}</span>${esc(t.text)}</div>`).join('');
  $('plan').innerHTML = doc.plan.map(i => `<li id="plan-${i.item_id}">${esc(i.text)}</li>`).join('');
  if (viaApi) { $('clinic').href = `/app/#/visit/${encodeURIComponent(CASE)}`; $('clinic').hidden = false; }
}

function showMode(playing) {
  const m = $('mode'), rec = doc.meta.source === 'recorded';
  const saved = doc.meta.saved ? ` from ${tidy(doc.meta.saved.slice(0, 10))}` : '';
  m.className = 'tag mode ' + (rec ? 'recorded' : 'live');
  m.textContent = rec ? `Recorded run${saved}${playing && SPEED > 1 ? `, shown at ${SPEED}× speed` : ''} (took ${Math.round(doc.meta.seconds)} s)` : 'Live run';
}

function addStep(s) {
  const li = document.createElement('li');
  li.className = 'fade ' + (s.kind === 'crossing' ? 'crossing' : '');
  li.innerHTML = `<span class="t">${Math.round(s.t)} s</span>${esc(s.label)}` +
    (s.crossed ? ` <span class="sent">(sent: tract ${esc(s.crossed.tract_id)}, ${esc(s.crossed.need)}, ${esc(s.crossed.days.join(' '))})</span>` : '');
  $('steps').appendChild(li);
}

function addFinding(it) {
  $(`plan-${it.item_id}`).insertAdjacentHTML('beforeend', `<span class="chip ${it.status} fade">${esc(it.status_label)}</span>`);
  const div = document.createElement('div');
  div.className = `card finding fade ${it.status}`;
  div.innerHTML = `<h3>${esc(it.plan_text)} <span class="chip ${it.status}">${esc(it.status_label)}</span></h3>` +
    it.barriers.map(b => `<div class="barrier"><b>${esc(tidy(b.label))}</b>: ${esc(tidy(b.reason))}
      <ul class="evidence">${b.evidence.map(e => `<li class="${e.said_in_visit ? '' : 'unsaid'}"><span class="src">${esc(e.source)}</span><span>${esc(tidy(e.text))}</span>` +
        (e.said_in_visit ? '' : '<span class="badge">not said in the visit</span>') + '</li>').join('')}</ul></div>`).join('') +
    (it.alternatives.length ? `<ul class="alts">${it.alternatives.map(a => `<li><span class="cat">${esc(tidy(a.label))}</span><div>${esc(a.text)}` +
      (a.resource ? `<div class="res">${esc(a.resource.name)} · ${esc(tidy(a.resource.hours))} · ${esc(a.resource.miles_from_home)} mi from home</div>` : '') + `</div></li>`).join('')}</ul>` : '') +
    (it.status !== 'likely_ok' ? `<div class="decide"><button data-d="accept">Accept</button><button data-d="dismiss" class="dismiss">Dismiss</button><span class="muted">the doctor decides</span></div>` : '');
  // The doctor's decision is remembered on this page only. With the backend: POST /feedback/<case> {item_id, decision}.
  div.querySelectorAll('.decide button').forEach(b => b.onclick = () => {
    div.querySelectorAll('.decide button').forEach(x => x.classList.remove('on')); b.classList.add('on');
  });
  $('findings').appendChild(div);
}

function finish() {
  const flagged = doc.items.filter(i => i.status !== 'likely_ok').length;
  const unsaid = doc.items.flatMap(i => i.barriers.flatMap(b => b.evidence)).filter(e => !e.said_in_visit).length;
  $('summary').hidden = false;
  $('summary').textContent = `${flagged} of ${doc.items.length} plan items may not work for this patient. ` +
    `${unsaid} ${unsaid === 1 ? 'piece of evidence was' : 'pieces of evidence were'} never said in the visit.`;
  $('crossings').innerHTML = doc.crossings.map(c => `<tr><td>${esc(c.tract_id)}</td><td>${esc(tidy(c.need))}</td><td>${esc(c.days.join(' '))}</td></tr>`).join('');
  $('boundary').hidden = false;
  showMode(false);
}

async function review() {
  $('review').disabled = true; $('steps').innerHTML = ''; $('findings').innerHTML = ''; $('summary').hidden = true; $('boundary').hidden = true;
  document.querySelectorAll('#plan .chip').forEach(c => c.remove());
  if (LIVE) { $('steps').innerHTML = '<li class="muted">Running the model on the private side (about a minute and a half)…</li>'; doc = await load(true); $('steps').innerHTML = ''; }
  showMode(true);
  const speed = doc.meta.source === 'recorded' ? SPEED : 1e9;          // a live result has already happened: show it at once
  const events = [...doc.steps.map(s => ({t: s.t, run: () => addStep(s)})), ...doc.items.map(i => ({t: i.t ?? 0, run: () => addFinding(i)}))].sort((a, b) => a.t - b.t);
  const t0 = performance.now();
  const tick = setInterval(() => { $('clock').textContent = `${Math.round((performance.now() - t0) / 1000 * Math.min(speed, 1e3))} s`; }, 200);
  for (const ev of events) {
    const wait = ev.t / speed * 1000 - (performance.now() - t0);
    if (wait > 0) await new Promise(r => setTimeout(r, wait));
    ev.run();
  }
  clearInterval(tick); $('clock').textContent = `${Math.round(doc.meta.seconds)} s`;
  finish(); $('review').disabled = false;
}

load(false).then(d => { doc = d; renderCase(); showMode(false); $('review').onclick = review; });
