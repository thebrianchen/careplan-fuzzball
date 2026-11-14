// Exhibits: pictures of what a finding cites. Each one is a pure function from plain data to an h`...` template.
// The map also needs mount(root, city) once its HTML is in the page, to draw the city behind the markers.
//   map({id, status, home, places, resources, animate})   home {tract_id, lat, lon} | null
//     places    [{ref, name, address, lat, lon, miles_from_home, time, trip | null}]   resources [{n, name, address, lat, lon}]
//     trip      {feasible, leave_by, arrive, minutes, assumes, rides: [{board, alight, path: [[lat, lon], ...]}]}
//   visitStrip({appts, cited, depts})                    appts: chart rows in date order, cited: Set of refs
//   countyBar(fact, {compact})                           fact.county {median, p75, max, median_text}
import { $$, cap, clock, date, h, plural } from './ui.js';

const KM = 111.32;                         // km per degree of latitude
const W = 360, H = 240;                    // the map's core in px; a wider box shows more of the city on both sides
const PAD_X = 48, PAD_Y = 38, MIN_KM = 4;  // MIN_KM: the closest view still shows enough street grid to read as a map
const r1 = v => Math.round(v * 10) / 10;

export function viewOf(points, { w = W, h = H, padX = PAD_X, padY = PAD_Y, minKm = MIN_KM } = {}) {
  const lats = points.map(p => p.lat), lons = points.map(p => p.lon);
  const lat0 = (Math.min(...lats) + Math.max(...lats)) / 2, lon0 = (Math.min(...lons) + Math.max(...lons)) / 2;
  const cos = Math.cos(lat0 * Math.PI / 180);
  const spanX = (Math.max(...lons) - Math.min(...lons)) * cos * KM, spanY = (Math.max(...lats) - Math.min(...lats)) * KM;
  return { lat0, lon0, cos, w, h, k: Math.min((w - 2 * padX) / spanX, (h - 2 * padY) / spanY, h / minKm) };   // k: px per km
}
export const at = (v, p) => [v.w / 2 + (p.lon - v.lon0) * v.cos * KM * v.k, v.h / 2 - (p.lat - v.lat0) * KM * v.k];
// The canvas's data-view for a view with its own core; backdrop() reads a missing core size as the visit map's.
export const viewAttr = v => [v.lat0, v.lon0, v.k, v.w, v.h].join(',');

const anchorAt = x => x < W * 0.28 ? 'start' : x > W * 0.72 ? 'end' : 'middle';
function label(x, y, text, above, cls) {
  const a = anchorAt(x);
  return h`<text class="${cls}" x="${r1(x + (a === 'start' ? -9 : a === 'end' ? 9 : 0))}" y="${r1(y + (above ? -13 : 22))}" text-anchor="${a}">${text}</text>`;
}
const textWidth = s => s.length * 6.4;     // 12px system sans, close enough to keep a label inside the core

// The label sits beside the middle of the straight line, on the side away from `avoid` (the drawn route) when there is one.
function lineLabel(x1, y1, x2, y2, text, avoid) {
  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2, w = textWidth(text);
  const right = { x: mx + 9, y: my - 9, a: 'start', from: mx + 9, to: mx + 9 + w };
  const left = { x: mx - 9, y: my + 4, a: 'end', from: mx - 9 - w, to: mx - 9 };
  const sides = (avoid ? avoid[0] > mx : (x2 - x1) * (y2 - y1) <= 0) ? [left, right] : [right, left];
  const p = sides.find(s => s.from > 4 && s.to < W - 4) || { x: mx, y: my + 4, a: 'middle' };   // no room beside the line: sit on it
  return h`<text class="leg" x="${r1(p.x)}" y="${r1(p.y)}" text-anchor="${p.a}">${text}</text>`;
}

const rides = p => p.trip && p.trip.feasible && p.trip.rides || [];
const line = pts => pts.map(([x, y], i) => `${i ? 'L' : 'M'}${r1(x)} ${r1(y)}`).join('');

// The trip as the schedule has it: each ride along the stops it passes, and the walks between (to the first stop, at each
// change, from the last stop). Without rides there is only a straight line, which claims no route.
function trip(view, from, to, p) {
  const paths = rides(p).map(r => r.path.map(([lat, lon]) => at(view, { lat, lon })));
  if (!paths.length) return { whole: line([from, to]), straight: line([from, to]) };
  const walks = [...paths.map((path, i) => [i ? paths[i - 1].at(-1) : from, path[0]]), [paths.at(-1).at(-1), to]];
  const all = paths.flat();
  return { whole: line([from, ...all, to]), ridden: paths.map(line).join(''), walked: walks.map(line).join(''),
           changes: walks.flat(), middle: [all.reduce((s, [x]) => s + x, 0) / all.length, all.reduce((s, [, y]) => s + y, 0) / all.length] };
}

function tripText(p) {
  const t = p.trip, when = clock(p.time), n = rides(p).length;
  if (!t) return `${p.miles_from_home} mi from the home neighborhood in a straight line.`;
  if (!t.feasible) return h`<span title="${t.assumes}">No CTA or Pace trip arrives by ${when}.</span>`;
  return h`<span title="${t.assumes}">To arrive for ${when}, leave by ${clock(t.leave_by)}. ${t.minutes} min${n ? ` on ${plural(n, 'ride')}` : ''} by CTA or Pace, one way.</span>`;
}

const mins = hhmm => +hhmm.slice(0, 2) * 60 + +hhmm.slice(3, 5);

// The trip against the clock, from leaving home to the appointment: each ride, the walking and waiting between rides,
// and the time to spare on arrival.
function tripBar(p) {
  const t = p.trip, rs = rides(p);
  if (!rs.length || !p.time) return '';
  const marks = [t.leave_by, ...rs.flatMap(r => [r.board, r.alight]), t.arrive, p.time];
  const segs = marks.slice(1).map((to, i) => ({ from: marks[i], to, min: mins(to) - mins(marks[i]),
    kind: i === marks.length - 2 ? 'spare' : i % 2 ? 'ride' : 'between' })).filter(s => s.min > 0);
  const total = kind => segs.filter(s => s.kind === kind).reduce((n, s) => n + s.min, 0);
  const NAME = { ride: 'Riding', between: 'Walking or waiting', spare: 'Early' };
  return h`<div class="tbar">
    <div class="tbar-ends"><span>Leave ${clock(t.leave_by)}</span><span>Appointment ${clock(p.time)}</span></div>
    <div class="tbar-track">${segs.map(s => h`<span class="${s.kind}" style="flex:${s.min}" title="${NAME[s.kind]}: ${clock(s.from)} to ${clock(s.to)}, ${s.min} min">${s.kind === 'ride' && s.min >= 10 ? s.min : ''}</span>`)}</div>
    <small><span><i class="ride"></i>Riding ${total('ride')} min</span><span><i class="between"></i>Walking and waiting ${t.minutes - total('ride')} min</span>${total('spare') ? h`<span><i class="spare"></i>${total('spare')} min early</span>` : ''}</small>
  </div>`;
}

export function map({ id, status, home, places, resources, animate = false }) {
  const ridden = places.flatMap(p => rides(p).flatMap(r => r.path.map(([lat, lon]) => ({ lat, lon }))));
  const view = viewOf([home, ...places, ...resources, ...ridden].filter(Boolean));
  const hp = home && at(view, home);
  const others = [...places, ...resources].map(p => at(view, p));
  const homeAbove = !hp || !others.length || others.reduce((s, [, y]) => s + y, 0) / others.length >= hp[1];
  const legs = hp ? places.map((p, i) => {
    const [x2, y2] = at(view, p), t = p.trip, n = rides(p).length, way = trip(view, hp, [x2, y2], p);
    const text = [t && t.feasible && (n ? `${t.minutes} min · ${plural(n, 'ride')}` : `${t.minutes} min by bus or train`), `${p.miles_from_home} mi`].filter(Boolean).join(' · ');
    return h`<mask id="mk-${id}-${i}" maskUnits="userSpaceOnUse" x="${-W}" y="${-H}" width="${3 * W}" height="${3 * H}"><path class="reveal" d="${way.whole}" pathLength="1"/></mask>
      <g mask="url(#mk-${id}-${i})">${way.straight ? h`<path class="leg-line" d="${way.straight}"/>`
        : h`<path class="walk-line" d="${way.walked}"/><path class="ride-line" d="${way.ridden}"/>${way.changes.map(([x, y]) => h`<circle class="change" cx="${r1(x)}" cy="${r1(y)}" r="2.2"/>`)}`}</g>
      ${lineLabel(hp[0], hp[1], x2, y2, text, way.middle)}`;
  }) : [];
  const placeMarks = places.map(p => {
    const [x, y] = at(view, p);
    return h`<g class="place" data-ref="${p.ref}"><title>${p.name}, ${p.address}</title><circle cx="${r1(x)}" cy="${r1(y)}" r="7"/>
      ${label(x, y, p.name, hp ? y < hp[1] : true, 'name')}</g>`;
  });
  const resMarks = resources.map(p => {
    const [x, y] = at(view, p);
    return h`<g class="res" data-res="${p.n}" tabindex="0"><title>${p.n}. ${p.name}, ${p.address}</title>
      <circle cx="${r1(x)}" cy="${r1(y)}" r="8"/><text x="${r1(x)}" y="${r1(y + 3.8)}" text-anchor="middle">${p.n}</text></g>`;
  });
  const homeMark = hp ? h`<g class="home"><title>Center of census tract ${home.tract_id}. The map shows the neighborhood, not the home address.</title>
      <circle class="ring" cx="${r1(hp[0])}" cy="${r1(hp[1])}" r="9"/><circle cx="${r1(hp[0])}" cy="${r1(hp[1])}" r="4"/>
      ${label(hp[0], hp[1], 'Home neighborhood', homeAbove, 'name')}</g>` : '';
  const said = [hp && 'the home neighborhood', ...places.map(p => p.name), resources.length && `${resources.length} places that could help`].filter(Boolean).join(', ');
  return h`<figure class="exmap ${status}${animate ? ' draw' : ''}">
    <div class="exmap-box">
      <canvas data-view="${view.lat0},${view.lon0},${view.k}" aria-hidden="true"></canvas>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="group" aria-label="Map: ${said}">${legs}${resMarks}${placeMarks}${homeMark}</svg>
    </div>
    <figcaption>${places.map(p => h`<p>${places.length > 1 ? h`<b>${p.name}.</b> ` : ''}${tripText(p)}</p>${tripBar(p)}`)}
      <small>${ridden.length ? 'The route is the last scheduled trip that arrives in time, which may not be the one the patient takes. ' : ''}Drawn here from tables already on the private side. Drawing it sends nothing anywhere.</small></figcaption>
  </figure>`;
}

function backdrop(canvas, city) {
  const w = canvas.clientWidth, ht = canvas.clientHeight;
  if (!w || !ht) return;
  const [lat0, lon0, k, cw = W, ch = H] = canvas.dataset.view.split(',').map(Number);
  const s = Math.min(w / cw, ht / ch) * k, cos = Math.cos(lat0 * Math.PI / 180), dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(ht * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const pts = city.stops || city.tracts;                // tract centers are far sparser, so they get bigger dots
  const r = city.stops ? Math.min(1.25, 0.5 + s / 80) : Math.min(3, 1.3 + s / 45);
  ctx.fillStyle = city.stops ? 'rgba(57,70,86,.30)' : 'rgba(57,70,86,.26)';
  ctx.beginPath();
  for (let i = 0; i < pts.lat.length; i++) {
    const x = w / 2 + (pts.lon[i] - lon0) * cos * KM * s, y = ht / 2 - (pts.lat[i] - lat0) * KM * s;
    if (x < -r || y < -r || x > w + r || y > ht + r) continue;
    ctx.moveTo(x + r, y); ctx.arc(x, y, r, 0, 2 * Math.PI);
  }
  ctx.fill();
}

// city: a function returning {tracts, stops | null} or a promise of it; called only when root has a map to draw.
export function mount(root, city) {
  const fresh = $$('.exmap canvas[data-view]', root).filter(c => !c.dataset.on);
  if (!fresh.length) return;
  fresh.forEach(c => { c.dataset.on = '1'; });
  Promise.resolve(city()).then(
    data => fresh.forEach(c => new ResizeObserver(() => backdrop(c, data)).observe(c)),
    err => console.error(`The map backdrop could not load: ${err.message}`));
}

// A numbered resource on the map and its line under "What could work instead" light up together.
export function linkResources(e) {
  const n = e.target.closest && e.target.closest('[data-res]');
  if (!n) return;
  const on = e.type === 'mouseover' || e.type === 'focusin';
  $$(`[data-res="${CSS.escape(n.dataset.res)}"]`, n.closest('.pitem') || document).forEach(x => x.classList.toggle('hl', on));
}

const SHAPE = {
  kept: h`<svg viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="5"/></svg>`,
  no_show: h`<svg viewBox="0 0 14 14" aria-hidden="true"><path d="M3 3l8 8M11 3l-8 8"/></svg>`,
  other: h`<svg viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/></svg>`,
};
export function visitStrip({ appts, cited, depts }) {
  const oneYear = new Set(appts.map(a => (a.date || '').slice(0, 4))).size === 1;
  const day = iso => oneYear ? date(iso).replace(/, \d{4}$/, '') : date(iso);
  const missed = appts.filter(a => a.status === 'no_show').length;
  return h`<span class="strip-count">${cap(`${missed} of ${appts.length} ${depts.join(' and ')} visits missed since ${day(appts[0].date)}`)}</span>
    <span class="strip">${appts.map(a => {
      const shape = SHAPE[a.status] ? a.status : 'other';
      return h`<button type="button" class="mk ${shape}${cited.has(a.ref) ? ' cited' : ''}" data-ref="${a.ref}" title="${date(a.date)}, ${clock(a.time)}, ${cap(a.dept)}: ${(a.status || 'no status').replace('_', ' ')}">${SHAPE[shape]}<span>${day(a.date)}</span></button>`;
    })}</span>`;
}

export function countyBar(f, { compact = false } = {}) {
  const c = f.county;
  if (!c || f.number == null || !(c.max > 0)) return '';
  const pct = v => Math.max(0, Math.min(100, v / c.max * 100));
  const edge = p => p < 10 ? ' s' : p > 90 ? ' e' : '';
  const v = pct(f.number), m = pct(c.median), z = pct(c.p75);
  return h`<span class="cbar${compact ? ' compact' : ''}" title="Where this tract stands among Cook County census tracts, from 0 to the highest. Shaded: the county's top quarter.">
    ${compact ? '' : h`<span class="cbar-lab here${edge(v)}" style="left:${r1(v)}%">This tract <b>${f.value}</b></span>`}
    <span class="cbar-track"><span class="cbar-zone" style="left:${r1(z)}%"></span><span class="cbar-med" style="left:${r1(m)}%"></span><span class="cbar-dot" style="left:${r1(v)}%"></span></span>
    <span class="cbar-lab med${edge(m)}" style="left:${r1(m)}%">County median ${c.median_text}</span>
  </span>`;
}
