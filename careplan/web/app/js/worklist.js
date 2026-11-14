// The doctor's day: every visit, and where its plan review stands.
import { $, $$, api, cap, date, h, plural, setPlanner } from './ui.js';

function status(c, planner, samples) {
  const r = c.review;
  if (!r) return h`<span class="chip none">Not reviewed</span>${c.has_recording ? h`<span class="hint">saved run available</span>`
    : samples ? h`<span class="hint">sample review</span>` : planner.answers ? '' : h`<span class="hint" title="The planner is not answering: ${planner.why}">needs the planner</span>`}`;
  if (!r.flagged) return h`<span class="chip likely_ok">Reviewed, no flags</span>`;
  return h`<span class="chip ${r.decided === r.flagged ? 'likely_ok' : 'at_risk'}">${r.flagged} of ${r.items} flagged</span>
    <span class="hint">${r.decided === r.flagged ? 'all decided' : `${r.decided} of ${r.flagged} decided`}</span>`;
}

export async function worklist(root) {
  const { cases, planner, samples } = await api('cases');
  setPlanner(planner);
  const reviewed = cases.filter(c => c.review).length, saved = cases.filter(c => c.has_recording).length;
  // Visits that can be reviewed right now without the planner (a saved real run exists) come first.
  cases.sort((a, b) => (b.has_recording - a.has_recording) || a.case_id.localeCompare(b.case_id));
  root.innerHTML = h`<div class="page">
    <div class="page-head">
      <div><h1>Visits</h1>
        <p class="muted">Before the patient leaves, check the plan against what was said, what is in the chart, and where they live.</p></div>
      <div class="page-tools"><span class="muted">${plural(cases.length, 'visit')} · ${saved} with a saved run · ${reviewed} reviewed</span>
        <input id="find" type="search" placeholder="Find a patient or clinic" aria-label="Find a patient or clinic"></div>
    </div>
    <div class="list" role="list">
      <div class="row head" aria-hidden="true"><span>Patient</span><span>Visit</span><span>Plan</span><span>Review</span></div>
      ${cases.map(c => h`<a class="row" role="listitem" href="#/visit/${c.case_id}" data-find="${`${c.name} ${c.setting} ${c.problems.join(' ')}`.toLowerCase()}">
        <span><b>${c.name}</b><small>${[c.age, c.sex, c.language !== 'English' && c.language].filter(Boolean).join(' · ')}</small></span>
        <span>${cap(c.setting)}<small>${c.problems.join(' · ')}</small></span>
        <span>${plural(c.plan_items, 'item')}<small>${date(c.date)}</small></span>
        <span class="st">${status(c, planner, samples)}</span>
      </a>`)}
      <div class="row empty" hidden>No visit matches.</div>
    </div>
  </div>`;
  $('#find', root).addEventListener('input', e => {
    const q = e.target.value.trim().toLowerCase();
    let any = false;
    $$('a.row', root).forEach(a => { a.hidden = !a.dataset.find.includes(q); any = any || !a.hidden; });
    $('.row.empty', root).hidden = any;
  });
}
