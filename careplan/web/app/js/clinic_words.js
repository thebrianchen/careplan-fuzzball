// Words the clinic page's parts share: how a barrier, a census tract, its neighborhood and a share are written.
import { cap } from './ui.js';

const LABEL = { med_cost: 'Medication cost', transport: 'Transportation', language_literacy: 'Language or literacy' };
export const HELPS = { food_access: 'food', med_cost: 'medication costs', utilities: 'a utility shutoff' };   // "places that could help with ..."
export const kindName = kind => LABEL[kind] || cap(kind.replace(/_/g, ' '));     // 'food_access' -> 'Food access'
export const nameOf = b => kindName(b.barrier);                                   // b: one of P.barriers
export const tractName = id => `Tract ${+id.slice(5, 9)}${id.slice(9) === '00' ? '' : `.${id.slice(9)}`}`;
export const hoodName = t => t.area ? `${tractName(t.tract_id)} in ${t.area}` : tractName(t.tract_id);   // t: one of P.tracts
export const pct = x => `${Math.round(x * 100)}%`;
export const andList = xs => xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs.at(-1)}`;   // ['a','b','c'] -> 'a, b and c'
// Kinds of located place, most useful first (for food a pantry before a grocery that takes SNAP): [one, many].
export const PLACE_KINDS = {
  food_pantry: ['food pantry', 'food pantries'], snap_grocery: ['grocery that takes SNAP', 'groceries that take SNAP'],
  fqhc: ['community health center', 'community health centers'],
  warming_center: ['warming center', 'warming centers'], cooling_center: ['cooling center', 'cooling centers'],
};
