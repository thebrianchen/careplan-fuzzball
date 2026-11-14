// Clinic page, "Public data against the exam room": what a neighborhood's public figures point to, against what the plans' reviews flagged.
//   publicView(P, state, sel) -> h`...`   P: the panel (GET /api/app/panel); state: {barrier, order, tract}; sel: the selected tract id
// Buttons carry data-go-barrier / data-go-tract; the page handles the clicks.
import { h, plural } from './ui.js';
import { countyBar } from './exhibits.js';
import { andList, hoodName, kindName, pct, tractName } from './clinic_words.js';

const TOP = 5;
const lower = k => kindName(k).toLowerCase();
const rate = (t, kind) => ({ t, n: t.barriers[kind] || 0, of: t.reviewed, share: t.reviewed ? (t.barriers[kind] || 0) / t.reviewed : 0 });
// A figure says so when it is past our rules' line: a ">=" rule writes the county's 75th percentile in the note, a USDA flag reads yes.
const saysSo = f => !!f.note || f.value === 'yes';

// The panel-wide comparison: every kind the reviews flag, plus any public kind they never flagged.
export function sides(P) {
  const kinds = [...P.barriers.map(b => ({ kind: b.barrier, flags: b.flags })),
    ...P.public_kinds.filter(k => !P.barriers.some(b => b.barrier === k)).map(kind => ({ kind, flags: 0 }))]
    .map(k => ({ ...k, measured: P.public_kinds.includes(k.kind), pointsIn: P.tracts.filter(t => t.public_points_to.includes(k.kind)).length,
      // lowest to highest; ties put the larger neighborhood last so the extremes quoted are the best supported
      rates: P.tracts.map(t => rate(t, k.kind)).sort((a, b) => a.share - b.share || a.of - b.of) }));
  const sum = ks => ks.reduce((s, k) => s + k.flags, 0);
  const measured = kinds.filter(k => k.measured), unmeasured = kinds.filter(k => !k.measured);
  return { hoods: P.tracts.length, kinds, measured, unmeasured, flags: sum(kinds), measuredFlags: sum(measured), unmeasuredFlags: sum(unmeasured),
    alike: P.tracts.filter(t => P.public_kinds.every(k => t.public_points_to.includes(k))).length };
}

// One neighborhood: what its public figures point to, what its visits flagged most, and which of the cases that is.
export function disagreement(t, P) {
  const flagged = P.barriers.map(b => ({ kind: b.barrier, n: t.barriers[b.barrier] || 0 })).filter(x => x.n)
    .sort((a, b) => b.n - a.n);                       // stable: ties keep the panel's order
  const top = flagged[0] || null;
  const points = P.public_kinds.filter(k => t.public_points_to.includes(k));
  const kase = !top ? 'none' : points.includes(top.kind) ? 'pointed' : P.public_kinds.includes(top.kind) ? 'quiet' : 'unmeasured';
  return { t, points, flagged, top, tied: top ? flagged.slice(1).filter(x => x.n === top.n).map(x => x.kind) : [], case: kase };
}

const panelText = S => S.unmeasuredFlags
  ? `${S.unmeasuredFlags} of ${S.flags} flags (${pct(S.unmeasuredFlags / S.flags)}) are for barriers no public figure measures.`
  : `Every one of the ${S.flags} flags is for a barrier a public figure measures.`;

function spreadText(S) {
  const widest = S.measured.reduce((a, k) => k.rates.at(-1).share - k.rates[0].share > a.rates.at(-1).share - a.rates[0].share ? k : a);
  const lo = widest.rates[0], hi = widest.rates.at(-1);
  const same = S.alike > 1 ? `The public figures point to all ${S.measured.length} of these in ${S.alike} of the ${S.hoods} neighborhoods, so they rank those neighborhoods alike.` : '';
  const visits = hi.share > lo.share
    ? `${same ? 'The visits do not: ' : 'In the visits, '}${lower(widest.kind)} ranges from ${lo.n} of ${lo.of} reviewed patients in ${hoodName(lo.t)} to ${hi.n} of ${hi.of} in ${hoodName(hi.t)}.`
    : '';
  return [same, visits].filter(Boolean).join(' ');
}

const pointsText = d => d.points.length
  ? `The public figures point to ${andList(d.points.map(lower))}.`
  : 'None of the public figures here reaches the line our rules use.';
const visitsText = d => d.top
  ? `The visits flagged ${lower(d.top.kind)} most: ${plural(d.top.n, 'flag')} among ${d.t.reviewed} reviewed patients${d.tied.length ? `, tied with ${andList(d.tied.map(lower))}` : ''}.`
  : `The visits flagged no barrier among ${d.t.reviewed} reviewed patients.`;
const CASE = {
  pointed: () => 'A public figure here points to it too.',
  quiet: () => 'Public figures measure it, but none here points to it.',
  unmeasured: () => 'No public figure here measures it.',
  none: () => '',
};
const hoodText = d => [pointsText(d), visitsText(d), CASE[d.case](d)].filter(Boolean).join(' ');

const kindButton = (kind, n, state, tract) => h`<button type="button" class="pk${kind === state.barrier ? ' on' : ''}" data-go-barrier="${kind}"${tract ? h` data-go-tract="${tract}"` : ''}>
  <span>${kindName(kind)}</span>${n == null ? '' : h`<b>${n}</b>`}</button>`;

function split(S, state) {
  const side = (cls, title, n, ks) => h`<div class="pside ${cls}"><span class="k"><i></i>${title}</span>
    <b class="pn">${n} <small>${pct(S.flags ? n / S.flags : 0)}</small></b>
    <div class="pks">${ks.map(k => kindButton(k.kind, k.flags, state))}</div></div>`;
  const w = S.flags ? S.measuredFlags / S.flags * 100 : 50;
  return h`<p class="plead">${panelText(S)}</p>
    <div class="psplit" role="img" aria-label="${S.measuredFlags} flags of kinds a public figure measures, ${S.unmeasuredFlags} of kinds none measures">
      <span class="pm" style="width:${w.toFixed(1)}%"></span><span class="pu" style="width:${(100 - w).toFixed(1)}%"></span></div>
    <div class="psides">${side('m', 'A public figure measures', S.measuredFlags, S.measured)}${side('u', 'No public figure measures', S.unmeasuredFlags, S.unmeasured)}</div>`;
}

function spread(S, state) {
  const most = Math.max(...S.measured.flatMap(k => k.rates.map(r => r.share)), 0.01), n = S.hoods, W = 4;
  const row = k => h`<li class="prow">${kindButton(k.kind, null, state)}
    <span class="pstrips">
      <svg class="ppub" viewBox="0 0 ${n * W} 6" preserveAspectRatio="none" aria-hidden="true">${k.rates.map((r, i) =>
        h`<rect x="${i * W}" width="${W - 1}" height="6" class="${r.t.public_points_to.includes(k.kind) ? 'on' : ''}"/>`)}</svg>
      <svg class="pvis" viewBox="0 0 ${n * W} 24" preserveAspectRatio="none" aria-hidden="true">${k.rates.map((r, i) => {
        const y = Math.round(24 - r.share / most * 23); return h`<rect x="${i * W}" y="${y}" width="${W - 1}" height="${24 - y}"/>`; })}</svg></span>
    <span class="pnums"><b>${k.pointsIn} of ${n}</b><span>${pct(k.rates[0].share)}–${pct(k.rates.at(-1).share)}</span></span></li>`;
  return h`<h3>Why public figures alone cannot rank these neighborhoods</h3>
    <div class="pkey"><span><i class="ppub-key"></i>Public figures point to it</span><span><i class="pvis-key"></i>The visits' flags per reviewed patient</span>
      <span class="hint">One column per neighborhood, lowest to highest in the visits</span></div>
    <ol class="prows">${S.measured.map(row)}</ol>
    <p class="pcap">${spreadText(S)}</p>`;
}

function hood(d, P, state) {
  const t = d.t, sel = t.tract_id;
  const figures = kind => {
    const fs = t.facts.filter(f => P.public_refs[kind]?.includes(f.ref) && saysSo(f));
    return fs.length ? fs.map(f => h`<div class="pfig"><span>${f.label}</span> <b>${f.value}</b>${countyBar(f, { compact: true })}</div>`)
      : h`<div class="pfig hint">The figure our rules read for this is not listed here.</div>`;
  };
  return h`<h3>${hoodName(t)} <span class="hint">${plural(t.reviewed, 'reviewed patient')}</span></h3>
    <p class="psay" data-case="${d.case}">${hoodText(d)}</p>
    <div class="phood">
      <div class="pcol pub"><span class="k">Public figures point to</span>
        ${d.points.length ? d.points.map(k => h`<div class="ppt">${kindButton(k, null, state, sel)}${figures(k)}</div>`) : h`<p class="hint">Nothing: no figure here is past our rules' line.</p>`}</div>
      <div class="pcol vis"><span class="k">The visits flagged</span>
        ${d.flagged.length ? h`<ol class="pflag">${d.flagged.slice(0, TOP).map(x => h`<li>${kindButton(x.kind, null, state, sel)}
          ${P.public_kinds.includes(x.kind) ? '' : h`<em class="pno">no public figure</em>`}<b>${x.n} of ${t.reviewed}</b></li>`)}</ol>` : h`<p class="hint">No flags.</p>`}</div>
    </div>`;
}

export function publicView(P, state, sel) {
  const S = sides(P), t = P.tracts.find(x => x.tract_id === sel);
  return h`<h2>Public data against the exam room</h2>
    <p class="hint">Public figures describe a neighborhood. The visits describe the patients in it. A patient can carry more than one flag.</p>
    <div class="pblock" id="public-split">${split(S, state)}</div>
    <div class="pblock" id="public-spread">${spread(S, state)}</div>
    ${t ? h`<div class="pblock" id="public-hood">${hood(disagreement(t, P), P, state)}</div>` : ''}`;
}
