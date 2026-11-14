// Clinic page, "Need against help": where a barrier is flagged and little help is near, ending in something the clinic can do.
//   gaps(P, state, sel) -> h`...`   P: the panel (GET /api/app/panel); state: {barrier, order, tract}; sel: the selected tract id
// Rows are buttons with data-go-barrier and data-go-tract; the page handles the click.
import { cap, h, hours, plural } from './ui.js';
import { HELPS, PLACE_KINDS, kindName, tractName } from './clinic_words.js';

const ROWS = 6, OPEN = 3, MIN_FLAGS = 3, PER_BARRIER = 2;
const USE = Object.keys(PLACE_KINDS), use = k => USE.includes(k) ? USE.indexOf(k) : USE.length;
const kindWords = (k, n) => (PLACE_KINDS[k] || [k, k])[n === 1 ? 0 : 1];

// near is null when our tables locate no help for the barrier at all, 0 when they do and none is within a mile.
export function gap(t, barrier, located) {
  const flags = t.barriers[barrier] || 0, near = located ? t.places_near?.[barrier] ?? 0 : null;
  return { flags, of: t.reviewed, near, perPlace: near ? flags / near : null };
}
// Nothing near (or nothing we can locate) first, by flags; then flags per place within a mile.
const worse = (a, b) => !!a.g.near - !!b.g.near || (b.g.perPlace ?? 0) - (a.g.perPlace ?? 0) || b.g.flags - a.g.flags
  || a.t.tract_id.localeCompare(b.t.tract_id);
const pairs = (P, b) => P.tracts.map(t => ({ t, b, g: gap(t, b.barrier, b.places) })).filter(p => p.g.flags >= MIN_FLAGS).sort(worse);

// One barrier: its widest gaps. All barriers: each barrier's two widest, most flags first, since flags per place
// means nothing for the kinds with no located help.
export function ranked(P, barrier) {
  if (barrier) return pairs(P, P.barriers.find(b => b.barrier === barrier)).slice(0, ROWS);
  return P.barriers.flatMap(b => pairs(P, b).slice(0, PER_BARRIER)).sort((x, y) => y.g.flags - x.g.flags || worse(x, y)).slice(0, ROWS);
}

export function move({ t, b, g }, P) {
  const place = g.near ? [...t.nearest?.[b.barrier] || []].sort((x, y) => use(x.kind) - use(y.kind) || x.miles - y.miles)[0] : null;
  if (place) return { kind: 'place', place };
  const program = P.programs?.[b.barrier]?.[0], noPlace = g.near === 0;
  if (!program) return { kind: 'none', noPlace };
  return { kind: 'program', program, noPlace, host: program.source_url ? new URL(program.source_url).host.replace(/^www\./, '') : null };
}

const tel = phone => phone.replace(/[^\d+]/g, '');
const SHOW = {
  place: ({ place: p }) => h`<p class="cgaps-k">A partner to call</p>
    <p class="cgaps-do"><b>${p.name}</b>, ${kindWords(p.kind, 1)}, ${p.miles} mi</p>
    <p class="cgaps-fine">${p.address} · ${hours(p.hours)} · ${p.phone ? h`<a href="tel:${tel(p.phone)}">${p.phone}</a>` : 'no phone listed'}</p>`,
  program: ({ program: r, host }) => h`<p class="cgaps-k">What the clinic can set up${r.confidence === 'general' ? h` <span class="cgaps-general">general guidance, verify locally</span>` : ''}</p>
    <p class="cgaps-do">${r.how_to_start}</p>
    <p class="cgaps-fine">When: ${r.eligible_when}.${r.caveats ? ` ${r.caveats}` : ''}${host ? ` Source: ${host}.` : ''}</p>`,
  none: () => h`<p class="cgaps-do">No program on file for this.</p>`,
};
const showMove = (m, b) => h`${m.noPlace ? h`<p class="cgaps-none">No place within a mile that could help with ${HELPS[b.barrier]}.</p>` : ''}${SHOW[m.kind](m)}`;

function numbers({ t, b, g }) {
  const need = `${plural(g.flags, `${kindName(b.barrier).toLowerCase()} flag`)} among ${plural(g.of, 'reviewed patient')}`;
  if (g.near == null) return h`${need} · <span class="cgaps-nohelp">no located help in our tables</span>`;
  if (!g.near) return h`${need} · <span class="cgaps-none">no place within a mile</span>`;
  const kinds = Object.entries(t.kinds_near?.[b.barrier] || {}).filter(([, n]) => n).sort(([x], [y]) => use(x) - use(y));
  return h`${need} · ${plural(g.near, 'place')} within a mile: ${kinds.map(([k, n]) => `${n} ${kindWords(k, n)}`).join(', ')}`;
}
const score = g => g.near == null ? plural(g.flags, 'flag') : g.near ? `${g.perPlace.toFixed(1)} flags per place` : h`<span class="cgaps-none">none near</span>`;

function says(P, b, rows) {
  const located = P.barriers.filter(x => x.places).map(x => kindName(x.barrier).toLowerCase());
  if (!b) return `Each barrier's two widest gaps, most flags first. For ${located.slice(0, -1).join(', ')} and ${located.at(-1)} a gap is flags set against the places within a mile; the other barriers have no located help in our tables, so their flags stand alone.`;
  const name = kindName(b.barrier).toLowerCase(), held = rows.reduce((s, p) => s + p.g.flags, 0);
  const hold = rows.length ? ` ${rows.length === 1 ? 'This neighborhood holds' : `These ${rows.length} neighborhoods hold`} ${held} of the panel's ${b.flags} ${name} flags.` : '';
  if (!b.places) return `Our tables have no located help for ${name}, so the rows rank by flags alone.${hold}`;
  const near = P.tracts.map(t => t.places_near?.[b.barrier] ?? 0), none = near.filter(n => !n).length;
  const reach = none ? `${plural(none, 'neighborhood')} ${none === 1 ? 'has' : 'have'} no such place.` : `Every neighborhood has at least ${plural(Math.min(...near), 'such place', 'such places')}.`;
  return `${cap(name)} flags set against the places within a mile that could help with ${HELPS[b.barrier]}, ranked by flags per place.${hold} ${reach}`;
}

export function gaps(P, state, sel) {
  const b = state.barrier && P.barriers.find(x => x.barrier === state.barrier), rows = ranked(P, state.barrier);
  const moves = rows.map(p => move(p, P)), keys = moves.map(m => JSON.stringify(m));
  const row = (p, i) => { const first = keys.indexOf(keys[i]);   // a move already shown above is not repeated in full
    return h`<li class="cgaps-row${i < OPEN && first === i ? ' open' : ''}${p.t.tract_id === sel ? ' on' : ''}">
    <button type="button" class="cgaps-go" data-go-barrier="${p.b.barrier}" data-go-tract="${p.t.tract_id}" aria-pressed="${p.t.tract_id === sel ? 'true' : 'false'}">
      <span class="cgaps-n">${i + 1}</span><span class="cgaps-where">${tractName(p.t.tract_id)}${b ? '' : h`<span> · ${kindName(p.b.barrier)}</span>`}</span>
      <span class="cgaps-score">${score(p.g)}</span><span class="cgaps-nums">${numbers(p)}</span></button>
    ${first < i ? h`<p class="cgaps-same">Same move as row ${first + 1}.</p>` : ''}<div class="cgaps-move">${showMove(moves[i], p.b)}</div></li>`; };
  return h`<h2>Need against help</h2><p class="cgaps-says">${says(P, b, rows)}</p>
    ${rows.length ? h`<ol class="cgaps-list">${rows.map(row)}</ol>` : h`<p class="cgaps-empty">No neighborhood has ${MIN_FLAGS} or more ${kindName(b.barrier).toLowerCase()} flags.</p>`}
    <p class="hint">Neighborhood and barrier pairs with fewer than ${MIN_FLAGS} flags are left out. A patient can carry more than one flag of a kind. Choose a row to see it on the map.</p>`;
}
