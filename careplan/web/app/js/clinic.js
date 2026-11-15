// The clinic manager's view: every plan in the panel checked, counted by the patient's neighborhood (census tract).
// Counts only: no patient reaches this page, and a neighborhood with fewer than k patients is not in the data at all.
//   GET /api/app/panel -> {panel: null | P}     GET /api/app/basemap -> {basemap: null | B, wide: null | W} (drawn by clinic_map.js)
// The view lives in the address: #/clinic?b=<barrier>&order=share&t=<tract>&p=<place>, kept current with replaceState.
// The map's center and zoom are the map's own and stay out of the address.
import { $, api, cap, date, h, plural } from './ui.js';
import { countyBar } from './exhibits.js';
import { HELPS, PLACE_KINDS, hoodName, nameOf, pct, tractName } from './clinic_words.js';
import { clinicMap, pinKey, placeKind } from './clinic_map.js';
import { gaps } from './clinic_gaps.js';
import { publicView } from './clinic_public.js';

// The public figures that speak to a barrier come first in a neighborhood's detail when that barrier is chosen.
const FACTS = {
  food_access: ['tract:places_foodinsecu', 'tract:lila_half_10', 'tract:lila_1_10'],
  transport: ['tract:no_vehicle_share', 'tract:places_lacktrpt', 'tract:early_service_stops'],
  utilities: ['tract:places_shututility'],
  housing: ['tract:places_housinsecu'],
};
const TOP = 8;
const LEADERS = 2;                     // the barriers flagged most are marked in the list; which ones is the data's call
const topOf = s => s.full ? TOP : 0;   // the first view has no tract ranking, so the map carries no rank badges
const FEW = 10;            // below this many reviewed patients one or two flags swing the share
const ORDERS = { count: 'Most flags', share: 'Highest share' };

const who = t => t.reviewed === t.patients ? plural(t.patients, 'patient') : `${plural(t.patients, 'patient')}, ${t.reviewed} reviewed`;

// The one measure the page reads: all barriers -> patients with any flag; one barrier -> that barrier's flags; both per reviewed patient.
const measure = (t, barrier) => {
  const n = barrier ? t.barriers[barrier] || 0 : t.flagged_patients;
  return { n, of: t.reviewed, share: t.reviewed ? n / t.reviewed : 0 };
};
const BY = {
  count: (a, b) => b.m.n - a.m.n || b.m.share - a.m.share,
  share: (a, b) => b.m.share - a.m.share || b.m.of - a.m.of,
};
const ranked = (P, barrier, order) => P.tracts.map(t => ({ t, m: measure(t, barrier) }))
  .sort((a, b) => BY[order](a, b) || a.t.tract_id.localeCompare(b.t.tract_id));
// tract: null follows the top row of the current ranking; a tract the manager picked stays until Clear.
const selected = (P, s) => s.tract || ranked(P, s.barrier, s.order)[0].t.tract_id;
// The located places that could help with a barrier, or null when that kind of help has no locations.
const placesFor = (P, barrier) => P.barriers.find(b => b.barrier === barrier)?.places ? P.places?.[barrier] || [] : null;
const nearText = n => n ? `${plural(n, 'place')} nearby` : 'no place nearby';

// A place stays open only while its barrier is chosen and its kind is shown.
const fix = (P, s, hidden) => ({ ...s, place: (placesFor(P, s.barrier) || []).some(p => p.id === s.place && !hidden.has(p.kind)) ? s.place : null });
function readAddress(P, query) {
  const q = new URLSearchParams(query || ''), b = q.get('b'), t = q.get('t');
  return fix(P, {
    full: q.get('full') === '1',
    barrier: P.barriers.some(x => x.barrier === b) ? b : null,
    order: q.get('order') === 'share' ? 'share' : 'count',
    tract: P.tracts.some(x => x.tract_id === t) ? t : null,
    place: q.get('p'),
  }, new Set());
}
function address(s) {
  const q = new URLSearchParams();
  if (s.full) q.set('full', '1');
  if (s.barrier) q.set('b', s.barrier);
  if (s.order !== 'count') q.set('order', s.order);
  if (s.tract) q.set('t', s.tract);
  if (s.place) q.set('p', s.place);
  return `#/clinic${q.toString() ? `?${q}` : ''}`;
}

// Shade: 0 = nothing flagged, then four steps of whole percents from the lowest share (to the 5% below) up to the highest.
function shades(P, barrier) {
  const all = P.tracts.map(t => measure(t, barrier).share * 100);
  const lo = Math.floor(Math.min(...all) / 5) * 5, hi = Math.max(lo + 1, Math.ceil(Math.max(...all)));
  const step = Math.ceil((hi - lo) / 4), edge = i => Math.min(hi, lo + i * step);
  const bin = m => m.n === 0 ? 0 : Math.min(4, Math.max(1, Math.ceil((m.share * 100 - lo) / step - 1e-9)));
  const labels = ['None', lo ? `${lo}–${edge(1)}%` : `up to ${edge(1)}%`, ...[1, 2, 3].map(i => `${edge(i)}–${edge(i + 1)}%`)];
  return { bin, labels, none: all.some(x => x === 0) };
}
const measureText = barrier => barrier ? 'flags per reviewed patient' : 'reviewed patients with any flag';

let basemapData = null;                              // the street maps (~2.5 MB), fetched once per page load
const basemap = () => basemapData || (basemapData = api('basemap').then(d => ({ B: d.basemap ?? null, W: d.wide ?? null }), err => {
  console.warn(`The street map could not load, so neighborhoods are drawn as dots: ${err.message}`);
  basemapData = null;
  return { B: null, W: null };
}));

function legend(P, state, hidden) {
  const b = state.barrier, sh = shades(P, b), pts = placesFor(P, b), chosen = P.barriers.find(x => x.barrier === b);
  const kinds = pts ? Object.keys(PLACE_KINDS).map(k => [k, pts.filter(p => p.kind === k).length]).filter(([, n]) => n) : [];
  return h`<div class="lg-shade-row"><span class="k">Shade: share, ${b ? `${nameOf(chosen).toLowerCase()} ` : ''}${measureText(b)}</span>
      <span class="lg-shade">${sh.labels.map((l, i) => i || sh.none ? h`<span><i class="b${i}"></i>${l}</span>` : '')}<span class="lg-out"><i></i>No shading: outside the panel</span></span></div>
    ${kinds.length ? h`<div class="lg-pins"><span class="k">${plural(pts.length, 'place')} that could help with ${HELPS[b]}</span>
      <span class="lg-kinds">${kinds.map(([k, n]) => h`<button type="button" data-kind="${k}" aria-pressed="${String(!hidden.has(k))}" title="${hidden.has(k) ? 'Show' : 'Hide'} these on the map">${pinKey(k)}${placeKind(k)} <b>${n}</b></button>`)}</span></div>` : ''}
    <div class="lg-keys"><span><b class="rank-key">1</b>Top ${Math.min(topOf(state), P.tracts.length)} in the list, by ${ORDERS[state.order].toLowerCase()}</span>
      <span><i class="clinic-key"></i>The clinic</span></div>`;
}

function barChart(P, barrier) {
  const most = Math.max(...P.barriers.map(b => b.flags));
  return h`<button type="button" class="ball" data-barrier="" aria-pressed="${String(!barrier)}">All barriers</button>
    <ol class="bbars">${P.barriers.map((b, i) => h`<li class="${i < LEADERS ? 'lead' : ''}"><button type="button" class="bbar" data-barrier="${b.barrier}" aria-pressed="${String(b.barrier === barrier)}">
      <span class="bl">${nameOf(b)}</span><span class="bt"><i style="width:${(b.flags / most * 100).toFixed(1)}%"></i></span><span class="bn">${b.flags}</span></button></li>`)}</ol>`;
}

function rankList(P, state, sel) {
  const b = state.barrier, rows = ranked(P, b, state.order), near = placesFor(P, b) && b;
  const shown = rows.slice(0, topOf(state)), pos = rows.findIndex(x => x.t.tract_id === sel);
  const row = ({ t, m }, i) => h`<li><button type="button" class="rrow${t.tract_id === sel ? ' on' : ''}" data-tract="${t.tract_id}" aria-pressed="${String(t.tract_id === sel)}">
    <span class="rn">${i + 1}</span><span class="rt">${t.area || tractName(t.tract_id)}
      <small>${t.area ? `${tractName(t.tract_id)} · ` : ''}${who(t)}${state.order === 'share' && t.reviewed < FEW ? h`<em class="few">few patients</em>` : ''}${near ? h`<span class="near${t.places_near[near] ? '' : ' none'}">${nearText(t.places_near[near])}</span>` : ''}</small></span>
    <span class="rc"><b>${m.n} of ${m.of}</b> ${pct(m.share)}</span><span class="rb"><i style="width:${(m.share * 100).toFixed(1)}%"></i></span></button></li>`;
  const sizes = P.tracts.map(t => t.reviewed), what = b ? 'flags' : 'patients with a flag';
  const how = state.order === 'count'
    ? `Each row is a census tract. Ranked by the number of ${what}; tracts have ${Math.min(...sizes)} to ${Math.max(...sizes)} reviewed patients, so the larger ones gather more ${b ? 'flags' : 'of them'}.`
    : `Each row is a census tract. Ranked by share; "few patients" marks a tract with fewer than ${FEW} reviewed, where a few ${b ? 'flags' : 'patients'} make a high share.`;
  return h`<div class="seg rorder" role="group" aria-label="Rank by">${Object.entries(ORDERS).map(([k, l]) =>
      h`<button type="button" data-order="${k}" class="${k === state.order ? 'on' : ''}" aria-pressed="${String(k === state.order)}">${l}</button>`)}</div>
    <ol class="rank">${shown.map(row)}${pos >= topOf(state) ? h`<li class="gap" aria-hidden="true">…</li>${row(rows[pos], pos)}` : ''}</ol>
    <p class="hint">${how}${b ? ' A patient can carry more than one flag.' : ''}</p>`;
}

// The panel by neighborhood, for the current barrier: the line a presenter can say aloud.
function areaStrip(P, barrier) {
  const of = a => barrier ? a.barriers[barrier] || 0 : a.flagged_patients, share = a => a.reviewed ? of(a) / a.reviewed : 0;
  const rows = [...(P.areas || [])].sort((x, y) => share(y) - share(x)), most = Math.max(0, ...rows.map(share));
  return h`${rows.map((a, i) => h`<li class="carea${i === 0 && most ? ' lead' : ''}"><b>${a.name}</b>
    <span class="carea-bar"><i style="width:${most ? (share(a) / most * 100).toFixed(1) : 0}%"></i></span>
    <span>${of(a)} of ${a.reviewed} · ${pct(share(a))}</span></li>`)}`;
}

// What the map shows for the current barrier and order, in numbers taken straight from the panel.
function says(P, state) {
  const b = state.barrier, rows = ranked(P, b, state.order), [{ t, m }] = rows;
  const key = state.order === 'count' ? x => x.m.n : x => x.m.share;
  const tied = rows.filter(x => key(x) === key(rows[0])).length - 1;
  const lead = `${hoodName(t)} has the ${state.order === 'count' ? 'most' : 'highest share of'}`, tie = tied ? `, tied with ${plural(tied, 'other tract')}` : '';
  if (!b) {
    const top = P.barriers.reduce((a, x) => x.flags > a.flags ? x : a);
    return `${nameOf(top)} is the barrier flagged most: ${top.flags} flags across ${P.reviewed} reviewed plans. ${lead} patients with a flag${tie}: ${m.n} of ${m.of} reviewed (${pct(m.share)}).`;
  }
  const chosen = P.barriers.find(x => x.barrier === b), near = placesFor(P, b) && t.places_near[b];
  const help = near == null ? '' : near
    ? ` ${plural(near, 'place')} that could help with ${HELPS[b]} ${near === 1 ? 'is' : 'are'} within a mile.`
    : ` No place that could help with ${HELPS[b]} is within a mile.`;
  return `${lead} ${nameOf(chosen).toLowerCase()} flags${tie}: ${m.n} among ${m.of} reviewed patients (${pct(m.share)}).${help}`;
}

function detail(P, state, sel) {
  const t = P.tracts.find(x => x.tract_id === sel);
  const order = state.barrier ? [...P.barriers.filter(b => b.barrier === state.barrier), ...P.barriers.filter(b => b.barrier !== state.barrier)] : P.barriers;
  const firstFacts = FACTS[state.barrier] || [];
  const facts = [...t.facts.filter(f => firstFacts.includes(f.ref)), ...t.facts.filter(f => !firstFacts.includes(f.ref))];
  const helps = order.filter(b => b.places && t.places_near[b.barrier] != null);
  const any = measure(t, null);
  return h`<div class="dhead"><div><h2>${t.area || tractName(t.tract_id)}</h2><small>${t.area ? `${tractName(t.tract_id)} · ` : ''}Census tract ${t.tract_id}</small></div>
      ${state.tract ? h`<button type="button" class="link" data-clear title="Go back to the top of the list">Clear</button>` : h`<small>Top of the list</small>`}</div>
    <div class="dcounts">
      <span><b>${t.patients}</b>patients</span>
      <span><b>${any.n} of ${any.of}</b>reviewed with a flag</span>
      <span><b>${t.appointments}</b>appointments</span>
      <span><b>${t.no_shows}</b>no-shows</span>
    </div>
    <h3>Flags here, per reviewed patient</h3>
    <ol class="dbars">${order.map(b => { const m = measure(t, b.barrier);
      return h`<li class="${b.barrier === state.barrier ? 'on' : ''}"><span>${nameOf(b)}</span><span class="bt"><i style="width:${Math.min(100, m.share * 100).toFixed(1)}%"></i></span><span class="bn">${m.n} of ${m.of}</span></li>`; })}</ol>
    ${helps.length ? h`<h3>Help nearby</h3><ul class="dhelp">${helps.map(b => h`<li class="${b.barrier === state.barrier ? 'on' : ''}"><b>${t.places_near[b.barrier]}</b> ${t.places_near[b.barrier] === 1 ? 'place' : 'places'} that could help with ${HELPS[b.barrier] || nameOf(b).toLowerCase()} within a mile</li>`)}</ul>` : ''}
    <h3>Public figures for this tract</h3>
    ${facts.map(f => h`<div class="rowline kv${firstFacts.includes(f.ref) ? ' rel' : ''}"><span>${cap(f.label)}${f.note ? h`<small class="warn">${f.note}</small>` : ''}</span><b>${f.value}</b>${countyBar(f, { compact: true })}</div>`)}
    <p class="hint">These figures describe the neighborhood, not any patient.</p>`;
}

function head(P) {
  const tag = P.source === 'sample'
    ? h`<span class="runtag sample">Sample</span><span>Counts from the patients' answer keys, not model reviews.</span>`
    : h`<span class="runtag reviews">${P.reviewed} of ${P.patients} plans reviewed</span><span>Counted ${date(P.built)}, ${new Date(P.built).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}.</span>`;
  return h`<div class="page-head clinic-head"><div><h1>Clinic</h1>
      <p class="muted">Every care plan in the clinic's panel, checked and counted by the patient's neighborhood, to show where plans fail and why. No patient is shown.</p>
      <p class="cfacts"><b>${P.patients}</b> patients · <b>${P.reviewed}</b> reviewed · <b>${P.tracts.length}</b> census tracts${P.areas?.length ? h` in <b>${P.areas.length}</b> neighborhoods` : ''}</p></div>
    <div class="runline csource">${tag}</div></div>`;
}

export async function clinic(root, query) {
  const [{ panel: P }, { B, W }] = await Promise.all([api('panel'), basemap()]);
  if (!P) {
    root.innerHTML = h`<div class="page narrow"><div class="page-head"><h1>Clinic</h1></div>
      <div class="notice"><b>This server has no panel results.</b><div class="muted">The clinic view counts reviews over the whole panel. None have been published on this server yet.</div></div></div>`;
    return;
  }
  root.innerHTML = h`<div class="page clinic">
    ${head(P)}
    <div class="cgrid">
      <section class="card cbars"><h2>Barriers flagged</h2><p class="hint">Flags across ${P.reviewed} reviewed plans</p><div id="bars"></div><h3 class="k careas-h" id="areas-title"></h3><ol class="careas" id="areas"></ol></section>
      <section class="card cmapcard"><h2 id="map-title"></h2><p class="csays" id="says"></p>
        <figure class="cmap"><div class="cmap-stack"></div>${B || W ? h`<figcaption class="cmap-src">Map: ${(B || W).source}</figcaption>` : ''}</figure>
        <div class="legend" id="legend"></div>
        <p class="cfrom">This map is built from what clinicians' plans ran into in visits, not from public data alone. Only counts left the patient side.</p></section>
      <section class="card crank"><h2 id="rank-title"></h2><div id="rank"></div></section>
      <section class="card cdetail" id="detail" aria-live="polite"></section>
    </div>
    <div class="cgrid2">
      <section class="card cgaps" id="gaps"></section>
      <section class="card cpublic" id="public"></section>
    </div>
    <button type="button" class="link cwhole" id="whole"></button></div>`;
  const el = sel => $(sel, root);
  const byId = Object.fromEntries(P.tracts.map(t => [t.tract_id, t]));
  const hidden = new Set();                            // kinds of place the manager switched off in the legend, for this visit only
  const unfolded = new Set();                          // the cards under the map that the manager opened in full
  let state = readAddress(P, query);
  const set = patch => { state = fix(P, { ...state, ...patch }, hidden); paint(); };
  const map = clinicMap(el('.cmap-stack'), { basemap: B, wide: W, tracts: P.tracts, clinic: P.clinic, pick: tract => set({ tract }), place: place => set({ place }) });
  const tip = id => {
    const t = byId[id], m = measure(t, state.barrier), chosen = P.barriers.find(x => x.barrier === state.barrier);
    return h`<b>${t.area ? `${t.area} · ` : ''}${tractName(id)}</b> · ${who(t)}<br>${chosen ? nameOf(chosen) : 'Any flag'}: <b>${m.n} of ${m.of}</b> (${pct(m.share)})`;
  };

  function paint() {
    const b = state.barrier, chosen = P.barriers.find(x => x.barrier === b), sh = shades(P, b), sel = selected(P, state);
    if (location.hash.startsWith('#/clinic') && location.hash !== address(state)) history.replaceState(null, '', address(state));
    el('.page').classList.toggle('focus', !state.full);
    el('#areas-title').textContent = chosen ? `${nameOf(chosen)} by neighborhood` : 'Patients with a flag, by neighborhood';
    el('#whole').textContent = state.full ? 'Show less' : 'Show everything';
    el('#bars').innerHTML = barChart(P, b);
    el('#map-title').textContent = chosen ? `${nameOf(chosen)}: where flags fall` : 'Where flagged plans live';
    el('#says').textContent = says(P, state);
    el('#areas').innerHTML = areaStrip(P, b);
    el('#rank-title').textContent = chosen ? `Where ${nameOf(chosen).toLowerCase()} clusters` : 'Where flagged patients cluster';
    map.update({
      bins: Object.fromEntries(P.tracts.map(t => [t.tract_id, sh.bin(measure(t, b))])),
      ranks: ranked(P, b, state.order).slice(0, topOf(state)).map(x => x.t.tract_id),
      selected: sel, places: (placesFor(P, b) || []).filter(p => !hidden.has(p.kind)), place: state.place, tip,
    });
    el('#legend').innerHTML = legend(P, state, hidden);
    el('#rank').innerHTML = rankList(P, state, sel);
    el('#detail').innerHTML = detail(P, state, sel);
    for (const [id, html] of [['gaps', gaps(P, state, sel)], ['public', publicView(P, state, sel)]]) {
      const more = unfolded.has(id);
      el(`#${id}`).classList.toggle('folded', !more);
      el(`#${id}`).innerHTML = h`${html}<button type="button" class="link cmore" data-more="${id}" aria-expanded="${String(more)}">${more ? 'Show less' : 'Show more'}</button>`;
    }
    el('.page').classList.toggle('one-barrier', !!b);
  }

  el('.page').addEventListener('click', e => {
    const bar = e.target.closest('[data-barrier]'), order = e.target.closest('[data-order]'), hood = e.target.closest('[data-tract]');
    const go = e.target.closest('[data-go-barrier],[data-go-tract]');     // a card below the map sends the manager to a view of it
    const kind = e.target.closest('.lg-kinds [data-kind]');
    if (e.target.closest('#whole')) set({ full: !state.full });
    else if (go) {
      set({ barrier: go.dataset.goBarrier ?? state.barrier, tract: go.dataset.goTract ?? state.tract });
      el('.cgrid').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else if (kind) {
      if (!hidden.delete(kind.dataset.kind)) hidden.add(kind.dataset.kind);
      set({});
    } else if (bar) set({ barrier: bar.dataset.barrier && bar.dataset.barrier !== state.barrier ? bar.dataset.barrier : null });
    else if (order) set({ order: order.dataset.order });
    else if (e.target.closest('[data-more]')) { const id = e.target.closest('[data-more]').dataset.more; unfolded.has(id) ? unfolded.delete(id) : unfolded.add(id); paint(); }
    else if (e.target.closest('[data-clear]')) set({ tract: null });
    else if (hood) set({ tract: hood.dataset.tract });
  });
  paint();
  return () => map.destroy();
}
