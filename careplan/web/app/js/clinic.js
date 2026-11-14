// The clinic manager's view: every plan in the panel checked, counted by the patient's neighborhood (census tract).
// Counts only: no patient reaches this page, and a neighborhood with fewer than k patients is not in the data at all.
//   GET /api/app/panel -> {panel: null | P}     GET /api/app/city (the map's backdrop)
// The view lives in the address: #/clinic?b=<barrier>&order=share&t=<tract>, kept current with replaceState.
import { $, $$, api, cap, date, h, plural } from './ui.js';
import { at, countyBar, mount, viewAttr, viewOf } from './exhibits.js';

const LABEL = { med_cost: 'Medication cost', transport: 'Transportation', language_literacy: 'Language or literacy' };
const HELPS = { food_access: 'food', med_cost: 'medication costs', utilities: 'utility bills' };   // "places that could help with ..."
// The public figures that speak to a barrier come first in a neighborhood's detail when that barrier is chosen.
const FACTS = {
  food_access: ['tract:places_foodinsecu', 'tract:lila_half_10', 'tract:lila_1_10'],
  transport: ['tract:no_vehicle_share', 'tract:places_lacktrpt', 'tract:early_service_stops'],
  utilities: ['tract:places_shututility'],
  housing: ['tract:places_housinsecu'],
};
const TOP = 8;
const FEW = 10;            // below this many reviewed patients one or two flags swing the share
const ORDERS = { count: 'Most flags', share: 'Highest share' };

const nameOf = b => LABEL[b.barrier] || cap(b.label);
const tractName = id => `Tract ${+id.slice(5, 9)}${id.slice(9) === '00' ? '' : `.${id.slice(9)}`}`;
const pct = x => `${Math.round(x * 100)}%`;
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

function readAddress(P, query) {
  const q = new URLSearchParams(query || ''), b = q.get('b'), t = q.get('t');
  return {
    barrier: P.barriers.some(x => x.barrier === b) ? b : null,
    order: q.get('order') === 'share' ? 'share' : 'count',
    tract: P.tracts.some(x => x.tract_id === t) ? t : null,
  };
}
function address(s) {
  const q = new URLSearchParams();
  if (s.barrier) q.set('b', s.barrier);
  if (s.order !== 'count') q.set('order', s.order);
  if (s.tract) q.set('t', s.tract);
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
const r1 = v => Math.round(v * 10) / 10;
const pin = (x, y, k) => `M${r1(x)} ${r1(y)}l${r1(-3 * k)} ${r1(-5.2 * k)}a${r1(3.5 * k)} ${r1(3.5 * k)} 0 1 1 ${r1(6 * k)} 0z`;   // a teardrop, tip on the place

let cityData = null;                                 // the backdrop (~0.5 MB), fetched once per page load
const city = () => cityData || (cityData = api('city'));

// Circle AREA is proportional to patients. The core is the box's own size in px, so marks and text keep their size on screen.
function geometry(P, w) {
  const hgt = Math.round(Math.min(Math.max(w * 0.8, 300), 540));
  const most = Math.max(...P.tracts.map(t => t.patients)), rmax = Math.max(22, Math.min(40, w * 0.07));
  const r = n => rmax * Math.sqrt(n / most);
  const pad = rmax + 8;
  const view = viewOf([...P.tracts, ...(P.clinic ? [P.clinic] : [])], { w, h: hgt, padX: pad, padY: pad, minKm: 1 });
  return { w, h: hgt, view, r, most };
}

function mapMarks(P, g) {
  const circles = [...P.tracts].sort((a, b) => b.patients - a.patients).map(t => {
    const [x, y] = at(g.view, t), r = g.r(t.patients);
    return h`<g class="hood" data-tract="${t.tract_id}" data-x="${r1(x)}" data-y="${r1(y)}" data-r="${r1(r)}" tabindex="0" role="button" aria-label="${tractName(t.tract_id)}">
      <circle class="dot" cx="${r1(x)}" cy="${r1(y)}" r="${r1(r)}"/>${r < 9 ? h`<circle class="hit" cx="${r1(x)}" cy="${r1(y)}" r="9"/>` : ''}</g>`;
  });
  const c = P.clinic && at(g.view, P.clinic);
  const clinic = c ? h`<g class="clinic" aria-hidden="true"><path class="lead"/><rect x="${r1(c[0] - 5)}" y="${r1(c[1] - 5)}" width="10" height="10" rx="2.5"/>
    <path d="M${r1(c[0])} ${r1(c[1] - 2.8)}v5.6M${r1(c[0] - 2.8)} ${r1(c[1])}h5.6"/>
    <text data-x="${r1(c[0])}" data-y="${r1(c[1])}">Clinic</text></g>` : '';
  return h`${circles}<g class="pins"></g><circle class="sel" r="0"/>${clinic}<g class="ranks" aria-hidden="true"></g>`;
}

function legend(P, g, state) {
  const b = state.barrier, sizes = [...new Set([Math.min(...P.tracts.map(t => t.patients)), median(P.tracts.map(t => t.patients)), g.most])];
  const big = g.r(g.most), bottom = 2 * big + 6;
  let ly = -Infinity;
  const rings = [...sizes].reverse().map(n => { const r = g.r(n), top = bottom - 2 * r; ly = Math.max(top, ly + 11);
    return h`<circle cx="${r1(big + 1)}" cy="${r1(bottom - r)}" r="${r1(r)}"/><path d="M${r1(big + 1)} ${r1(top)}H${r1(2 * big + 5)}L${r1(2 * big + 10)} ${r1(ly)}"/><text x="${r1(2 * big + 13)}" y="${r1(ly + 3.5)}">${n}</text>`; });
  const sh = shades(P, b), pts = placesFor(P, b), chosen = P.barriers.find(x => x.barrier === b);
  const ranks = Math.min(TOP, P.tracts.length);
  return h`<div class="lg lg-sizes"><span class="k">Size: patients</span>
      <svg width="${Math.ceil(2 * big + 34)}" height="${Math.ceil(bottom + 2)}" class="lg-size" aria-hidden="true">${rings}</svg></div>
    <div class="lg lg-rest">
      <div><span class="k">Shade: share, ${b ? `${nameOf(chosen).toLowerCase()} ` : ''}${measureText(b)}</span>
        <span class="lg-shade">${sh.labels.map((l, i) => i || sh.none ? h`<span><i class="b${i}"></i>${l}</span>` : '')}</span></div>
      <div class="lg-keys">
        <span><b class="rank-key">1</b>Top ${ranks} in the list, by ${ORDERS[state.order].toLowerCase()}</span>
        ${pts ? h`<span><svg class="pin-key" width="9" height="12" aria-hidden="true"><path class="pin" d="${pin(4.5, 11, 1)}"/></svg>${plural(pts.length, 'place')} that could help with ${HELPS[b]}</span>` : ''}
        <span><i class="clinic-key"></i>The clinic</span></div></div>`;
}
const median = xs => { const s = [...xs].sort((a, b) => a - b); return s[Math.floor((s.length - 1) / 2)]; };

function barChart(P, barrier) {
  const most = Math.max(...P.barriers.map(b => b.flags));
  return h`<button type="button" class="ball" data-barrier="" aria-pressed="${!barrier}">All barriers</button>
    <ol class="bbars">${P.barriers.map(b => h`<li><button type="button" class="bbar" data-barrier="${b.barrier}" aria-pressed="${b.barrier === barrier}">
      <span class="bl">${nameOf(b)}</span><span class="bt"><i style="width:${(b.flags / most * 100).toFixed(1)}%"></i></span><span class="bn">${b.flags}</span></button></li>`)}</ol>`;
}

function rankList(P, state, sel) {
  const b = state.barrier, rows = ranked(P, b, state.order), near = placesFor(P, b) && b;
  const shown = rows.slice(0, TOP), pos = rows.findIndex(x => x.t.tract_id === sel);
  const row = ({ t, m }, i) => h`<li><button type="button" class="rrow${t.tract_id === sel ? ' on' : ''}" data-tract="${t.tract_id}" aria-pressed="${t.tract_id === sel}">
    <span class="rn">${i + 1}</span><span class="rt">${tractName(t.tract_id)}
      <small>${who(t)}${state.order === 'share' && t.reviewed < FEW ? h`<em class="few">few patients</em>` : ''}${near ? h`<span class="near${t.places_near[near] ? '' : ' none'}">${nearText(t.places_near[near])}</span>` : ''}</small></span>
    <span class="rc"><b>${m.n} of ${m.of}</b> ${pct(m.share)}</span><span class="rb"><i style="width:${(m.share * 100).toFixed(1)}%"></i></span></button></li>`;
  const sizes = P.tracts.map(t => t.reviewed), what = b ? 'flags' : 'patients with a flag';
  const how = state.order === 'count'
    ? `Ranked by the number of ${what}; ties go to the higher share. Neighborhoods have ${Math.min(...sizes)} to ${Math.max(...sizes)} reviewed patients, so the larger ones gather more ${b ? 'flags' : 'of them'}.`
    : `Ranked by share; ties go to the larger neighborhood. "Few patients" marks a neighborhood with fewer than ${FEW} reviewed, where a few ${b ? 'flags' : 'patients'} make a high share.`;
  return h`<div class="seg rorder" role="group" aria-label="Rank by">${Object.entries(ORDERS).map(([k, l]) =>
      h`<button type="button" data-order="${k}" class="${k === state.order ? 'on' : ''}" aria-pressed="${k === state.order}">${l}</button>`)}</div>
    <ol class="rank">${shown.map(row)}${pos >= TOP ? h`<li class="gap" aria-hidden="true">…</li>${row(rows[pos], pos)}` : ''}</ol>
    <p class="hint">${how}${b ? ' A patient can carry more than one flag.' : ''}</p>`;
}

// What the map shows for the current barrier and order, in numbers taken straight from the panel.
function says(P, state) {
  const b = state.barrier, rows = ranked(P, b, state.order), [{ t, m }] = rows;
  const key = state.order === 'count' ? x => x.m.n : x => x.m.share;
  const tied = rows.filter(x => key(x) === key(rows[0])).length - 1;
  const lead = `${tractName(t.tract_id)} has the ${state.order === 'count' ? 'most' : 'highest share of'}`, tie = tied ? `, tied with ${plural(tied, 'other neighborhood')}` : '';
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
  return h`<div class="dhead"><div><h2>${tractName(t.tract_id)}</h2><small>Census tract ${t.tract_id}</small></div>
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
      <p class="cfacts"><b>${P.patients}</b> made-up patients · <b>${P.reviewed}</b> reviewed · <b>${P.tracts.length}</b> neighborhoods shown · ${plural(P.hidden_tracts, 'neighborhood')} with fewer than ${P.k} patients ${P.hidden_tracts === 1 ? 'is' : 'are'} not shown</p></div>
    <div class="runline csource">${tag}</div></div>`;
}

export async function clinic(root, query) {
  const { panel: P } = await api('panel');
  if (!P) {
    root.innerHTML = h`<div class="page narrow"><div class="page-head"><h1>Clinic</h1></div>
      <div class="notice"><b>This server has no panel results.</b><div class="muted">The clinic view counts reviews over the whole panel. None have been published on this server yet.</div></div></div>`;
    return;
  }
  root.innerHTML = h`<div class="page clinic">
    ${head(P)}
    <div class="cgrid">
      <section class="card cbars"><h2>Barriers flagged</h2><p class="hint">Flags across ${P.reviewed} reviewed plans. Choose one to see where it clusters.</p><div id="bars"></div></section>
      <section class="card cmapcard"><h2 id="map-title"></h2><p class="csays" id="says"></p>
        <figure class="exmap cmap"><div class="exmap-box cmap-box"></div><div class="cmap-tip" role="status" hidden></div></figure>
        <div class="legend" id="legend"></div></section>
      <section class="card crank"><h2 id="rank-title"></h2><div id="rank"></div></section>
      <section class="card cdetail" id="detail" aria-live="polite"></section>
    </div></div>`;
  const el = sel => $(sel, root), box = el('.cmap-box');
  const g = geometry(P, Math.round(box.clientWidth));
  box.style.aspectRatio = `${g.w} / ${g.h}`;
  box.innerHTML = h`<canvas data-view="${viewAttr(g.view)}" aria-hidden="true"></canvas>
    <svg viewBox="0 0 ${g.w} ${g.h}" preserveAspectRatio="xMidYMid meet" role="group" aria-label="Map of the panel's neighborhoods">${mapMarks(P, g)}</svg>`;
  mount(root, city);
  const byId = Object.fromEntries(P.tracts.map(t => [t.tract_id, t]));
  let state = readAddress(P, query);
  const set = patch => { state = { ...state, ...patch }; paint(); };

  function paint() {
    const b = state.barrier, chosen = P.barriers.find(x => x.barrier === b), sh = shades(P, b), sel = selected(P, state);
    if (location.hash.startsWith('#/clinic') && location.hash !== address(state)) history.replaceState(null, '', address(state));
    el('#bars').innerHTML = barChart(P, b);
    el('#map-title').textContent = chosen ? `${nameOf(chosen)}: where flags fall` : 'Where flagged plans live';
    el('#says').textContent = says(P, state);
    el('#rank-title').textContent = chosen ? `Where ${nameOf(chosen).toLowerCase()} clusters` : 'Where flagged patients cluster';
    $$('.hood', box).forEach(n => {
      const m = measure(byId[n.dataset.tract], b);
      n.dataset.bin = sh.bin(m);
      n.classList.toggle('on', n.dataset.tract === sel);
    });
    const pts = placesFor(P, b) || [], k = g.w < 500 ? 0.8 : 1;
    $('.pins', box).innerHTML = h`${pts.map(p => { const [x, y] = at(g.view, p); return h`<path class="pin" d="${pin(x, y, k)}"><title>${p.name}</title></path>`; })}`;
    const nums = ranked(P, b, state.order).slice(0, TOP).map(({ t }, i) => { const n = $(`.hood[data-tract="${t.tract_id}"]`, box);
      return { x: +n.dataset.x, y: +n.dataset.y + (t.tract_id === P.clinic?.tract_id ? -11 : 4), i }; });
    $('.ranks', box).innerHTML = h`${nums.map(p => h`<text x="${p.x}" y="${p.y}">${p.i + 1}</text>`)}`;
    placeClinicLabel(nums);
    const ring = $('.sel', box), on = $(`.hood[data-tract="${sel}"]`, box);
    ring.setAttribute('cx', on.dataset.x); ring.setAttribute('cy', on.dataset.y); ring.setAttribute('r', Math.max(+on.dataset.r, 6) + 2.5);
    el('#legend').innerHTML = legend(P, g, state);
    el('#rank').innerHTML = rankList(P, state, sel);
    el('#detail').innerHTML = detail(P, state, sel);
    el('.page').classList.toggle('one-barrier', !!b);
  }

  // The clinic sits among the densest circles, so its label goes where it covers the fewest circles and rank numbers,
  // on a short leader line when it has to move away from the marker.
  function placeClinicLabel(nums) {
    const lab = $('.clinic text', box);
    if (!lab) return;
    const x = +lab.dataset.x, y = +lab.dataset.y, w = 36, hh = 14;
    const hoods = $$('.hood', box).map(n => [+n.dataset.x, +n.dataset.y, +n.dataset.r]);
    const near = (qx, qy, ax, ay, bx, by) => {   // distance from a point to the segment a-b
      const f = Math.max(0, Math.min(1, ((qx - ax) * (bx - ax) + (qy - ay) * (by - ay)) / ((bx - ax) ** 2 + (by - ay) ** 2 || 1)));
      return Math.hypot(qx - ax - f * (bx - ax), qy - ay - f * (by - ay));
    };
    const hits = (l, t) => hoods.filter(([cx, cy, r]) => Math.hypot(cx - Math.max(l, Math.min(cx, l + w)), cy - Math.max(t, Math.min(cy, t + hh))) < r).length;
    const best = [9, 28, 46].flatMap(d => [0, 180, -40, -140, 40, 140, -90, 90].map(a => {
      const dx = Math.round(Math.cos(a * Math.PI / 180) * d), dy = Math.round(Math.sin(a * Math.PI / 180) * d);
      const px = x + dx, py = y + dy, l = dx > 4 ? px : dx < -4 ? px - w : px - w / 2, t = py - hh / 2;
      const out = l < 2 || t < 2 || l + w > g.w - 2 || t + hh > g.h - 2;
      const crossed = d > 9 && nums.some(p => near(p.x, p.y - 4, x, y, px, py) < 7);
      const score = hits(l, t) + 3 * nums.filter(p => p.x > l - 6 && p.x < l + w + 6 && p.y > t - 2 && p.y < t + hh + 10).length + (crossed ? 3 : 0) + (out ? 99 : 0) + d / 100;
      return { px, py, l, t, d, a, score };
    })).sort((p, q) => p.score - q.score)[0];
    lab.setAttribute('x', r1(best.l)); lab.setAttribute('y', r1(best.t + 11)); lab.setAttribute('text-anchor', 'start');
    const a = best.a * Math.PI / 180;
    $('.clinic .lead', box).setAttribute('d', best.d > 9 ? `M${r1(x + 7 * Math.cos(a))} ${r1(y + 7 * Math.sin(a))}L${r1(best.px)} ${r1(best.py)}` : '');
  }

  const tip = el('.cmap-tip');
  function showTip(n) {
    const t = byId[n.dataset.tract], m = measure(t, state.barrier), chosen = P.barriers.find(x => x.barrier === state.barrier);
    tip.innerHTML = h`<b>${tractName(t.tract_id)}</b> · ${who(t)}<br>${chosen ? nameOf(chosen) : 'Any flag'}: <b>${m.n} of ${m.of}</b> (${pct(m.share)})`;
    tip.hidden = false;
    const s = box.clientWidth / g.w, half = tip.offsetWidth / 2;
    const x = Math.min(Math.max(+n.dataset.x * s, half + 4), box.clientWidth - half - 4);
    const above = (+n.dataset.y - +n.dataset.r) * s - 8;
    tip.classList.toggle('below', above < tip.offsetHeight + 4);
    tip.style.left = `${x}px`;
    tip.style.top = `${above < tip.offsetHeight + 4 ? (+n.dataset.y + +n.dataset.r) * s + 8 : above}px`;
  }
  const svg = $('svg', box);
  svg.addEventListener('pointerover', e => { const n = e.target.closest('.hood'); if (n) showTip(n); });
  svg.addEventListener('pointerleave', () => { tip.hidden = true; });
  svg.addEventListener('focusin', e => { const n = e.target.closest('.hood'); if (n) showTip(n); });
  svg.addEventListener('focusout', () => { tip.hidden = true; });
  svg.addEventListener('keydown', e => {
    const n = e.target.closest('.hood');
    if (n && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); set({ tract: n.dataset.tract }); n.focus(); }
  });
  el('.page').addEventListener('click', e => {
    const bar = e.target.closest('[data-barrier]'), order = e.target.closest('[data-order]'), hood = e.target.closest('[data-tract]');
    if (bar) set({ barrier: bar.dataset.barrier && bar.dataset.barrier !== state.barrier ? bar.dataset.barrier : null });
    else if (order) set({ order: order.dataset.order });
    else if (e.target.closest('[data-clear]')) set({ tract: null });
    else if (hood) set({ tract: hood.dataset.tract });
  });
  paint();
}
