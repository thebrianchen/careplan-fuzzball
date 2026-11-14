// The clinic page's map: the panel's neighborhoods as their census-tract outlines over a quiet street map, with pins
// for the places that could help. It owns only the view (center and zoom); everything it shows comes from the page.
//   const map = clinicMap(stack, {basemap, wide, tracts, clinic, pick, place})   stack: an empty element sized by CSS
//     basemap  B from GET /api/app/basemap: the streets around the clinic, or null
//     wide     W from the same call: the city around it, simplified, with no local streets, or null; its `areas` (the
//              community areas, outlined and named) may be missing
//              (with neither, each neighborhood is a small circle at its center)
//     pick(id) the manager chose a neighborhood on the map; place(id | null) a pin's card was opened or closed
//   map.update({bins, ranks, selected, places, place, tip})   after every page paint
//     bins {tract_id: 0..4}; ranks: tract ids in rank order; places: the pins to show; tip(tract_id) -> h`...`
//   map.destroy()
// View: {x, y, k} is the center in km east and south of the middle of B's box, and px per km. Screen = size/2 + (km - center) * k.
// The view ranges over W's box. W is drawn everywhere; inside B's box, B's finer layers take over between HANDOVER's stops.
import { $, $$, cap, h, hours, raw } from './ui.js';
import { PLACE_KINDS, tractName } from './clinic_words.js';

const KM = 111.32, PAD = 18, TINY = 18;
const SPAN = 0.8;          // km: zoomed all the way in the view still spans about four blocks, so a neighborhood reads as one
const NEAR = 30;           // px per km: below this a neighborhood is a few px across, so rank badges and pins hide
const CELL = { wide: 6, basemap: 1.5 };    // km per grid cell; W is sparse and wide, B dense and small
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
const HANDOVER = ramp([[22, 0], [30, 1]]);       // how much of B shows inside its box; W gives way by the same amount

// W and B share a style per kind of feature, so the hand-over is only a change of detail, never of look.
const STYLE = {
  water: { from: 'water', fill: '#d4e3eb' },
  tracts: { from: 'tracts', stroke: '#d9dee3', width: ramp([[12, 0.5], [60, 0.6], [600, 1]]), alpha: ramp([[12, 0.5], [30, 1]]) },
  rail: { from: 'rail', stroke: '#a9b0b8', width: ramp([[12, 0.6], [60, 0.9], [600, 1.6]]), alpha: ramp([[12, 0.55], [40, 1]]), dash: [5, 4] },
  arterials: { from: 'arterials', stroke: '#b9c1c9', width: ramp([[12, 1.1], [60, 1.6], [300, 2.6], [1200, 6]]),
    label: { order: 1, win: true, font: '600 11px', color: '#56616d', room: 40, need: () => 1.4 } },
  highways: { from: 'highways', casing: '#aeb6bf', stroke: '#fff', width: ramp([[12, 1.8], [60, 2.6], [300, 4.5], [1200, 9]]),
    label: { order: 0, win: true, font: '600 11.5px', color: '#4a5562', room: 40, need: ramp([[12, 1.15], [60, 1.4]]) } },
};
// Bottom to top within a set. `from` picks the set's features; `keep` filters them; `min` is the zoom (px per km) a layer starts at.
// A `label` names a line where its straight stretch on screen is at least `need(k)` times the name plus `room`, so at low zoom only
// the long through streets get names; `order` says who claims room first, and `win` lets a name sit over a pin.
const LAYERS = [
  ...['water', 'tracts', 'rail', 'arterials', 'highways'].map(kind => ({ set: 'wide', ...STYLE[kind] })),
  { set: 'basemap', ...STYLE.water },
  { set: 'basemap', ...STYLE.tracts },
  { set: 'basemap', from: 'streets', keep: f => !f.name || f.name === 'Alley', min: 150, stroke: '#e1e5e9', width: ramp([[60, 0.35], [300, 0.8], [1200, 2.5]]), alpha: ramp([[60, 0.5], [250, 1]]) },
  { set: 'basemap', from: 'streets', keep: f => f.name && f.name !== 'Alley', stroke: '#cfd5db', width: ramp([[60, 0.5], [300, 1.3], [1200, 4]]), alpha: ramp([[40, 0], [62, 0.7], [250, 1]]),
    label: { order: 2, font: '10.5px', color: '#68737f', room: 26, need: ramp([[70, 4.5], [230, 1.6], [460, 1]]) } },
  { set: 'basemap', ...STYLE.rail },
  { set: 'basemap', ...STYLE.arterials },
  { set: 'basemap', ...STYLE.highways },
  // Chicago's community areas, the names people know. Painted once over both sets, since the hand-over would fade W out over the panel.
  { set: 'wide', from: 'areas', over: true, stroke: '#8f99a4', width: ramp([[12, 0.8], [120, 1.1]]), alpha: ramp([[12, 0.5], [40, 0.9], [160, 0.9], [420, 0]]), dash: [1, 3.5],
    names: { order: 1.5, font: '500 10px', spacing: '1.6px', color: '#6b7682', line: 12 } },
];
const AREAS = LAYERS.find(L => L.from === 'areas');
const LABELLED = LAYERS.map((L, i) => [L, i]).filter(([L]) => L.label || L.names).sort(([a, i], [b, j]) => (a.label || a.names).order - (b.label || b.names).order || j - i).map(([, i]) => i);
const FADE = ramp([[180, 1], [700, 0.62]]);      // zoomed in to a few blocks, the streets matter more than the shade
const FONT = '-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif';
const SCALE = [[100, 'ft'], [200, 'ft'], [500, 'ft'], [1000, 'ft'], [0.5, 'mi'], [1, 'mi'], [2, 'mi'], [5, 'mi'], [10, 'mi']];
const KM_PER = { ft: 0.0003048, mi: 1.609344 };
const lakeName = name => /^Lk /.test(name) && `Lake ${name.slice(3)}`;   // only lakes are named: TIGER's channel shapes reach into the lake

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

// The map data in km: each feature's points as a flat array, its bounds, and the straight runs a name can sit on.
function prepare(B, W, tracts) {
  const frame = (B || W)?.box;
  const lat0 = frame ? (frame.south + frame.north) / 2 : tracts.reduce((s, t) => s + t.lat, 0) / tracts.length;
  const lon0 = frame ? (frame.west + frame.east) / 2 : tracts.reduce((s, t) => s + t.lon, 0) / tracts.length;
  const cos = Math.cos(lat0 * Math.PI / 180);
  const km = (lat, lon) => [(lon - lon0) * cos * KM, -(lat - lat0) * KM];
  const flat = pts => { const a = new Float32Array(pts.length * 2); pts.forEach(([la, lo], i) => { [a[2 * i], a[2 * i + 1]] = km(la, lo); }); return a; };
  const bounds = arrs => { const b = [Infinity, Infinity, -Infinity, -Infinity];
    for (const a of arrs) for (let i = 0; i < a.length; i += 2) { b[0] = Math.min(b[0], a[i]); b[1] = Math.min(b[1], a[i + 1]); b[2] = Math.max(b[2], a[i]); b[3] = Math.max(b[3], a[i + 1]); }
    return b; };
  const pathOf = rings => rings.map(a => { let s = ''; for (let i = 0; i < a.length; i += 2) s += `${i ? 'L' : 'M'}${a[i].toFixed(3)} ${a[i + 1].toFixed(3)}`; return s + 'Z'; }).join('');
  const boxOf = b => [...km(b.north, b.west), ...km(b.south, b.east)];

  const shapes = tracts.map(t => {
    const c = km(t.lat, t.lon), outline = B?.tracts[t.tract_id] || W?.tracts[t.tract_id];
    const rings = outline ? outline.map(flat)
      : [Float32Array.from({ length: 48 }, (_, i) => c[i % 2] + 0.17 * (i % 2 ? Math.sin(i * Math.PI / 24) : Math.cos(i * Math.PI / 24)))];
    return { id: t.tract_id, c, rings, d: pathOf(rings), b: bounds(rings), outline: !!outline };
  });
  const box = (W || B) ? boxOf((W || B).box) : (b => [b[0] - 0.6, b[1] - 0.6, b[2] + 0.6, b[3] + 0.6])(bounds(shapes.flatMap(s => s.rings)));

  const sets = { wide: W, basemap: B };
  const layers = LAYERS.map(L => {
    const D = sets[L.set];
    const src = !D ? [] : L.from === 'tracts' ? Object.entries(D.tracts).map(([id, rings]) => ({ name: id, rings }))
      : L.from === 'water' ? D.water : (D[L.from] || []).filter(f => !L.keep || L.keep(f));
    const feats = src.map(f => { const parts = (f.rings || [f.line]).map(flat); return { name: f.name, parts, b: bounds(parts) }; });
    return { ...L, tiles: tile(feats, L.fill, CELL[L.set]), runs: L.label ? feats.flatMap(runsOf) : [] };
  });
  // Lakes, one entry per name across both sets, biggest first: the name is written inside where it fits. TIGER cuts
  // the lake into named and unnamed pieces, so a name's middle and ends must be in its own water and its corners in any water.
  const waters = new Map(), wet = [];
  for (const f of [...(W?.water || []), ...(B?.water || [])]) {
    const parts = f.rings.map(flat);
    wet.push({ parts, b: bounds(parts) });
    const text = lakeName(f.name || '');
    if (!text) continue;
    const g = waters.get(text) || waters.set(text, { text, parts: [] }).get(text);
    g.parts.push(...parts);
  }
  for (const g of waters.values()) { g.b = bounds(g.parts); g.size = (g.b[2] - g.b[0]) * (g.b[3] - g.b[1]); }
  const areas = (W?.areas || []).map(a => {
    const parts = a.rings.map(flat), b = bounds(parts), text = a.name.toUpperCase(), words = text.split(' ');
    const splits = words.slice(1).map((_, i) => [words.slice(0, i + 1).join(' '), words.slice(i + 1).join(' ')]);
    const two = splits.sort((p, q) => Math.max(...p.map(l => l.length)) - Math.max(...q.map(l => l.length)))[0];
    return { name: a.name, parts, b, at: middle(parts, b), lines: two ? [[text], two] : [[text]], size: (b[2] - b[0]) * (b[3] - b[1]) };
  }).sort((a, b) => b.size - a.size);
  return { km, shapes, box, detail: B && boxOf(B.box), wide: !!W, layers, wet, waters: [...waters.values()].sort((a, b) => b.size - a.size), areas };
}

// The inside point farthest from the outline, sampled on a grid: an L-shaped area's box middle can fall outside it.
function middle(parts, b) {
  let best = [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2], far = -1;
  for (let i = 0.5; i < 20; i++) for (let j = 0.5; j < 20; j++) {
    const x = b[0] + (b[2] - b[0]) * i / 20, y = b[1] + (b[3] - b[1]) * j / 20;
    if (!within(x, y, parts)) continue;
    let d = Infinity;
    for (const a of parts) for (let n = 0, m = a.length - 2; n < a.length; m = n, n += 2) {
      const dx = a[n] - a[m], dy = a[n + 1] - a[m + 1], t = Math.max(0, Math.min(1, ((x - a[m]) * dx + (y - a[m + 1]) * dy) / (dx * dx + dy * dy || 1)));
      d = Math.min(d, Math.hypot(x - a[m] - t * dx, y - a[m + 1] - t * dy));
    }
    if (d > far) { far = d; best = [x, y]; }
  }
  return best;
}

// Features grouped into a coarse grid by their middle, one Path2D per cell, so a frame strokes only the cells in view.
function tile(feats, closed, size) {
  const cells = new Map();
  for (const f of feats) {
    const key = `${Math.floor((f.b[0] + f.b[2]) / 2 / size)},${Math.floor((f.b[1] + f.b[3]) / 2 / size)}`;
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
const prepared = (B, W, tracts) => cached && cached.B === B && cached.W === W && cached.tracts === tracts ? cached.m : (cached = { B, W, tracts, m: prepare(B, W, tracts) }).m;

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

const meet = (a, b) => { const r = [Math.max(a[0], b[0]), Math.max(a[1], b[1]), Math.min(a[2], b[2]), Math.min(a[3], b[3])]; return r[0] < r[2] && r[1] < r[3] ? r : null; };
const contains = (o, r) => r[0] >= o[0] && r[1] >= o[1] && r[2] <= o[2] && r[3] <= o[3];
// Whether point (x, y) is inside the rings (even-odd, flat km arrays).
function within(x, y, parts) {
  let n = false;
  for (const a of parts) for (let i = 0, j = a.length - 2; i < a.length; j = i, i += 2)
    if ((a[i + 1] > y) !== (a[j + 1] > y) && x < (a[j] - a[i]) * (y - a[i + 1]) / (a[j + 1] - a[i + 1]) + a[i]) n = !n;
  return n;
}

export function clinicMap(stack, { basemap, wide, tracts, clinic, pick, place }) {
  const M = prepared(basemap, wide, tracts), byId = Object.fromEntries(M.shapes.map(s => [s.id, s]));
  const clinicAt = clinic && M.km(clinic.lat, clinic.lon), farText = `The clinic's panel: ${tracts.length} census tracts`;
  stack.innerHTML = h`<canvas class="cmap-base" aria-hidden="true"></canvas>
    <svg class="cmap-fills" role="group" aria-label="Neighborhoods of the panel"><g class="km">${M.shapes.map(s =>
      h`<path class="hood${s.outline ? '' : ' dot'}" data-hood="${s.id}" d="${s.d}" tabindex="0" role="button" aria-label="${tractName(s.id)}"/>`)}</g></svg>
    <canvas class="cmap-names" aria-hidden="true"></canvas>
    <svg class="cmap-marks"><g class="km"><path class="sel-halo"/><path class="sel"/></g><g class="pins"></g><g class="ranks"></g>
      ${clinicAt ? h`<g class="clinic" aria-hidden="true"><rect x="-6" y="-6" width="12" height="12" rx="3"/><path d="M0-3.3v6.6M-3.3 0h6.6"/><text>Clinic</text></g>` : ''}</svg>
    <div class="cmap-ctl"><button type="button" data-zoom="in" aria-label="Zoom in">+</button><button type="button" data-zoom="out" aria-label="Zoom out">−</button><button type="button" data-zoom="fit" class="fit" title="Back to the whole panel">Fit</button>${M.wide ? h`<button type="button" data-zoom="city" class="fit" title="Out to the whole city">City</button>` : ''}</div>
    <div class="cmap-scale" aria-hidden="true"><span></span><i></i></div>
    <div class="cmap-tip" role="status" hidden></div>`;
  // The card is the stack's sibling so that on a phone it can open as a sheet under the map instead of over it.
  const card = document.createElement('div');
  card.className = 'cmap-card'; card.setAttribute('role', 'dialog'); card.hidden = true;
  stack.after(card);
  stack.tabIndex = 0;
  stack.setAttribute('aria-label', 'Map. Arrow keys pan, plus and minus zoom.');
  const base = $('.cmap-base', stack), names = $('.cmap-names', stack), fills = $('.cmap-fills', stack), marks = $('.cmap-marks', stack);
  const tip = $('.cmap-tip', stack), scale = $('.cmap-scale', stack), hoods = Object.fromEntries($$('.hood', stack).map(n => [n.dataset.hood, n]));
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let w = 0, ht = 0, view = null, home = null, touched = false, frame = 0, flight = null, model = null, shown = {}, fromMap = null;
  let pins = [], pinKeyNow = '';

  const toPx = ([x, y], v = view) => [w / 2 + (x - v.x) * v.k, ht / 2 + (y - v.y) * v.k];
  const toKm = (px, py, v = view) => [v.x + (px - w / 2) / v.k, v.y + (py - ht / 2) / v.k];
  const bw = M.box[2] - M.box[0], bh = M.box[3] - M.box[1];
  const kMin = () => Math.min(w / bw, ht / bh);
  const kMax = () => Math.max(kMin(), Math.min(w, ht) / SPAN);
  // Along an axis where the view is narrower than the box it stays inside it, otherwise the whole box stays in view. Zoomed in,
  // the view may also run past the box's edge by up to half of itself, a share that shrinks to nothing at the city zoom, so wheeling out about
  // a place near the edge keeps that place under the pointer, and all the way out the box fits exactly.
  function clamp(v) {
    const k = Math.min(kMax(), Math.max(kMin(), v.k)), hw = w / 2 / k, hh = ht / 2 / k, give = Math.min(1, 2 * (1 - kMin() / k));
    const fit = (c, lo, hi, half) => { lo -= give * half; hi += give * half; return Math.min(Math.max(lo + half, hi - half), Math.max(Math.min(lo + half, hi - half), c)); };
    return { x: fit(v.x, M.box[0], M.box[2], hw), y: fit(v.y, M.box[1], M.box[3], hh), k };
  }
  const fitTo = (b, pad = PAD) => clamp({ x: (b[0] + b[2]) / 2, y: (b[1] + b[3]) / 2, k: Math.min((w - 2 * pad) / (b[2] - b[0] || 1e-3), (ht - 2 * pad) / (b[3] - b[1] || 1e-3)) });
  const hull = (b, s) => [Math.min(b[0], s[0]), Math.min(b[1], s[1]), Math.max(b[2], s[2]), Math.max(b[3], s[3])];
  const panel = M.shapes.reduce((b, s) => hull(b, s.b), [Infinity, Infinity, -Infinity, -Infinity]);
  const panelBounds = () => clinicAt ? hull(panel, [...clinicAt, ...clinicAt]) : panel;

  const schedule = () => { if (!frame) frame = requestAnimationFrame(draw); };
  const setView = v => { view = clamp(v); schedule(); };
  function fly(to, ms) {
    to = clamp(to);
    if (reduced.matches || !view) { flight = null; setView(to); return; }
    flight = { from: view, to, t0: performance.now(), ms: ms ?? Math.min(650, 380 + 90 * Math.abs(Math.log2(to.k / view.k))) };
    schedule();
  }
  const stop = () => { flight = null; touched = true; };
  function zoomAt(px, py, k, animate) {
    const [x, y] = toKm(px, py), k2 = Math.min(kMax(), Math.max(kMin(), k));
    const to = { x: x - (px - w / 2) / k2, y: y - (py - ht / 2) / k2, k: k2 };
    touched = true;
    if (animate) fly(to, 260); else { flight = null; setView(to); }
  }
  // Brings a neighborhood (or a point) into view when it is off screen or too small to see, keeping the zoom when that is enough.
  function reveal(b, animate) {
    const [x0, y0] = toPx([b[0], b[1]]), [x1, y1] = toPx([b[2], b[3]]), m = 10;
    const inside = x0 >= m && y0 >= m && x1 <= w - m && y1 <= ht - m, tiny = b[2] > b[0] && Math.max(x1 - x0, y1 - y0) < TINY;
    if (inside && !tiny && (b[2] > b[0] || view.k >= NEAR)) return;
    const big = b[2] - b[0] > 0, k = !big ? Math.max(view.k, NEAR) : tiny ? Math.min(w, ht) * 0.35 / Math.max(b[2] - b[0], b[3] - b[1])
      : Math.min(view.k, (w - 4 * PAD) / (b[2] - b[0]), (ht - 4 * PAD) / (b[3] - b[1]));
    const to = { x: (b[0] + b[2]) / 2, y: (b[1] + b[3]) / 2, k };
    if (animate) fly(to); else setView(to);
  }

  function paint(ctx, L, r, a, k) {
    const alpha = a * (L.alpha ? L.alpha(k) : 1);
    const cells = alpha > 0 && k >= (L.min || 0) ? L.tiles.filter(c => overlaps(c.b, r)) : [];
    if (!cells.length) return;
    ctx.globalAlpha = alpha;
    if (L.fill) { ctx.fillStyle = L.fill; cells.forEach(c => ctx.fill(c.path)); return; }
    const width = L.width(k) / k;
    ctx.setLineDash(L.dash ? L.dash.map(d => d / k) : []);
    if (L.casing) { ctx.strokeStyle = L.casing; ctx.lineWidth = width + 2 / k; cells.forEach(c => ctx.stroke(c.path)); }
    ctx.strokeStyle = L.stroke; ctx.lineWidth = width; cells.forEach(c => ctx.stroke(c.path));
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
    const t = M.wide ? HANDOVER(v.k) : 1, d = M.detail;
    const ctx = base.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#eef1f3'; ctx.fillRect(0, 0, w, ht);
    ctx.setTransform(dpr * v.k, 0, 0, dpr * v.k, dpr * tx, dpr * ty);
    ctx.save();
    ctx.beginPath(); ctx.rect(M.box[0], M.box[1], bw, bh); ctx.fillStyle = '#fafbfb'; ctx.fill(); ctx.clip();
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    // W outside B's box; inside it, W at 1 - t under B at t.
    const passes = d ? [['wide', 'out', 1], ['wide', 'in', 1 - t], ['basemap', 'in', t]] : [['wide', null, 1]];
    for (const [set, where, a] of passes) {
      const rr = where === 'in' ? meet(r, d) : r;
      if (a <= 0 || !rr || (where === 'out' && contains(d, r))) continue;
      ctx.save();
      if (where) { ctx.beginPath(); if (where === 'out') ctx.rect(M.box[0], M.box[1], bw, bh); ctx.rect(d[0], d[1], d[2] - d[0], d[3] - d[1]); ctx.clip('evenodd'); }
      for (const L of M.layers) if (L.set === set && !L.over) paint(ctx, L, rr, a, v.k);
      ctx.restore();
    }
    for (const L of M.layers) if (L.over) paint(ctx, L, r, 1, v.k);
    ctx.restore();
    const m = `matrix(${v.k} 0 0 ${v.k} ${tx} ${ty})`;
    fills.firstElementChild.setAttribute('transform', m);
    fills.style.setProperty('--fo', FADE(v.k).toFixed(2));
    marks.firstElementChild.setAttribute('transform', m);
    const far = v.k < NEAR;
    stack.classList.toggle('far', far);
    scaleBar();
    const { hard, soft } = placeMarks(far);
    label(r, hard, soft, dpr, t);
    placeCard();
    stack.dataset.view = `${v.x.toFixed(3)},${v.y.toFixed(3)},${v.k.toFixed(1)}`;
  }

  // A round distance, at most about 100 px long, drawn to the view's scale.
  function scaleBar() {
    const room = Math.min(100, w * 0.28), [n, u] = SCALE.findLast(([n, u]) => n * KM_PER[u] * view.k <= room) || SCALE[0];
    const km = n * KM_PER[u], text = `${n.toLocaleString('en-US')} ${u}`;
    $('i', scale).style.width = `${(km * view.k).toFixed(1)}px`;
    if (scale.dataset.km !== String(km)) { scale.dataset.km = km; $('span', scale).textContent = text; }
  }

  // Rank badges, pins and the clinic in screen px. Returns the boxes street names must keep clear of: `hard` ones always,
  // `soft` ones (pins) unless the name is an expressway's or an arterial's.
  const textWidths = {};
  function placeMarks(far) {
    const box = el => [el.offsetLeft - 4, el.offsetTop - 4, el.offsetLeft + el.offsetWidth + 4, el.offsetTop + el.offsetHeight + 4];
    const hard = [box($('.cmap-ctl', stack)), box(scale)];
    const c = clinicAt && toPx(clinicAt), badges = [];
    if (!far) $$('.ranks .rk', marks).forEach(g => {
      const [x0, y0] = toPx(byId[g.dataset.rk].c), clear = ([px, py]) => (!c || Math.hypot(px - c[0], py - c[1]) >= 16) && badges.every(o => Math.hypot(px - o[0] - 9, py - o[1] - 9) >= 17);
      const [x, y] = [[0, 0], [0, -17], [0, 17], [17, 0], [-17, 0], [13, -13], [-13, -13]].map(([dx, dy]) => [x0 + dx, y0 + dy]).find(clear) || [x0, y0];
      g.setAttribute('transform', `translate(${x.toFixed(1)} ${y.toFixed(1)})`);
      badges.push([x - 9, y - 9, x + 9, y + 9]);
    });
    const s = Math.min(1.15, Math.max(0.72, 0.72 + 0.2 * Math.log2(view.k / 70)));
    marks.style.setProperty('--ps', s.toFixed(2));
    const dots = far ? [] : pins.map(p => { const [x, y] = toPx(p.at); p.g.setAttribute('transform', `translate(${x.toFixed(1)} ${y.toFixed(1)})`); return [x - 6 * s, y - 6 * s, x + 6 * s, y + 6 * s]; });
    if (c) {
      const g = $('.clinic', marks), t = $('text', g), text = far ? farText : 'Clinic', th = 15;
      if (t.textContent !== text) t.textContent = text;
      const tw = textWidths[text] ||= Math.ceil(t.getComputedTextLength()) || 38;
      g.setAttribute('transform', `translate(${c[0].toFixed(1)} ${c[1].toFixed(1)})`);
      // Zoomed out the label names the whole cluster, so it sits beside the panel's outline rather than beside the clinic.
      const [p0, p1] = [toPx([panel[0], panel[1]]), toPx([panel[2], panel[3]])], my = (p0[1] + p1[1]) / 2 - c[1] - th / 2, mx = (p0[0] + p1[0]) / 2 - c[0] - tw / 2;
      const spots = far ? [[p1[0] - c[0] + 8, my], [p0[0] - c[0] - 8 - tw, my], [mx, p0[1] - c[1] - 6 - th], [mx, p1[1] - c[1] + 6]]
        : [[10, -th / 2], [-10 - tw, -th / 2], [-tw / 2, -10 - th], [-tw / 2, 10], [9, -th - 4], [-9 - tw, -th - 4], [9, 4], [-9 - tw, 4]];
      const score = ([dx, dy]) => { const b = [c[0] + dx, c[1] + dy, c[0] + dx + tw, c[1] + dy + th];
        const out = b[0] < 4 || b[1] < 4 || b[2] > w - 4 || b[3] > ht - 4;
        return 4 * badges.filter(o => overlaps(o, b)).length + dots.filter(o => overlaps(o, b)).length + 8 * hard.filter(o => overlaps(o, b)).length + (out ? 99 : 0); };
      const best = spots.reduce((a, q) => score(q) < score(a) ? q : a);
      t.setAttribute('x', best[0].toFixed(1)); t.setAttribute('y', (best[1] + 11.5).toFixed(1));
      hard.push([c[0] - 8, c[1] - 8, c[0] + 8, c[1] + 8], [c[0] + best[0] - 3, c[1] + best[1] - 2, c[0] + best[0] + tw + 3, c[1] + best[1] + th + 2]);
    }
    return { hard: hard.concat(badges), soft: dots };
  }

  const widths = new Map();
  const write = (ctx, text, color, x, y, ang = 0) => {
    ctx.save(); ctx.translate(x, y); ctx.rotate(ang);
    ctx.lineWidth = 3; ctx.strokeStyle = 'rgba(250,251,251,.92)'; ctx.strokeText(text, 0, 0.5);
    ctx.fillStyle = color; ctx.fillText(text, 0, 0.5);
    ctx.restore();
  };
  const wet = (x, y) => M.wet.some(f => x >= f.b[0] && x <= f.b[2] && y >= f.b[1] && y <= f.b[3] && within(x, y, f.parts));
  // A lake's name, inside it where the whole name fits, on one line or else a word per line. The spots sit on a grid fixed to the
  // map, so the name stays put while the map pans.
  function waterNames(ctx, inner, taken, drawn) {
    const v = view, step = 30 / v.k, LH = 14;
    ctx.font = `italic 600 12px ${FONT}`;
    for (const g of M.waters) {
      const room = meet(inner, g.b);
      if (!room || drawn.length >= 2) continue;
      for (const lines of g.text.includes(' ') ? [[g.text], g.text.split(' ')] : [[g.text]]) {
        const tw = Math.max(...lines.map(l => ctx.measureText(l).width)), hw = (tw / 2 + 4) / v.k, hh = (lines.length * LH / 2 + 2) / v.k;
        if (room[2] - room[0] < 2 * hw || room[3] - room[1] < 2 * hh) continue;
        const cx = (room[0] + room[2]) / 2, cy = (room[1] + room[3]) / 2, spots = [];
        for (let x = Math.ceil((room[0] + hw) / step) * step; x <= room[2] - hw; x += step)
          for (let y = Math.ceil((room[1] + hh) / step) * step; y <= room[3] - hh; y += step) spots.push([x, y]);
        spots.sort((a, b) => Math.hypot(a[0] - cx, a[1] - cy) - Math.hypot(b[0] - cx, b[1] - cy));
        const at = spots.find(([x, y]) => [0, -hw, hw].every(dx => within(x + dx, y, g.parts)) && [[-hw, -hh], [hw, -hh], [-hw, hh], [hw, hh]].every(([dx, dy]) => wet(x + dx, y + dy))
          && !taken.some(o => overlaps(o, [...toPx([x - hw, y - hh]), ...toPx([x + hw, y + hh])])));
        if (!at) continue;
        const [x, y] = toPx(at);
        taken.push([x - hw * v.k, y - hh * v.k, x + hw * v.k, y + hh * v.k]); drawn.push(g.text);
        lines.forEach((l, i) => write(ctx, l, '#5f8399', x, y + (i - (lines.length - 1) / 2) * LH));
        break;
      }
    }
  }

  // Each community area's name once, inside it where the whole name fits, on one line or else two, as near its middle as is clear.
  // Like a lake's name, the spots sit on a grid fixed to the map.
  function areaNames(ctx, inner, taken, soft, edges, drawn) {
    const v = view, spec = AREAS.names, step = 24 / v.k;
    // A faded halo smears on the dark shades, so the names go once the outlines are half faded, a few blocks across.
    if (AREAS.alpha(v.k) < 0.4 || !M.areas.length) return;
    ctx.font = `${spec.font} ${FONT}`; ctx.letterSpacing = spec.spacing;
    for (const g of M.areas) {
      const room = meet(inner, g.b);
      if (!room) continue;
      for (const lines of g.lines) {
        const tw = Math.max(...lines.map(l => { let n = widths.get('area' + l); if (n == null) widths.set('area' + l, n = ctx.measureText(l).width); return n; }));
        const hw = (tw / 2 + 3) / v.k, hh = (lines.length * spec.line / 2 + 2) / v.k, [cx, cy] = g.at, spots = [];
        if (room[2] - room[0] < 2 * hw || room[3] - room[1] < 2 * hh) continue;
        for (let x = cx + Math.ceil((room[0] + hw - cx) / step) * step; x <= room[2] - hw; x += step)
          for (let y = cy + Math.ceil((room[1] + hh - cy) / step) * step; y <= room[3] - hh; y += step) spots.push([x, y]);
        spots.sort((p, q) => Math.hypot(p[0] - cx, p[1] - cy) - Math.hypot(q[0] - cx, q[1] - cy));
        const fits = ([x, y]) => [-hw, -hw / 2, 0, hw / 2, hw].every(dx => [-hh, 0, hh].every(dy => within(x + dx, y + dy, g.parts)));
        const boxAt = ([x, y]) => [...toPx([x - hw, y - hh]), ...toPx([x + hw, y + hh])];
        const clear = s => { const b = boxAt(s); return !taken.some(o => overlaps(o, b)) && !edges.some(e => crosses(e, b)) && fits(s); };
        const at = spots.find(s => clear(s) && !soft.some(o => overlaps(o, boxAt(s)))) || spots.find(clear);
        if (!at) continue;
        const [x, y] = toPx(at), b = boxAt(at);
        taken.push(b); drawn.push([g.name, ...b.map(Math.round)]);
        lines.forEach((l, i) => write(ctx, l, spec.color, x, y + (i - (lines.length - 1) / 2) * spec.line));
        break;
      }
    }
    ctx.letterSpacing = '0px';
  }

  // Each line's name once, along the longest stretch of it on screen, upright, skipped where it would touch a name already written.
  // B's names stay in B's box and only once B has taken over; W's names keep out of it from then on.
  function label(r, hard, soft, dpr, t) {
    const ctx = names.getContext('2d'), v = view, drawn = [], seen = new Set();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, ht);
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
    const inner = [r[0] + 6 / v.k, r[1] + 6 / v.k, r[2] - 6 / v.k, r[3] - 6 / v.k], d = M.detail;
    const dpx = d && t >= 0.5 && [...toPx([d[0], d[1]]), ...toPx([d[2], d[3]])];
    const sel = byId[model?.selected], edges = [];
    if (sel && overlaps(sel.b, r)) for (const a of sel.rings) for (let i = 2; i < a.length; i += 2) edges.push([...toPx([a[i - 2], a[i - 1]]), ...toPx([a[i], a[i + 1]])]);
    waterNames(ctx, inner, hard, drawn);
    const areas = [];
    for (const i of LABELLED) {
      const L = M.layers[i], spec = L.label, own = L.set === 'basemap';
      if (L.names) { areaNames(ctx, inner, hard, soft, edges, areas); continue; }
      const room = own ? t >= 0.5 && d && meet(inner, d) : inner, keepOut = !own && dpx;
      if (!room) continue;
      ctx.font = `${spec.font} ${FONT}`;
      const need = spec.need(v.k);
      const cands = [];
      for (const run of L.runs) {
        if (seen.has(run.key) || run.len * v.k < 40) continue;
        const c = clip(run.x1, run.y1, run.x2, run.y2, room);
        if (!c) continue;
        const len = (c[1] - c[0]) * run.len * v.k;
        let tw = widths.get(spec.font + run.text);
        if (tw == null) widths.set(spec.font + run.text, tw = ctx.measureText(run.text).width);
        if (len < (tw + spec.room) * need) continue;
        cands.push({ run, c, len, tw });
      }
      cands.sort((a, b) => b.len - a.len);
      for (const { run, c, tw } of cands) {
        if (seen.has(run.key)) continue;
        const full = run.len * v.k, step = Math.max(tw * 3, 180), a0 = c[0] * full + tw / 2 + 6, a1 = c[1] * full - tw / 2 - 6;
        const mid = (a0 + a1) / 2, snap = Math.round(mid / step) * step;
        let ang = Math.atan2(run.y2 - run.y1, run.x2 - run.x1);
        if (ang > Math.PI / 2) ang -= Math.PI; else if (ang <= -Math.PI / 2) ang += Math.PI;
        if (ang > Math.PI / 2 - 0.17) ang -= Math.PI;        // near-vertical names all read bottom to top
        const cx = Math.abs(Math.cos(ang)), sy = Math.abs(Math.sin(ang)), hw = (tw * cx + 13 * sy) / 2 + 3, hh = (tw * sy + 13 * cx) / 2 + 2;
        // the snapped middle keeps a name still while the map pans; the other spots are for when something is in the way
        const spots = [snap >= a0 && snap <= a1 ? snap : mid, a0 + (a1 - a0) * 0.25, a0 + (a1 - a0) * 0.75, a0, a1].map(at => {
          const f = at / full, x = w / 2 + (run.x1 + (run.x2 - run.x1) * f - v.x) * v.k, y = ht / 2 + (run.y1 + (run.y2 - run.y1) * f - v.y) * v.k;
          return { x, y, box: [x - hw, y - hh, x + hw, y + hh] };
        });
        const clear = ({ box }) => !hard.some(o => overlaps(o, box)) && !edges.some(e => crosses(e, box)) && !(keepOut && overlaps(keepOut, box));
        const spot = spots.find(s => clear(s) && !soft.some(o => overlaps(o, s.box))) || (spec.win && spots.find(clear));
        if (!spot) continue;
        hard.push(spot.box); seen.add(run.key); drawn.push(run.text);
        write(ctx, run.text, spec.color, spot.x, spot.y, ang);
      }
    }
    names.dataset.labels = drawn.join('|');      // what the canvas wrote, for anyone checking the map from the page
    names.dataset.areas = JSON.stringify(areas);   // [[name, left, top, right, bottom], ...] in px
  }

  function placeCard() {
    if (card.hidden || !view) return;
    const p = pins.find(q => q.id === card.dataset.id);
    if (!p) return;
    if (getComputedStyle(card).position !== 'absolute') {       // the phone's sheet under the map
      card.style.left = card.style.top = card.style.visibility = ''; card.dataset.side = 'sheet';
      return false;
    }
    const [x, y] = toPx(p.at), cw = card.offsetWidth, ch = card.offsetHeight, gap = 12;
    const off = x < 0 || y < 0 || x > w || y > ht || view.k < NEAR;
    card.style.visibility = off ? 'hidden' : '';
    const above = y - gap - ch >= 4, below = y + gap + ch <= ht - 4;
    const side = !above && !below;
    let left, top;
    if (side) { left = x + gap + cw <= w - 4 ? x + gap : x - gap - cw; top = Math.min(Math.max(4, y - ch / 2), ht - ch - 4); }
    else { left = Math.min(Math.max(4, x - cw / 2), w - cw - 4); top = above ? y - gap - ch : y + gap; }
    left = Math.min(Math.max(4, left), w - cw - 4);
    const ox = stack.offsetLeft + stack.clientLeft, oy = stack.offsetTop + stack.clientTop;
    card.style.left = `${Math.round(left + ox)}px`; card.style.top = `${Math.round(top + oy)}px`;
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
      <b class="nm" title="${p.name}">${p.name}</b><small class="kd">${placeKind(p.kind)}</small>
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
    ...[stack, card].map(el => on(el, 'wheel', e => {
      if (!view || (el === card && card.dataset.side === 'sheet')) return;
      e.preventDefault();
      const dy = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? ht : 1);
      stop(); tip.hidden = true;
      zoomAt(...local(e), view.k * Math.exp(-dy * (e.ctrlKey ? 0.01 : 0.0018)));
    }, { passive: false })),
    on(stack, 'dblclick', e => { if (!e.target.closest('.cmap-ctl,.cmap-card')) zoomAt(...local(e), view.k * 2, true); }),
    on(stack, 'click', e => {
      const z = e.target.closest('[data-zoom]'), pin = e.target.closest('.pin'), hood = e.target.closest('.hood');
      if (z) {
        const d = z.dataset.zoom;
        touched = d !== 'fit';
        if (d === 'fit') fly(home); else if (d === 'city') fly({ x: (M.box[0] + M.box[2]) / 2, y: (M.box[1] + M.box[3]) / 2, k: 0 }); else zoomAt(w / 2, ht / 2, view.k * (d === 'in' ? 2 : 0.5), true);
      } else if (pin) place(pin.dataset.place === card.dataset.id ? null : pin.dataset.place);
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
    on(card, 'click', e => { if (e.target.closest('[data-close]')) { place(null); stack.focus({ preventScroll: true }); } }),
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
    destroy() { offs.forEach(f => f()); sized.disconnect(); cancelAnimationFrame(frame); card.remove(); },
  };
}
