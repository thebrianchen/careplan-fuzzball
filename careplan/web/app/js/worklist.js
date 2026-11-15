// The doctor's day: every visit, and where its plan review stands.
import { $, $$, api, cap, date, h, plural, setPlanner } from './ui.js';
import { avatar } from './visit_layout.js';

function status(c) {
  const r = c.review;
  if (!r) return h`<span class="chip none">Not reviewed</span>`;
  if (!r.flagged) return h`<span class="chip likely_ok">Reviewed, no flags</span>`;
  return h`<span class="chip ${r.decided === r.flagged ? 'likely_ok' : 'at_risk'}">${r.flagged} of ${r.items} flagged</span>`;
}

export async function worklist(root) {
  const { cases, planner, samples } = await api('cases');
  setPlanner(planner);
  const reviewed = cases.filter(c => c.review).length, saved = cases.filter(c => c.has_recording).length;
  // Visits that can be reviewed right now without the planner (a saved real run exists) come first.
  cases.sort((a, b) => (b.has_recording - a.has_recording) || a.case_id.localeCompare(b.case_id));
  root.innerHTML = h`<div class="page">
    <div class="page-head">
      <div><h1>Visits</h1></div>
      <div class="page-tools"><span class="muted">${plural(cases.length, 'visit')} · ${saved} with a saved run · ${reviewed} reviewed</span>
        <input id="find" type="search" placeholder="Find a patient or clinic" aria-label="Find a patient or clinic"></div>
    </div>
    <div class="patient-grid" role="list">
      ${cases.map(c => h`<a class="patient-card" role="listitem" href="#/visit/${c.case_id}" data-find="${`${c.name} ${c.setting} ${c.problems.join(' ')}`.toLowerCase()}">
        <div class="patient-card-body">
          <div class="patient-card-head"><div class="patient-identity">${avatar()}<div><h2>${c.name}</h2><small>${[c.age != null && `Age ${c.age}`, c.sex, c.language !== 'English' && c.language].filter(Boolean).join(' · ')}</small></div></div>
            <span class="patient-status">${status(c)}</span></div>
          <div class="patient-visit"><b>${cap(c.setting)}</b><small>${date(c.date)}</small></div>
          <p class="patient-problems">${c.problems.join(' · ')}</p>
        </div><div class="patient-card-footer"><span>${plural(c.plan_items, 'plan item')}</span><span>${!c.review ? c.has_recording ? 'Saved run available' : samples ? 'Sample review available' : 'Needs review' : [c.review.flagged ? `${c.review.decided} of ${c.review.flagged} decided` : '', c.review.source === 'sample' ? 'Sample' : ''].filter(Boolean).join(' · ')}</span></div>
      </a>`)}
      <div class="patient-empty" hidden>No visit matches.</div>
    </div>
  </div>`;
  $('#find', root).addEventListener('input', e => {
    const q = e.target.value.trim().toLowerCase();
    let any = false;
    $$('a.patient-card', root).forEach(a => { a.hidden = !a.dataset.find.includes(q); any = any || !a.hidden; });
    $('.patient-empty', root).hidden = any;
  });
}
