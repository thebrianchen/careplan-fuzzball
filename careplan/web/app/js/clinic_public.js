// Clinic page, "Public data against the exam room": what a neighborhood's public figures point to, against what the plans' reviews flagged.
//   publicView(P, state, sel) -> h`...`   P: the panel (GET /api/app/panel); state: {barrier, order, tract}; sel: the selected tract id
// The page repaints this card on every state change and handles clicks on [data-tract] and [data-barrier] itself.
import { h } from './ui.js';

export function publicView(P, state, sel) {
  return h`<h2>Public data against the exam room</h2>`;
}
