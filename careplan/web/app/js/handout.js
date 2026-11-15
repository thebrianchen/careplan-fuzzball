// The patient handout, built from the reviewed plan and the doctor's decisions.
// Two versions: a draft assembled on the page from the review (no model needed), and the plain-language version in the
// patient's language, which the planner writes (POST /summarize/<case>, the app's existing route).
import { $, api, date, h, hours, setPlanner } from './ui.js';
import { repeats } from './visit.js';

function draftSteps(it, dec) {
  if (it.status === 'likely_ok' || (dec && dec.decision === 'dismiss')) return ['Keep doing this as planned.'];
  const alts = it.alternatives.map(a => a.text + (a.resource ? ` (${[a.resource.name, hours(a.resource.hours), a.resource.phone].filter(Boolean).join(', ')})` : ''));
  if (dec && dec.note) return dec.note.split('\n');   // the doctor's own wording, or the alternatives the doctor chose
  return alts;
}

export async function handout(root, id) {
  let doc = await api(`case/${id}`);
  setPlanner(doc.planner);
  const lang = doc.patient.handout_language;

  function paint(message) {
    if (!doc.items.length) {
      root.innerHTML = h`<div class="page narrow"><a class="back" href="#/visit/${id}">← Back to the visit</a>
        <div class="notice">The plan has not been reviewed yet. <a href="#/visit/${id}">Review it first</a>, then prepare the handout.</div></div>`;
      return;
    }
    const w = doc.handout;
    const sections = w
      ? w.items.map(s => ({ heading: s.heading || (doc.plan.find(x => x.item_id === s.item_id) || {}).text, steps: s.steps }))
      : doc.items.filter(it => !repeats(doc, doc.plan).includes(it.item_id)).map(it => ({ heading: it.plan_text, steps: draftSteps(it, doc.decisions[it.item_id]) }));
    root.innerHTML = h`<div class="page narrow handout">
      <div class="handout-bar">
        <a class="back" href="#/visit/${id}">← Back to the visit</a>
        <div class="actions">
          <button type="button" id="write">${w ? 'Write it again' : `Write it in plain ${lang}`}</button>
          <button type="button" id="print" class="primary">Print</button>
        </div>
      </div>
      ${message ? h`<div class="notice bad">${message}</div>` : ''}
      <article class="sheet">
        <h1>Your care plan</h1>
        <p class="lede">Prepared after your visit on ${date(doc.visit.date)}. Bring this with you.</p>
        ${sections.map(s => h`<h2>${s.heading}</h2>${s.steps.map(x => h`<p class="stepline">${x}</p>`)}`)}
        ${w && w.closing ? h`<p class="closing">${w.closing}</p>` : ''}
        <p class="foot">Questions? Call the clinic.</p>
      </article>
    </div>`;
    $('#print', root).onclick = () => window.print();
    $('#write', root).onclick = async e => {
      e.target.disabled = true; e.target.textContent = 'Writing…';
      const pl = (await api('system')).planner;          // ask first: a planner that is down would keep this waiting for minutes
      setPlanner(pl);
      if (!pl.answers) return paint(`The planner is not answering: ${pl.why}. This step needs it; the draft below is unchanged.`);
      const r = await fetch(`/summarize/${id}`, { method: 'POST' }).catch(() => null);
      if (r && r.ok) { doc.handout = await r.json(); return paint(); }
      const why = r ? String((await r.json().catch(() => ({}))).error || '').slice(0, 160) : '';
      paint(`The plain-language version was not written${why ? ` (${why})` : ''}. This step needs the planner; the draft below is unchanged.`);
    };
  }
  paint();
}
