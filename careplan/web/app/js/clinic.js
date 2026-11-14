// The clinic manager's view: every plan in the panel checked, counted by the patient's neighborhood (census tract).
// Counts only: no patient reaches this page, and a neighborhood with fewer than k patients is not in the data at all.
//   GET /api/app/panel -> {panel: null | P}     GET /api/app/city (the map's backdrop)
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

const nameOf = b => LABEL[b.barrier] || cap(b.label);
const tractName = id => `Tract ${+id.slice(5, 9)}${id.slice(9) === '00' ? '' : `.${id.slice(9)}`}`;
const pct = x => `${Math.round(x * 100)}%`;
const who = t => t.reviewed === t.patients ? plural(t.patients, 'patient') : `${plural(t.patients, 'patient')}, ${t.reviewed} reviewed`;

// The one measure the page reads: all barriers -> patients with any flag; one barrier -> that barrier's flags; both per reviewed patient.
const measure = (t, barrier) => {
  const n = barrier ? t.barriers[barrier] || 0 : t.flagged_patients;
  return { n, of: t.reviewed, share: t.reviewed ? n / t.reviewed : 0 };
};
const ranked = (P, barrier) => P.tracts.map(t => ({ t, m: measure(t, barrier) }))
  .sort((a, b) => b.m.share - a.m.share || b.m.of - a.m.of || a.t.tract_id.localeCompare(b.t.tract_id));

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
  const r1 = v => Math.round(v * 10) / 10;
  const circles = [...P.tracts].sort((a, b) => b.patients - a.patients).map(t => {
    const [x, y] = at(g.view, t), r = g.r(t.patients);
    return h`<g class="hood" data-tract="${t.tract_id}" data-x="${r1(x)}" data-y="${r1(y)}" data-r="${r1(r)}" tabindex="0" role="button" aria-label="${tractName(t.tract_id)}">
      <circle class="dot" cx="${r1(x)}" cy="${r1(y)}" r="${r1(r)}"/>${r < 9 ? h`<circle class="hit" cx="${r1(x)}" cy="${r1(y)}" r="9"/>` : ''}</g>`;
  });
  const c = P.clinic && at(g.view, P.clinic);
  const clinic = c ? h`<g class="clinic" aria-hidden="true"><rect x="${r1(c[0] - 6.5)}" y="${r1(c[1] - 6.5)}" width="13" height="13" rx="3"/>
    <path d="M${r1(c[0])} ${r1(c[1] - 3.6)}v7.2M${r1(c[0] - 3.6)} ${r1(c[1])}h7.2"/>
    <text data-x="${r1(c[0])}" data-y="${r1(c[1])}">Clinic</text></g>` : '';
  return h`${circles}<circle class="sel" r="0" hidden/>${clinic}<g class="ranks" aria-hidden="true"></g>`;
}

function legend(P, g, barrier) {
  const sizes = [...new Set([Math.min(...P.tracts.map(t => t.patients)), median(P.tracts.map(t => t.patients)), g.most])];
  const big = g.r(g.most), wd = sizes.reduce((s, n) => s + 2 * g.r(n) + 10, 0);
  let x = 0;
  const rings = sizes.map(n => { const r = g.r(n); x += r; const cx = x; x += r + 10;
    return h`<circle cx="${cx.toFixed(1)}" cy="${(2 * big - r).toFixed(1)}" r="${r.toFixed(1)}"/><text x="${cx.toFixed(1)}" y="${(2 * big + 14).toFixed(1)}">${n}</text>`; });
  const sh = shades(P, barrier);
  return h`<div class="lg"><span class="k">Size: patients</span>
      <svg width="${Math.ceil(wd)}" height="${Math.ceil(2 * big + 18)}" class="lg-size" aria-hidden="true">${rings}</svg></div>
    <div class="lg"><span class="k">Shade: ${measureText(barrier)}</span>
      <span class="lg-shade">${sh.labels.map((l, i) => i || sh.none ? h`<span><i class="b${i}"></i>${l}</span>` : '')}</span></div>
    <div class="lg lg-clinic"><span><i class="clinic-key"></i>The clinic</span></div>`;
}
const median = xs => { const s = [...xs].sort((a, b) => a - b); return s[Math.floor((s.length - 1) / 2)]; };

function barChart(P, barrier) {
  const most = Math.max(...P.barriers.map(b => b.flags));
  return h`<button type="button" class="ball" data-barrier="" aria-pressed="${!barrier}">All barriers</button>
    <ol class="bbars">${P.barriers.map(b => h`<li><button type="button" class="bbar" data-barrier="${b.barrier}" aria-pressed="${b.barrier === barrier}">
      <span class="bl">${nameOf(b)}</span><span class="bt"><i style="width:${(b.flags / most * 100).toFixed(1)}%"></i></span><span class="bn">${b.flags}</span></button></li>`)}</ol>`;
}

function rankList(P, state) {
  const rows = ranked(P, state.barrier);
  const shown = rows.slice(0, TOP), pos = rows.findIndex(x => x.t.tract_id === state.tract);
  const row = ({ t, m }, i) => h`<li><button type="button" class="rrow${t.tract_id === state.tract ? ' on' : ''}" data-tract="${t.tract_id}" aria-pressed="${t.tract_id === state.tract}">
    <span class="rn">${i + 1}</span><span class="rt">${tractName(t.tract_id)}<small>${who(t)}</small></span>
    <span class="rc"><b>${m.n} of ${m.of}</b> ${pct(m.share)}</span><span class="rb"><i style="width:${(m.share * 100).toFixed(1)}%"></i></span></button></li>`;
  const sizes = P.tracts.map(t => t.reviewed);
  return h`<ol class="rank">${shown.map(row)}${pos >= TOP ? h`<li class="gap" aria-hidden="true">…</li>${row(rows[pos], pos)}` : ''}</ol>
    <p class="hint">Ranked by share; ties go to the larger neighborhood. Neighborhoods have ${Math.min(...sizes)} to ${Math.max(...sizes)} reviewed patients, so on a small one a few ${state.barrier ? 'flags' : 'patients'} make a high share.${state.barrier ? ' A patient can carry more than one flag.' : ''}</p>`;
}

function detail(P, state) {
  const t = P.tracts.find(x => x.tract_id === state.tract);
  if (!t) return h`<h2>Neighborhood</h2><p class="hint">Choose a circle on the map or a row in the list to see that neighborhood's counts, its barriers and its public figures.</p>`;
  const order = state.barrier ? [...P.barriers.filter(b => b.barrier === state.barrier), ...P.barriers.filter(b => b.barrier !== state.barrier)] : P.barriers;
  const firstFacts = FACTS[state.barrier] || [];
  const facts = [...t.facts.filter(f => firstFacts.includes(f.ref)), ...t.facts.filter(f => !firstFacts.includes(f.ref))];
  const helps = order.filter(b => b.places && t.places_near[b.barrier] != null);
  const any = measure(t, null);
  return h`<div class="dhead"><div><h2>${tractName(t.tract_id)}</h2><small>Census tract ${t.tract_id}</small></div>
      <button type="button" class="link" data-tract="${t.tract_id}" title="Clear the choice">Clear</button></div>
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

export async function clinic(root) {
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
      <section class="card cmapcard"><h2 id="map-title"></h2>
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
  let state = { barrier: null, tract: null };
  const set = patch => { state = { ...state, ...patch }; paint(); };

  function paint() {
    const b = state.barrier, chosen = P.barriers.find(x => x.barrier === b), sh = shades(P, b);
    el('#bars').innerHTML = barChart(P, b);
    el('#map-title').textContent = chosen ? `${nameOf(chosen)}: where flags fall` : 'Where flagged plans live';
    el('#rank-title').textContent = chosen ? `Where ${nameOf(chosen).toLowerCase()} clusters` : 'Where flagged patients cluster';
    $$('.hood', box).forEach(n => {
      const m = measure(byId[n.dataset.tract], b);
      n.dataset.bin = sh.bin(m);
      n.classList.toggle('on', n.dataset.tract === state.tract);
    });
    const nums = ranked(P, b).slice(0, TOP).map(({ t }, i) => { const n = $(`.hood[data-tract="${t.tract_id}"]`, box);
      return { x: +n.dataset.x, y: +n.dataset.y + (t.tract_id === P.clinic?.tract_id ? -11 : 4), i }; });
    $('.ranks', box).innerHTML = h`${nums.map(p => h`<text x="${p.x}" y="${p.y}">${p.i + 1}</text>`)}`;
    placeClinicLabel(nums);
    const sel = $('.sel', box), on = state.tract && $(`.hood[data-tract="${state.tract}"]`, box);
    sel.toggleAttribute('hidden', !on);
    if (on) { sel.setAttribute('cx', on.dataset.x); sel.setAttribute('cy', on.dataset.y); sel.setAttribute('r', Math.max(+on.dataset.r, 6) + 2.5); }
    el('#legend').innerHTML = legend(P, g, b);
    el('#rank').innerHTML = rankList(P, state);
    el('#detail').innerHTML = detail(P, state);
    el('.page').classList.toggle('one-barrier', !!b);
  }

  // The clinic's label takes the first side of its marker where it covers none of the ranked circles' numbers.
  function placeClinicLabel(nums) {
    const lab = $('.clinic text', box);
    if (!lab) return;
    const x = +lab.dataset.x, y = +lab.dataset.y, w = 40;
    const sides = [[x + 11, y + 4, 'start', x + 11], [x - 11, y + 4, 'end', x - 11 - w], [x, y - 12, 'middle', x - w / 2], [x, y + 21, 'middle', x - w / 2]];
    const clear = ([, ty, , left]) => !nums.some(p => p.x > left - 8 && p.x < left + w + 8 && p.y > ty - 20 && p.y < ty + 12);
    const [tx, ty, anchor] = sides.find(clear) || sides[0];
    lab.setAttribute('x', tx); lab.setAttribute('y', ty); lab.setAttribute('text-anchor', anchor);
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
    if (n && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); set({ tract: state.tract === n.dataset.tract ? null : n.dataset.tract }); n.focus(); }
  });
  el('.page').addEventListener('click', e => {
    const bar = e.target.closest('[data-barrier]'), hood = e.target.closest('[data-tract]');
    if (bar) set({ barrier: bar.dataset.barrier && bar.dataset.barrier !== state.barrier ? bar.dataset.barrier : null });
    else if (hood) set({ tract: state.tract === hood.dataset.tract ? null : hood.dataset.tract });
  });
  paint();
}
