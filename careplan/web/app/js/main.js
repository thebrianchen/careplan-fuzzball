// Care Plan Check: the clinic app. Hash routes, one view at a time; each view may return a cleanup function.
//   #/                 the day's visits (doctor)
//   #/visit/<case>     one visit: review the plan, inspect the evidence, decide
//   #/handout/<case>   the patient handout built from the reviewed plan
//   #/clinic?b=&order=&t=  the whole panel by neighborhood (clinic manager); the query is optional
//   #/system           what the operator sees: planner, boundary, reviews, decisions
import { $, $$, h } from './ui.js';
import { worklist } from './worklist.js';
import { visit } from './visit.js';
import { handout } from './handout.js';
import { system } from './system.js';
import { clinic } from './clinic.js';

const ROUTES = [
  [/^#\/?$/, worklist, 'visits'],
  [/^#\/visit\/([\w-]+)$/, visit, 'visits'],
  [/^#\/handout\/([\w-]+)$/, handout, 'visits'],
  [/^#\/clinic(?:\?(.*))?$/, clinic, 'clinic'],
  [/^#\/system$/, system, 'system'],
];
let cleanup = null, turn = 0;

async function route() {
  const mine = ++turn, root = $('#view'), hash = location.hash || '#/';
  if (cleanup) { cleanup(); cleanup = null; }
  const hit = ROUTES.map(([re, view, nav]) => [re.exec(hash), view, nav]).find(([m]) => m);
  if (!hit) { location.hash = '#/'; return; }
  const [m, view, nav] = hit;
  $$('header nav a').forEach(a => a.classList.toggle('on', a.dataset.nav === nav));
  window.scrollTo(0, 0);
  try {
    const done = await view(root, ...m.slice(1));
    if (mine === turn) cleanup = done || null; else if (done) done();
  } catch (err) {
    console.error(err);
    if (mine === turn) root.innerHTML = h`<div class="page"><div class="notice bad"><b>This page could not load.</b> ${err.message}
      <div class="muted">The app needs its server: <code>python -m careplan.serve</code>, then open <code>/app/</code>.</div></div></div>`;
  }
}
// Projector mode (stronger contrast, larger small text): a class on <html>, kept across reloads. Shift+P switches it.
function stage(on) {
  document.documentElement.classList.toggle('stage', on);
  localStorage.setItem('cp.stage', on ? '1' : '');
  window.dispatchEvent(new Event('cp:stage'));
}
window.addEventListener('keydown', e => {
  if (e.key === 'P' && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.target.closest?.('input, textarea, select, [contenteditable]')) stage(!document.documentElement.classList.contains('stage'));
});
stage(localStorage.getItem('cp.stage') === '1');
window.addEventListener('hashchange', route);
route();
