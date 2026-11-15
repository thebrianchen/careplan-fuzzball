// The operator's view (and the judges'): is the planner up, what crossed the boundary, what was reviewed, what the
// doctors decided. Case ids and counts only: no patient fields are sent to this page. Refreshes every few seconds.
//   GET /api/app/system     GET /redteam/live (leak-test counts, the app's existing route)
import { $, api, cap, date, h, plural, secs, setPlanner, timeOfDay } from './ui.js';

const median = xs => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor((s.length - 1) / 2)] : null; };
const num = n => n == null ? '—' : Number(n).toLocaleString();
const uptime = s => s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
const SOURCE = { live: 'Live', recorded: 'Saved run', sample: 'Sample' };

function tiles(s) {
  const ok = s.runs.filter(r => r.ok), live = ok.filter(r => r.source === 'live'), failed = s.runs.filter(r => !r.ok).length;
  const med = median(live.map(r => r.seconds).filter(x => x != null));
  const dec = { accept: 0, edit: 0, dismiss: 0 };
  s.decisions.forEach(d => { if (d.decision in dec) dec[d.decision]++; });
  return h`
    <div class="tile"><span class="k">Reviews</span><b>${ok.length}${failed ? h` <span class="bad">· ${failed} failed</span>` : ''}</b></div>
    <div class="tile"><span class="k">Live review time</span><b>${med == null ? '—' : secs(med)}</b></div>
    <div class="tile"><span class="k">Requests to the public side</span><b>${s.crossings.length}</b></div>
    <div class="tile"><span class="k">Clinician decisions</span><b>${dec.accept} accepted · ${dec.edit} edited · ${dec.dismiss} dismissed</b></div>`;
}

const table = (head, rows, empty) => h`<table class="grid"><thead><tr>${head.map(x => h`<th>${x}</th>`)}</tr></thead>
  <tbody>${rows.length ? rows : h`<tr><td colspan="${head.length}" class="muted">${empty}</td></tr>`}</tbody></table>`;

export async function system(root) {
  root.innerHTML = h`<div class="page system">
    <div class="page-head"><div><h1>System</h1></div>
      <div class="page-tools"><span class="muted" id="asof"></span></div></div>
    <div class="tiles" id="tiles"></div>
    <section class="card" hidden><h2>Leak test</h2><div id="leak" class="muted">Checking…</div></section>
    <section class="card sruns"><h2>Reviews</h2><div id="runs"></div></section>
    <div class="two">
      <section class="card"><h2>Requests to the public side</h2><div id="crossings"></div></section>
      <section class="card"><h2>Clinician decisions</h2><div id="decisions"></div></section>
    </div>
    <section class="card"><h2>Activity</h2><div id="activity"></div></section>
  </div>`;
  const el = sel => $(sel, root);

  async function refresh() {
    let s;
    try { s = await api('system'); } catch (err) { el('#asof').textContent = `Not refreshed: ${err.message}`; return; }
    setPlanner(s.planner);
    el('#asof').textContent = `As of ${timeOfDay(s.now)} · ${plural(s.config.cases, 'visit')} loaded · rule signals ${s.config.signals ? 'on' : 'off'}`;
    el('#tiles').innerHTML = tiles(s);
    el('#runs').innerHTML = table(['When', 'Visit', 'Source', 'Planner', 'Time', 'Model turns', 'Tokens in / out', 'Lookups', 'Crossings', 'Flagged', 'Result'],
      s.runs.map(r => h`<tr><td>${timeOfDay(r.ts)}</td><td class="mono"><a href="#/visit/${r.case_id}">${r.case_id}</a></td><td><span class="runtag ${r.source}">${SOURCE[r.source] || r.source}</span></td>
        <td class="mono">${r.profile}</td><td>${secs(r.seconds)}</td><td>${r.steps ?? '—'}</td><td>${num(r.prompt_tokens)} / ${num(r.completion_tokens)}</td>
        <td>${r.tool_calls ?? '—'}</td><td>${r.crossings ?? '—'}</td><td>${r.ok ? `${r.flagged} of ${r.items}` : '—'}</td>
        <td>${r.ok ? h`<span class="chip likely_ok">Finished</span>` : h`<span class="chip blocked" title="${r.error}">Did not finish</span>`}</td></tr>`),
      'No review yet on this server. Open a visit and review its plan.');
    el('#crossings').innerHTML = table(['Visit', 'Census tract', 'Need', 'Days', 'Time window', 'Answered by'],
      s.crossings.map(c => h`<tr><td class="mono">${c.case_id}</td><td class="mono">${c.tract_id}</td><td>${c.need}</td><td>${c.days.length === 7 ? 'Every day' : c.days.join(' ') || 'Any'}</td>
        <td>${c.start || c.end ? `${c.start || ''}–${c.end || ''}` : 'Any'}</td><td>${c.via_gateway ? 'Gateway' : 'Local table'}${c.source === 'recorded' ? ' (in the saved run)' : ''}</td></tr>`),
      'Nothing has been asked of the public side.');
    el('#decisions').innerHTML = table(['When', 'Visit', 'Item', 'Decision', 'Clinician\'s wording'],
      s.decisions.map(d => h`<tr><td>${date(d.ts)} ${timeOfDay(d.ts)}</td><td class="mono">${d.case_id}</td><td class="mono">${d.item_id}</td>
        <td><span class="dec ${d.decision}">${d.decision}</span></td><td>${d.note || ''}</td></tr>`),
      'No decision has been logged.');
    el('#activity').innerHTML = table(['When', 'What', 'Visit', 'Detail'],
      s.activity.map(a => h`<tr><td>${timeOfDay(a.ts)}</td><td>${a.event}</td><td class="mono">${a.case_id || ''}</td>
        <td class="muted">${Object.entries(a).filter(([k, v]) => !['ts', 'event', 'case_id'].includes(k) && v != null).map(([k, v]) => `${k.replace('_', ' ')}: ${v}`).join(' · ')}</td></tr>`),
      'Nothing has happened since this server started.');
  }
  async function leak() {
    try {
      const r = await (await fetch('/redteam/live')).json(), a = r.single_agent, b = r.split;
      el('#leak').closest('.card').hidden = !(a.runs || b.runs);     // shown only when there are results to show
      el('#leak').innerHTML = a.runs || b.runs
        ? h`<div class="leak"><div><b class="${a.leaked ? 'bad' : ''}">${a.leaked} of ${a.runs}</b><span>runs leaked patient identifiers with one agent holding the chart and the web</span></div>
            <div><b class="${b.leaked ? 'bad' : 'good'}">${b.leaked} of ${b.runs}</b><span>runs leaked with the split design</span></div></div>
            `
        : 'No leak-test results on this machine. The counts appear here while a leak test writes its results.';
    } catch (err) { el('#leak').textContent = 'The leak-test counts could not be read.'; }
  }
  await refresh(); leak();
  const timers = [setInterval(refresh, 4000), setInterval(leak, 10000)];
  return () => timers.forEach(clearInterval);
}
