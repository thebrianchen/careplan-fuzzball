// The clinic page's map: the panel's neighborhoods as their census-tract outlines over a quiet street map, with pins
// for the places that could help. It owns only the view (center and zoom); everything it shows comes from the page.
//   const map = clinicMap(stack, {basemap, tracts, clinic, pick, place})   stack: an empty element sized by CSS
//     basemap  B from GET /api/app/basemap, or null (each neighborhood is then a small circle at its center)
//     pick(id) the manager chose a neighborhood on the map; place(id | null) a pin's card was opened or closed
//   map.update({bins, ranks, selected, places, place, tip})   after every page paint
//     bins {tract_id: 0..4}; ranks: tract ids in rank order; places: the pins to show; tip(tract_id) -> h`...`
//   map.destroy()
// View: {x, y, k} is the center in km east and south of the box's middle, and px per km. Screen = size/2 + (km - center) * k.
import { $, $$, cap, h, hours, raw } from './ui.js';
import { PLACE_KINDS, tractName } from './clinic_words.js';

const KM = 111.32, K_MAX = 1400, PAD = 18, TINY = 18;
const ease = t => t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
// A style value that changes with zoom, linear in log(k) between stops [[k, value], ...].
const ramp = stops => k => {
  if (k <= stops[0][0]) return stops[0][1];
  for (let i = 1; i < stops.length; i++) if (k <= stops[i][0]) {
    const [k0, a] = stops[i - 1], [k1, b] = stops[i], f = Math.log(k / k0) / Math.log(k1 / k0);
    return a + (b - a) * f;
  }
  return stops.at(-1)[1];
};

// Bottom to top. `from` picks the basemap's features; `keep` filters them; `min` is the zoom (px per km) a layer starts at. A `label` names a street where its straight stretch
// on screen is at least `need(k)` times the name plus `room`, so at low zoom only the long through streets get names.
const LAYERS = [
  { name: 'water', from: 'water', fill: '#d8e5ec' },
  { name: 'tracts', from: 'tracts', stroke: '#d9dee3', width: ramp([[60, 0.6], [600, 1]]) },
  { name: 'lanes', from: 'streets', keep: f => !f.name || f.name === 'Alley', min: 150, stroke: '#e1e5e9', width: ramp([[60, 0.35], [300, 0.8], [1200, 2.5]]), alpha: ramp([[60, 0.5], [250, 1]]) },
  { name: 'streets', from: 'streets', keep: f => f.name && f.name !== 'Alley', stroke: '#cfd5db', width: ramp([[60, 0.5], [300, 1.3], [1200, 4]]), alpha: ramp([[60, 0.7], [250, 1]]),
    label: { font: '10.5px', color: '#68737f', room: 26, need: ramp([[70, 4.5], [230, 1.6], [460, 1]]) } },
  { name: 'rail', from: 'rail', stroke: '#a9b0b8', width: ramp([[60, 0.9], [600, 1.6]]), dash: [5, 4] },
  { name: 'arterials', from: 'arterials', stroke: '#b9c1c9', width: ramp([[60, 1.6], [300, 2.6], [1200, 6]]),
    label: { font: '600 11px', color: '#56616d', room: 40, need: () => 1.4 } },
  { name: 'highways', from: 'highways', casing: '#aeb6bf', stroke: '#fff', width: ramp([[60, 2.6], [300, 4.5], [1200, 9]]),
    label: { font: '600 11.5px', color: '#4a5562', room: 40, need: () => 1.4 } },
];
const FADE = ramp([[180, 1], [700, 0.62]]);      // zoomed in to a few blocks, the streets matter more than the shade
const FONT = '-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif';

// Pin glyphs centered on the place, about 10 px across at scale 1. Filled and hollow pairs share a barrier.
const GLYPH = {
  snap_grocery: '<circle r="4.6"/>',
  food_pantry: '<rect class="open" x="-4.1" y="-4.1" width="8.2" height="8.2" rx="1.2"/>',
  fqhc: '<path d="M-1.8-5.2h3.6v3.4h3.4v3.6h-3.4v3.4h-3.6v-3.4h-3.4v-3.6h3.4z"/>',
  warming_center: '<path d="M0-5.6L5.3 3.8H-5.3z"/>',
  cooling_center: '<path class="open" d="M0-5.4L4.8 0 0 5.4-4.8 0z"/>',
};
export const placeKind = kind => cap((PLACE_KINDS[kind] || [kind])[0]);   // 'food_pantry' -> 'Food pantry'
export const pinKey = kind => h`<svg class="pin-key" viewBox="-7 -7 14 14" width="14" height="14" aria-hidden="true"><g class="glyph">${raw(GLYPH[kind])}</g></svg>`;

// 'Mon 08:30-20:00; Tue 08:30-20:00; Wed 08:30-20:00' -> 'Mon–Wed 8:30 AM–8:00 PM': consecutive days with the same hours as one range.
function openHours(text) {
  const days = String(text || '').split('; ').map(d => /^(\w{3}) (.+)$/.exec(d));
  if (!days.length || days.some(d => !d)) return hours(text);
  const runs = [];
  for (const [, day, time] of days) { const last = runs.at(-1); if (last?.time === time) last.to = day; else runs.push({ from: day, to: day, time }); }
  return runs.map(r => `${r.from}${r.to !== r.from ? `–${r.to}` : ''} ${hours(r.time)}`).join('; ');
}

const norm = name => name.replace(/^I- /, 'I-');
const sameStreet = name => norm(name).replace(/^[NSEW] /, '');   // "N Cicero Ave" and "Cicero Ave" are one street
const hide = name => /^State Rte /.test(name);                    // the route number of a street already named

// The basemap in km: each feature's points as a flat array, its bounds, and the straight runs a name can sit on.
function prepare(B, tracts) {
  const lat0 = B ? (B.box.south + B.box.north) / 2 : tracts.reduce((s, t) => s + t.lat, 0) / tracts.length;
  const lon0 = B ? (B.box.west + B.box.east) / 2 : tracts.reduce((s, t) => s + t.lon, 0) / tracts.length;
  const cos = Math.cos(lat0 * Math.PI / 180);
  const km = (lat, lon) => [(lon - lon0) * cos * KM, -(lat - lat0) * KM];
  const flat = pts => { const a = new Float32Array(pts.length * 2); pts.forEach(([la, lo], i) => { [a[2 * i], a[2 * i + 1]] = km(la, lo); }); return a; };
  const bounds = arrs => { const b = [Infinity, Infinity, -Infinity, -Infinity];
    for (const a of arrs) for (let i = 0; i < a.length; i += 2) { b[0] = Math.min(b[0], a[i]); b[1] = Math.min(b[1], a[i + 1]); b[2] = Math.max(b[2], a[i]); b[3] = Math.max(b[3], a[i + 1]); }
    return b; };
  const pathOf = rings => rings.map(a => { let s = ''; for (let i = 0; i < a.length; i += 2) s += `${i ? 'L' : 'M'}${a[i].toFixed(3)} ${a[i + 1].toFixed(3)}`; return s + 'Z'; }).join('');

  const shapes = tracts.map(t => {
    const c = km(t.lat, t.lon), outline = B?.tracts[t.tract_id];
    const rings = outline ? outline.map(flat)
      : [Float32Array.from({ length: 48 }, (_, i) => c[i % 2] + 0.17 * (i % 2 ? Math.sin(i * Math.PI / 24) : Math.cos(i * Math.PI / 24)))];
    return { id: t.tract_id, c, rings, d: pathOf(rings), b: bounds(rings), outline: !!outline };
  });
  const box = B ? [...km(B.box.north, B.box.west), ...km(B.box.south, B.box.east)]
    : (b => [b[0] - 0.6, b[1] - 0.6, b[2] + 0.6, b[3] + 0.6])(bounds(shapes.flatMap(s => s.rings)));

  const layers = LAYERS.map(L => {
    const src = !B ? [] : L.from === 'tracts' ? Object.entries(B.tracts).map(([id, rings]) => ({ name: id, rings }))
      : L.from === 'water' ? B.water : B[L.from].filter(f => !L.keep || L.keep(f));
    const feats = src.map(f => { const parts = (f.rings || [f.line]).map(flat); return { name: f.name, parts, b: bounds(parts) }; });
    return { ...L, tiles: tile(feats, L.fill), runs: L.label ? feats.flatMap(runsOf) : [] };
  });
  return { km, shapes, box, layers };
}

// Features grouped into a coarse grid by their middle, one Path2D per cell, so a frame strokes only the cells in view.
function tile(feats, closed) {
  const cells = new Map();
  for (const f of feats) {
    const key = `${Math.floor((f.b[0] + f.b[2]) / 2 / 1.5)},${Math.floor((f.b[1] + f.b[3]) / 2 / 1.5)}`;
    const c = cells.get(key) || cells.set(key, { path: new Path2D(), b: [Infinity, Infinity, -Infinity, -Infinity] }).get(key);
    for (const a of f.parts) {
      for (let i = 0; i < a.length; i += 2) c.path[i ? 'lineTo' : 'moveTo'](a[i], a[i + 1]);
      if (closed) c.path.closePath();
    }
    c.b = [Math.min(c.b[0], f.b[0]), Math.min(c.b[1], f.b[1]), Math.max(c.b[2], f.b[2]), Math.max(c.b[3], f.b[3])];
  }
  return [...cells.values()];
}

// The nearly straight stretches of a named line, where its name can be written along it.
function runsOf(f) {
  if (!f.name || hide(f.name) || f.name === 'Alley') return [];
  const out = [], text = norm(f.name), key = sameStreet(f.name);
  for (const a of f.parts) {
    let s = 0, dir = null;
    const close = e => {
      const len = Math.hypot(a[e] - a[s], a[e + 1] - a[s + 1]);
      if (len > 0.08) out.push({ text, key, x1: a[s], y1: a[s + 1], x2: a[e], y2: a[e + 1], len });
    };
    for (let i = 2; i < a.length; i += 2) {
      const dx = a[i] - a[i - 2], dy = a[i + 1] - a[i - 1];
      if (Math.hypot(dx, dy) < 1e-4) continue;
      const ang = Math.atan2(dy, dx);
      if (dir == null) { dir = ang; continue; }
      const turn = Math.abs(((ang - dir + 3 * Math.PI) % (2 * Math.PI)) - Math.PI);
      if (turn > 0.2) { close(i - 2); s = i - 2; dir = ang; }
    }
    close(a.length - 2);
  }
  return out;
}

let cached = null;
const prepared = (B, tracts) => cached && cached.B === B && cached.tracts === tracts ? cached.m : (cached = { B, tracts, m: prepare(B, tracts) }).m;

const overlaps = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
// The part of segment p-q inside the rectangle r, as parameters [t0, t1] along it, or null.
function clip(px, py, qx, qy, r) {
  let t0 = 0, t1 = 1;
  const dx = qx - px, dy = qy - py;
  for (const [p, q] of [[-dx, px - r[0]], [dx, r[2] - px], [-dy, py - r[1]], [dy, r[3] - py]]) {
    if (p === 0) { if (q < 0) return null; continue; }
    const t = q / p;
    if (p < 0) { if (t > t1) return null; t0 = Math.max(t0, t); } else { if (t < t0) return null; t1 = Math.min(t1, t); }
  }
  return [t0, t1];
}

// Whether segment [x1, y1, x2, y2] in px touches box [l, t, r, b].
const crosses = ([x1, y1, x2, y2], b) => {
  if (Math.max(x1, x2) < b[0] || Math.min(x1, x2) > b[2] || Math.max(y1, y2) < b[1] || Math.min(y1, y2) > b[3]) return false;
  return !!clip(x1, y1, x2, y2, b);
};

export function clinicMap(stack, { basemap, tracts, clinic, pick, place }) {
  const M = prepared(basemap, tracts), byId = Object.fromEntries(M.shapes.map(s => [s.id, s]));
  const clinicAt = clinic && M.km(clinic.lat, clinic.lon);
  stack.innerHTML = h`<canvas class="cmap-base" aria-hidden="true"></canvas>
    <svg class="cmap-fills" role="group" aria-label="Neighborhoods of the panel"><g class="km">${M.shapes.map(s =>
      h`<path class="hood${s.outline ? '' : ' dot'}" data-hood="${s.id}" d="${s.d}" tabindex="0" role="button" aria-label="${tractName(s.id)}"/>`)}</g></svg>
    <canvas class="cmap-names" aria-hidden="true"></canvas>
    <svg class="cmap-marks"><g class="km"><path class="sel-halo"/><path class="sel"/></g><g class="pins"></g><g class="ranks"></g>
      ${clinicAt ? h`<g class="clinic" aria-hidden="true"><rect x="-6" y="-6" width="12" height="12" rx="3"/><path d="M0-3.3v6.6M-3.3 0h6.6"/><text>Clinic</text></g>` : ''}</svg>
    <div class="cmap-ctl"><button type="button" data-zoom="in" aria-label="Zoom in">+</button><button type="button" data-zoom="out" aria-label="Zoom out">−</button><button type="button" data-zoom="fit" class="fit" title="Back to the whole panel">Fit</button></div>
    <div class="cmap-tip" role="status" hidden></div>
    <div class="cmap-card" role="dialog" hidden></div>`;
  stack.tabIndex = 0;
  stack.setAttribute('aria-label', 'Map. Arrow keys pan, plus and minus zoom.');
  const base = $('.cmap-base', stack), names = $('.cmap-names', stack), fills = $('.cmap-fills', stack), marks = $('.cmap-marks', stack);
  const tip = $('.cmap-tip', stack), card = $('.cmap-card', stack), hoods = Object.fromEntries($$('.hood', stack).map(n => [n.dataset.hood, n]));
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let w = 0, ht = 0, view = null, home = null, touched = false, frame = 0, flight = null, model = null, shown = {}, fromMap = null;
  let pins = [], pinKeyNow = '';

  const toPx = ([x, y], v = view) => [w / 2 + (x - v.x) * v.k, ht / 2 + (y - v.y) * v.k];
  const toKm = (px, py, v = view) => [v.x + (px - w / 2) / v.k, v.y + (py - ht / 2) / v.k];
  const bw = M.box[2] - M.box[0], bh = M.box[3] - M.box[1];
  const kMin = () => Math.min(w / bw, ht / bh);
  // The view never leaves the box: along an axis where the view is narrower than the box it stays inside, otherwise it centers.
  function clamp(v) {
    const k = Math.min(K_MAX, Math.max(kMin(), v.k)), hw = w / 2 / k, hh = ht / 2 / k;
    const fit = (c, lo, hi, half) => hi - lo <= 2 * half ? (lo + hi) / 2 : Math.min(hi - half, Math.max(lo + half, c));
    return { x: fit(v.x, M.box[0], M.box[2], hw), y: fit(v.y, M.box[1], M.box[3], hh), k };
  }
  const fitTo = (b, pad = PAD) => clamp({ x: (b[0] + b[2]) / 2, y: (b[1] + b[3]) / 2, k: Math.min((w - 2 * pad) / (b[2] - b[0] || 1e-3), (ht - 2 * pad) / (b[3] - b[1] || 1e-3)) });
  const panelBounds = () => M.shapes.reduce((b, s) => [Math.min(b[0], s.b[0]), Math.min(b[1], s.b[1]), Math.max(b[2], s.b[2]), Math.max(b[3], s.b[3])], clinicAt ? [...clinicAt, ...clinicAt] : [Infinity, Infinity, -Infinity, -Infinity]);

  const schedule = () => { if (!frame) frame = requestAnimationFrame(draw); };
  const setView = v => { view = clamp(v); schedule(); };
  function fly(to, ms = 380) {
    to = clamp(to);
    if (reduced.matches || !view) { flight = null; setView(to); return; }
    flight = { from: view, to, t0: performance.now(), ms };
    schedule();
  }
  const stop = () => { flight = null; touched = true; };
  function zoomAt(px, py, k, animate) {
    const [x, y] = toKm(px, py), k2 = Math.min(K_MAX, Math.max(kMin(), k));
    const to = { x: x - (px - w / 2) / k2, y: y - (py - ht / 2) / k2, k: k2 };
    touched = true;
    if (animate) fly(to, 260); else { flight = null; setView(to); }
  }
  // Brings a neighborhood (or a point) into view when it is off screen or too small to see, keeping the zoom when that is enough.
  function reveal(b, animate) {
    const [x0, y0] = toPx([b[0], b[1]]), [x1, y1] = toPx([b[2], b[3]]), m = 10;
    const inside = x0 >= m && y0 >= m && x1 <= w - m && y1 <= ht - m, tiny = b[2] > b[0] && Math.max(x1 - x0, y1 - y0) < TINY;
    if (inside && !tiny) return;
    const big = b[2] - b[0] > 0, k = !big ? view.k : tiny ? Math.min(w, ht) * 0.35 / Math.max(b[2] - b[0], b[3] - b[1])
      : Math.min(view.k, (w - 4 * PAD) / (b[2] - b[0]), (ht - 4 * PAD) / (b[3] - b[1]));
    const to = { x: (b[0] + b[2]) / 2, y: (b[1] + b[3]) / 2, k };
    if (animate) fly(to); else setView(to);
  }

  function draw(now) {
    frame = 0;
    if (!w || !view) return;
    if (flight) {
      const f = Math.min(1, (now - flight.t0) / flight.ms), e = ease(f), { from: a, to: b } = flight;
      view = { x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e, k: a.k * (b.k / a.k) ** e };
      if (f < 1) schedule(); else { view = b; flight = null; }
    }
    const v = view, dpr = window.devicePixelRatio || 1, tx = w / 2 - v.x * v.k, ty = ht / 2 - v.y * v.k;
    const r = [v.x - w / 2 / v.k, v.y - ht / 2 / v.k, v.x + w / 2 / v.k, v.y + ht / 2 / v.k];
    const ctx = base.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#eef1f3'; ctx.fillRect(0, 0, w, ht);
    ctx.setTransform(dpr * v.k, 0, 0, dpr * v.k, dpr * tx, dpr * ty);
    ctx.save();
    ctx.beginPath(); ctx.rect(M.box[0], M.box[1], bw, bh); ctx.fillStyle = '#fafbfb'; ctx.fill(); ctx.clip();
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    for (const L of M.layers) {
      const cells = v.k >= (L.min || 0) ? L.tiles.filter(c => overlaps(c.b, r)) : [];
      if (!cells.length) continue;
      ctx.globalAlpha = L.alpha ? L.alpha(v.k) : 1;
      if (L.fill) { ctx.fillStyle = L.fill; cells.forEach(c => ctx.fill(c.path)); continue; }
      const width = L.width(v.k) / v.k;
      ctx.setLineDash(L.dash ? L.dash.map(d => d / v.k) : []);
      if (L.casing) { ctx.strokeStyle = L.casing; ctx.lineWidth = width + 2 / v.k; cells.forEach(c => ctx.stroke(c.path)); }
      ctx.strokeStyle = L.stroke; ctx.lineWidth = width; cells.forEach(c => ctx.stroke(c.path));
    }
    ctx.restore();
    const m = `matrix(${v.k} 0 0 ${v.k} ${tx} ${ty})`;
    fills.firstElementChild.setAttribute('transform', m);
    fills.style.setProperty('--fo', FADE(v.k).toFixed(2));
    marks.firstElementChild.setAttribute('transform', m);
    const taken = placeMarks();
    label(r, taken, dpr);
    placeCard();
    stack.dataset.view = `${v.x.toFixed(3)},${v.y.toFixed(3)},${v.k.toFixed(1)}`;
  }

  // Rank badges, pins and the clinic in screen px. Returns the boxes street names must keep clear of.
  function placeMarks() {
    const taken = [];
    const ctl = $('.cmap-ctl', stack);
    taken.push([ctl.offsetLeft - 4, ctl.offsetTop - 4, ctl.offsetLeft + ctl.offsetWidth + 4, ctl.offsetTop + ctl.offsetHeight + 4]);
    const c = clinicAt && toPx(clinicAt), badges = [];
    $$('.ranks .rk', marks).forEach(g => {
      const [x0, y0] = toPx(byId[g.dataset.rk].c), clear = ([px, py]) => (!c || Math.hypot(px - c[0], py - c[1]) >= 16) && badges.every(o => Math.hypot(px - o[0] - 9, py - o[1] - 9) >= 17);
      const [x, y] = [[0, 0], [0, -17], [0, 17], [17, 0], [-17, 0], [13, -13], [-13, -13]].map(([dx, dy]) => [x0 + dx, y0 + dy]).find(clear) || [x0, y0];
      g.setAttribute('transform', `translate(${x.toFixed(1)} ${y.toFixed(1)})`);
      badges.push([x - 9, y - 9, x + 9, y + 9]);
    });
    const s = Math.min(1.15, Math.max(0.72, 0.72 + 0.2 * Math.log2(view.k / 70)));
    marks.style.setProperty('--ps', s.toFixed(2));
    const dots = pins.map(p => { const [x, y] = toPx(p.at); p.g.setAttribute('transform', `translate(${x.toFixed(1)} ${y.toFixed(1)})`); return [x - 6 * s, y - 6 * s, x + 6 * s, y + 6 * s]; });
    if (c) {
      const g = $('.clinic', marks), t = $('text', g), tw = 38, th = 15;
      g.setAttribute('transform', `translate(${c[0].toFixed(1)} ${c[1].toFixed(1)})`);
      const spots = [[10, -th / 2], [-10 - tw, -th / 2], [-tw / 2, -10 - th], [-tw / 2, 10], [9, -th - 4], [-9 - tw, -th - 4], [9, 4], [-9 - tw, 4]];
      const score = ([dx, dy]) => { const b = [c[0] + dx, c[1] + dy, c[0] + dx + tw, c[1] + dy + th];
        const out = b[0] < 2 || b[1] < 2 || b[2] > w - 2 || b[3] > ht - 2;
        return 4 * badges.filter(o => overlaps(o, b)).length + dots.filter(o => overlaps(o, b)).length + (out ? 99 : 0); };
      const best = spots.reduce((a, q) => score(q) < score(a) ? q : a);
      t.setAttribute('x', best[0]); t.setAttribute('y', best[1] + 11.5);
      taken.push([c[0] - 8, c[1] - 8, c[0] + 8, c[1] + 8], [c[0] + best[0] - 3, c[1] + best[1] - 2, c[0] + best[0] + tw + 3, c[1] + best[1] + th + 2]);
    }
    return taken.concat(badges, dots);
  }

  const widths = new Map();
  // Each street's name once, along the longest stretch of it on screen, upright, skipped where it would touch a name already written.
  function label(r, taken, dpr) {
    const ctx = names.getContext('2d'), v = view, drawn = [], seen = new Set();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, ht);
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
    const inner = [r[0] + 6 / v.k, r[1] + 6 / v.k, r[2] - 6 / v.k, r[3] - 6 / v.k];
    const sel = byId[model?.selected], edges = [];
    if (sel && overlaps(sel.b, r)) for (const a of sel.rings) for (let i = 2; i < a.length; i += 2) edges.push([...toPx([a[i - 2], a[i - 1]]), ...toPx([a[i], a[i + 1]])]);
    for (const L of [...M.layers].reverse()) {          // highways claim their room first
      if (!L.label) continue;
      const spec = L.label;
      ctx.font = `${spec.font} ${FONT}`;
      const need = spec.need(v.k);
      const cands = [];
      for (const run of L.runs) {
        if (seen.has(run.key) || run.len * v.k < 40) continue;
        const t = clip(run.x1, run.y1, run.x2, run.y2, inner);
        if (!t) continue;
        const len = (t[1] - t[0]) * run.len * v.k;
        let tw = widths.get(spec.font + run.text);
        if (tw == null) widths.set(spec.font + run.text, tw = ctx.measureText(run.text).width);
        if (len < (tw + spec.room) * need) continue;
        cands.push({ run, t, len, tw });
      }
      cands.sort((a, b) => b.len - a.len);
      for (const { run, t, len, tw } of cands) {
        if (seen.has(run.key)) continue;
        const full = run.len * v.k, step = Math.max(tw * 3, 180), a0 = t[0] * full + tw / 2 + 6, a1 = t[1] * full - tw / 2 - 6;
        const mid = (a0 + a1) / 2, snap = Math.round(mid / step) * step;
        let ang = Math.atan2(run.y2 - run.y1, run.x2 - run.x1);
        if (ang > Math.PI / 2) ang -= Math.PI; else if (ang <= -Math.PI / 2) ang += Math.PI;
        if (ang > Math.PI / 2 - 0.17) ang -= Math.PI;        // near-vertical names all read bottom to top
        const cx = Math.abs(Math.cos(ang)), sy = Math.abs(Math.sin(ang)), hw = (tw * cx + 13 * sy) / 2 + 3, hh = (tw * sy + 13 * cx) / 2 + 2;
        // the snapped middle keeps a name still while the map pans; the other spots are for when something is in the way
        const spot = [snap >= a0 && snap <= a1 ? snap : mid, a0 + (a1 - a0) * 0.25, a0 + (a1 - a0) * 0.75, a0, a1].map(at => {
          const f = at / full, x = w / 2 + (run.x1 + (run.x2 - run.x1) * f - v.x) * v.k, y = ht / 2 + (run.y1 + (run.y2 - run.y1) * f - v.y) * v.k;
          return { x, y, box: [x - hw, y - hh, x + hw, y + hh] };
        }).find(({ box }) => !taken.some(o => overlaps(o, box)) && !edges.some(e => crosses(e, box)));
        if (!spot) continue;
        const { x, y, box } = spot;
        taken.push(box); seen.add(run.key); drawn.push(run.text);
        ctx.save(); ctx.translate(x, y); ctx.rotate(ang);
        ctx.lineWidth = 3; ctx.strokeStyle = 'rgba(250,251,251,.92)'; ctx.strokeText(run.text, 0, 0.5);
        ctx.fillStyle = spec.color; ctx.fillText(run.text, 0, 0.5);
        ctx.restore();
      }
    }
    names.dataset.labels = drawn.join('|');      // what the canvas wrote, for anyone checking the map from the page
  }

  function placeCard() {
    if (card.hidden || !view) return;
    const p = pins.find(q => q.id === card.dataset.id);
    if (!p) return;
    const [x, y] = toPx(p.at), cw = card.offsetWidth, ch = card.offsetHeight, gap = 12;
    const off = x < 0 || y < 0 || x > w || y > ht;
    card.style.visibility = off ? 'hidden' : '';
    const above = y - gap - ch >= 4, below = y + gap + ch <= ht - 4;
    const side = !above && !below;
    let left, top;
    if (side) { left = x + gap + cw <= w - 4 ? x + gap : x - gap - cw; top = Math.min(Math.max(4, y - ch / 2), ht - ch - 4); }
    else { left = Math.min(Math.max(4, x - cw / 2), w - cw - 4); top = above ? y - gap - ch : y + gap; }
    left = Math.min(Math.max(4, left), w - cw - 4);
    card.style.left = `${Math.round(left)}px`; card.style.top = `${Math.round(top)}px`;
    card.style.setProperty('--ax', `${Math.round(x - left)}px`); card.style.setProperty('--ay', `${Math.round(y - top)}px`);
    card.dataset.side = side ? (left > x ? 'right' : 'left') : above ? 'above' : 'below';
    return side && x > left - 2 && x < left + cw + 2;      // in a short or narrow box the card would sit on its own pin
  }

  function openCard(id) {
    const p = model.places.find(q => q.id === id);
    card.hidden = !p;
    pins.forEach(q => q.g.classList.toggle('on', q.id === id));
    if (!p) { delete card.dataset.id; return; }
    card.dataset.id = id;
    card.setAttribute('aria-label', p.name);
    const tel = p.phone && p.phone.replace(/[^\d+]/g, '');
    card.innerHTML = h`<button type="button" class="x" data-close aria-label="Close">×</button>
      <b class="nm">${p.name}</b><small class="kd">${placeKind(p.kind)}</small>
      <p class="ad">${p.address}</p><p class="hr">${openHours(p.hours)}</p>
      ${tel ? h`<a class="tel" href="tel:${tel}">${p.phone}</a>` : ''}
      ${p.eligibility ? h`<p class="el">${p.eligibility}</p>` : ''}`;
    if (placeCard() && ht > card.offsetHeight + 40) fly({ ...view, y: view.y + (toPx(M.km(p.lat, p.lon))[1] - 16) / view.k }, 300);
  }

  function showTip(html, x, y) {
    tip.innerHTML = html; tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    const left = Math.min(Math.max(4, x - tw / 2), w - tw - 4), top = y - th - 14 >= 4 ? y - th - 14 : Math.min(ht - th - 4, y + 18);
    tip.style.left = `${Math.round(left)}px`; tip.style.top = `${Math.round(top)}px`;
  }
  const local = e => { const b = stack.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; };
  const tipFor = el => {
    const pin = el.closest('.pin'), hood = el.closest('.hood');
    if (pin) { const p = pins.find(q => q.id === pin.dataset.place)?.p; return p && h`<b>${p.name}</b><br>${placeKind(p.kind)}`; }
    return hood && model?.tip(hood.dataset.hood);
  };

  let drag = null, dragged = false;
  const on = (el, type, fn, opt) => { el.addEventListener(type, fn, opt); return () => el.removeEventListener(type, fn, opt); };
  const offs = [
    on(stack, 'pointerdown', e => {
      if (e.pointerType === 'touch' || e.button !== 0 || e.target.closest('.cmap-ctl,.cmap-card')) return;
      drag = { id: e.pointerId, x: e.clientX, y: e.clientY, v: view, moved: false };
    }),
    on(stack, 'pointermove', e => {
      if (drag && e.pointerId === drag.id && view) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (!drag.moved && Math.hypot(dx, dy) < 4) return;
        if (!drag.moved) { drag.moved = true; stack.setPointerCapture(e.pointerId); stack.classList.add('dragging'); tip.hidden = true; stop(); }
        setView({ x: drag.v.x - dx / view.k, y: drag.v.y - dy / view.k, k: view.k });
        return;
      }
      const html = e.pointerType !== 'touch' && !e.target.closest('.cmap-card,.cmap-ctl') && tipFor(e.target);
      if (html) showTip(html, ...local(e)); else tip.hidden = true;
    }),
    ...['pointerup', 'pointercancel'].map(t => on(stack, t, e => {
      if (!drag || e.pointerId !== drag.id) return;
      if (drag.moved) { dragged = true; setTimeout(() => { dragged = false; }); stack.classList.remove('dragging'); }
      drag = null;
    })),
    on(stack, 'pointerleave', () => { tip.hidden = true; }),
    on(stack, 'click', e => { if (dragged) { e.stopPropagation(); e.preventDefault(); dragged = false; } }, true),
    on(stack, 'wheel', e => {
      if (!view) return;
      e.preventDefault();
      const dy = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? ht : 1);
      stop(); tip.hidden = true;
      zoomAt(...local(e), view.k * Math.exp(-dy * (e.ctrlKey ? 0.01 : 0.0018)));
    }, { passive: false }),
    on(stack, 'dblclick', e => { if (!e.target.closest('.cmap-ctl,.cmap-card')) zoomAt(...local(e), view.k * 2, true); }),
    on(stack, 'click', e => {
      const z = e.target.closest('[data-zoom]'), pin = e.target.closest('.pin'), hood = e.target.closest('.hood');
      if (z) { const d = z.dataset.zoom; touched = d !== 'fit'; if (d === 'fit') fly(home); else zoomAt(w / 2, ht / 2, view.k * (d === 'in' ? 2 : 0.5), true); }
      else if (e.target.closest('[data-close]')) { place(null); stack.focus(); }
      else if (pin) place(pin.dataset.place === card.dataset.id ? null : pin.dataset.place);
      else if (hood) { fromMap = hood.dataset.hood; pick(hood.dataset.hood); }
    }),
    on(stack, 'focusin', e => {
      const n = e.target.closest('.hood,.pin');
      if (!n || !view) return;
      const at = n.classList.contains('hood') ? byId[n.dataset.hood].c : pins.find(q => q.id === n.dataset.place).at;
      showTip(tipFor(n), ...toPx(at));
    }),
    on(stack, 'focusout', () => { tip.hidden = true; }),
    on(stack, 'keydown', e => {
      const n = e.target.closest('.hood,.pin');
      if (n && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        if (n.classList.contains('pin')) place(n.dataset.place); else { fromMap = n.dataset.hood; pick(n.dataset.hood); n.focus(); }
        return;
      }
      if (!view || e.target.closest('.cmap-card')) return;
      const pan = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
      if (pan) { e.preventDefault(); stop(); setView({ ...view, x: view.x + pan[0] * 80 / view.k, y: view.y + pan[1] * 80 / view.k }); }
      else if (e.key === '+' || e.key === '=') { e.preventDefault(); zoomAt(w / 2, ht / 2, view.k * 2, true); }
      else if (e.key === '-' || e.key === '_') { e.preventDefault(); zoomAt(w / 2, ht / 2, view.k / 2, true); }
      else if (e.key === '0') { e.preventDefault(); touched = false; fly(home); }
    }),
    on(document, 'keydown', e => { if (e.key === 'Escape' && !card.hidden) { const id = card.dataset.id; place(null); $(`.pin[data-place="${id}"]`, stack)?.focus(); } }),
    on(document, 'click', e => { if (!card.hidden && !e.target.closest('.cmap-card,.pin,.cmap-ctl')) place(null); }),
  ];

  const sized = new ResizeObserver(() => {
    const nw = stack.clientWidth, nh = stack.clientHeight, dpr = window.devicePixelRatio || 1;
    if (!nw || !nh || (nw === w && nh === ht)) return;
    w = nw; ht = nh;
    [base, names].forEach(c => { c.width = Math.round(w * dpr); c.height = Math.round(ht * dpr); });
    fills.setAttribute('viewBox', `0 0 ${w} ${ht}`); marks.setAttribute('viewBox', `0 0 ${w} ${ht}`);
    const first = !view;
    home = fitTo(panelBounds());
    view = touched && view ? clamp(view) : home;
    if (first && model) bringIntoView(model, {}, false);
    schedule();
  });
  sized.observe(stack);

  function bringIntoView(m, was, animate) {
    if (m.selected !== was.selected && m.selected !== fromMap && byId[m.selected]) reveal(byId[m.selected].b, animate);
    const p = m.place !== was.place && pins.find(q => q.id === m.place);
    if (p) reveal([...p.at, ...p.at], animate);
  }

  return {
    update(m) {
      const was = shown;
      model = m;
      for (const [id, n] of Object.entries(hoods)) { n.dataset.bin = m.bins[id] ?? ''; n.classList.toggle('on', id === m.selected); }
      const sel = byId[m.selected];
      $$('.km path', marks).forEach(p => p.setAttribute('d', sel ? sel.d : ''));
      const rk = m.ranks.join();
      if (rk !== was.ranks?.join()) $('.ranks', marks).innerHTML = h`${m.ranks.map((id, i) => h`<g class="rk" data-rk="${id}"><circle r="8.5"/><text y="0.5">${i + 1}</text></g>`)}`;
      const pk = m.places.map(p => p.id).join();
      if (pk !== pinKeyNow) {
        pinKeyNow = pk;
        $('.pins', marks).innerHTML = h`${m.places.map(p => h`<g class="pin" data-place="${p.id}" data-kind="${p.kind}" tabindex="0" role="button" aria-label="${p.name}, ${placeKind(p.kind)}"><circle class="hit" r="11"/><g class="glyph">${raw(GLYPH[p.kind])}</g></g>`)}`;
        const els = Object.fromEntries($$('.pin', marks).map(g => [g.dataset.place, g]));
        pins = m.places.map(p => ({ id: p.id, p, at: M.km(p.lat, p.lon), g: els[p.id] }));
      }
      if (m.place !== card.dataset.id || pk !== was.pk) openCard(m.place);
      if (view) bringIntoView(m, was, true);
      fromMap = null;
      shown = { selected: m.selected, place: m.place, ranks: m.ranks, pk };
      schedule();
    },
    destroy() { offs.forEach(f => f()); sized.disconnect(); cancelAnimationFrame(frame); },
  };
}
