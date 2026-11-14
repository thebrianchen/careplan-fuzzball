// Small helpers shared by every view. No framework: tagged templates that escape by default.
class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ESC[c]);
export const raw = s => new Raw(String(s));
const val = v => v == null || v === false ? '' : v instanceof Raw ? v.s : Array.isArray(v) ? v.map(val).join('') : esc(v);
// h`<p>${text}</p>`: every interpolation is escaped unless it is another h`...`, raw(...) or an array of those.
export const h = (strings, ...vals) => new Raw(strings.reduce((out, s, i) => out + s + (i < vals.length ? val(vals[i]) : ''), ''));

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export async function api(path, body) {
  const r = await fetch('/api/app/' + path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `The server answered ${r.status}.`);
  return data;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
export function date(iso) {                        // '2026-10-21' or an ISO timestamp -> 'Oct 21, 2026' (no timezone shifts)
  const m = /^(\d{4})-(\d\d)-(\d\d)/.exec(iso || '');
  return m ? `${MONTHS[+m[2] - 1]} ${+m[3]}, ${m[1]}` : (iso || '');
}
export function clock(hhmm) {                      // '15:00' -> '3:00 PM'
  const m = /^(\d\d):(\d\d)/.exec(hhmm || '');
  return m ? `${(+m[1] + 11) % 12 + 1}:${m[2]} ${+m[1] < 12 ? 'AM' : 'PM'}` : (hhmm || '');
}
export const timeOfDay = iso => iso ? new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' }) : '';
export const secs = s => s == null ? '—' : `${Math.round(s)} s`;
export const cap = s => s ? s[0].toUpperCase() + s.slice(1) : '';
export const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;

export function setPlanner(p) {                    // the pill in the top bar
  const el = $('#planner');
  if (!p || !el) return;
  el.hidden = false;
  el.className = 'tag planner ' + (p.answers ? 'up' : 'down');
  el.textContent = `Planner ${p.answers ? 'answering' : 'not answering'}`;
  el.title = `${p.profile} (${p.model}) at ${p.host}. This checks that the endpoint answers, not that it generates.`;
}
