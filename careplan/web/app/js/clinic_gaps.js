// Clinic page, "Need against help": where a barrier is flagged and little help is near, ending in something the clinic can do.
//   gaps(P, state, sel) -> h`...`   P: the panel (GET /api/app/panel); state: {barrier, order, tract}; sel: the selected tract id
// The page repaints this card on every state change and handles clicks on [data-tract] and [data-barrier] itself.
import { h } from './ui.js';

export function gaps(P, state, sel) {
  return h`<h2>Need against help</h2>`;
}
