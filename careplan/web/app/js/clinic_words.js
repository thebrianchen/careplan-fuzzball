// Words the clinic page's parts share: how a barrier, a neighborhood and a share are written.
import { cap } from './ui.js';

const LABEL = { med_cost: 'Medication cost', transport: 'Transportation', language_literacy: 'Language or literacy' };
export const HELPS = { food_access: 'food', med_cost: 'medication costs', utilities: 'a utility shutoff' };   // "places that could help with ..."
export const kindName = kind => LABEL[kind] || cap(kind.replace(/_/g, ' '));     // 'food_access' -> 'Food access'
export const nameOf = b => kindName(b.barrier);                                   // b: one of P.barriers
export const tractName = id => `Tract ${+id.slice(5, 9)}${id.slice(9) === '00' ? '' : `.${id.slice(9)}`}`;
export const pct = x => `${Math.round(x * 100)}%`;
