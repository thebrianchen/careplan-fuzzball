// Presentation for the clinician's draft. Review results retain the original cards and item IDs.
import { h, cap, date } from './ui.js';

export const CATEGORIES = ['appointment', 'diet', 'medication', 'activity', 'referral', 'monitoring', 'education'];
export function avatar() {
  return h`<svg class="patient-avatar" viewBox="0 0 96 96" role="img" aria-label="Placeholder patient portrait">
    <circle cx="48" cy="48" r="48" fill="#dcece9"/><circle cx="48" cy="35" r="17" fill="#7e9995"/>
    <path d="M13 96v-9c0-21 15-33 35-33s35 12 35 33v9" fill="#607f7b"/></svg>`;
}

export function setupVisitLayout(root, doc) {
  const page = root.querySelector('.page.visit'), banner = page.querySelector('.banner');
  const context = page.querySelector('.context'), tabs = context.querySelector('.tabs');
  const review = page.querySelector('.review'), plan = page.querySelector('#plan');
  const back = banner.querySelector('.back');
  back.remove();
  page.insertAdjacentHTML('afterbegin', h`<nav class="page-back breadcrumb" aria-label="Breadcrumb">
    <a href="#/">Visits</a><span aria-hidden="true">/</span><span aria-current="page">${doc.patient.name} · Visit</span></nav>`);
  banner.innerHTML = h`<div class="patient-identity">${avatar()}<div class="who"><h1>${doc.patient.name}</h1>
    <span>${[doc.patient.age != null && `Age ${doc.patient.age}`, doc.patient.sex, doc.patient.language].filter(Boolean).join(' · ')}</span></div></div>
    <div class="patient-facts">
      ${doc.patient.coverage ? h`<div class="patient-fact"><b>Insurance</b><span>${doc.patient.coverage}</span></div>` : ''}
      ${doc.patient.pcp ? h`<div class="patient-fact"><b>Primary care</b><span>${doc.patient.pcp}</span></div>` : ''}
    </div><div class="condition-label">Conditions on record</div>
    <div class="problems">${doc.patient.problems.map(p => h`<span>${p}</span>`)}</div>`;

  // Move existing chart nodes (not copies), so evidence links still find their original data-ref.
  const chart = context.querySelector('[data-pane=chart]');
  const sections = [];
  for (const node of [...chart.children]) {
    if (node.tagName === 'H3') sections.push({ title: node.textContent.toLowerCase(), nodes: [node] });
    else if (sections.length) sections.at(-1).nodes.push(node);
  }
  chart.replaceChildren();
  for (const section of sections) {
    const key = section.title.startsWith('medications') ? 'medications' : section.title.startsWith('social needs') ? 'social' : 'chart';
    let pane = context.querySelector(`[data-pane="${key}"]`);
    if (!pane) {
      pane = document.createElement('div'); pane.className = 'pane'; pane.dataset.pane = key; pane.hidden = true;
      context.append(pane);
    }
    pane.append(...section.nodes);
  }
  const tabOrder = [['visit', 'Transcript'], ['chart', 'Notes'], ['medications', 'Medications'], ['social', 'Social needs'], ['hood', 'Neighborhood']];
  tabs.innerHTML = tabOrder.map(([key, title]) => h`<button type="button" role="tab" data-tab="${key}" aria-selected="${key === 'visit'}" class="${key === 'visit' ? 'on' : ''}">${title}</button>`).join('');
  for (const [key, title] of tabOrder) {
    if (!context.querySelector(`[data-pane="${key}"]`)) context.insertAdjacentHTML('beforeend', h`<div class="pane" data-pane="${key}" hidden><p class="pane-note">No ${title.toLowerCase()} on record.</p></div>`);
  }
  context.querySelector('[data-pane=visit] .pane-note').outerHTML = h`<div class="visit-summary">
    <button type="button" class="visit-picker" aria-disabled="true" aria-label="Current visit: ${doc.visit.setting}, ${date(doc.visit.date)}">
      <span class="visit-picker-copy"><span class="visit-picker-title">${cap(doc.visit.setting)}</span><span class="visit-picker-date">${date(doc.visit.date)}</span></span>
      <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>
    </button></div>`;
  context.querySelectorAll('[data-pane=medications] .rowline').forEach(row => {
    const small = row.querySelector('small'), cost = small?.textContent.match(/\$[\d,.]+ a month/)?.[0];
    const warning = small?.querySelector('.warn')?.textContent;
    small?.remove();
    const description = document.createElement('div'); description.className = 'med-description';
    description.append(...row.childNodes); row.append(description);
    if (cost) row.insertAdjacentHTML('beforeend', h`<span class="med-cost">${cost}</span>`);
    if (warning) row.insertAdjacentHTML('beforeend', h`<span class="med-refill">${warning}</span>`);
  });

  let scroll = null;
  tabs.addEventListener('pointerdown', () => { scroll = { x: window.scrollX, y: window.scrollY }; });
  tabs.addEventListener('click', event => {
    const tab = event.target.closest('[data-tab]'); if (!tab) return;
    event.preventDefault(); event.stopPropagation();
    const position = scroll || { x: window.scrollX, y: window.scrollY }; scroll = null;
    context.style.minHeight = Math.max(context.offsetHeight, parseFloat(context.style.minHeight) || 0) + 'px';
    context.querySelectorAll('[data-tab]').forEach(button => {
      const active = button === tab; button.classList.toggle('on', active); button.setAttribute('aria-selected', String(active));
    });
    context.querySelectorAll('[data-pane]').forEach(pane => { pane.hidden = pane.dataset.pane !== tab.dataset.tab; });
    window.scrollTo({ left: position.x, top: position.y, behavior: 'instant' });
  });

  const go = review.querySelector('#go'), boundary = review.querySelector('#boundary');
  const action = document.createElement('div'); action.className = 'review-action';
  go.before(action); action.append(go, boundary);
  boundary.setAttribute('role', 'tooltip'); go.setAttribute('aria-describedby', 'boundary');
  review.querySelector('.review-head p').hidden = true;

  const originals = [...plan.querySelectorAll('.pitem')].map(item => ({ item, text: item.querySelector('.ptext'), html: item.querySelector('.ptext').innerHTML }));
  const byId = new Map(doc.plan.map(item => [item.item_id, item]));
  const draftOrder = [];
  for (const category of CATEGORIES) {
    const entries = originals.filter(entry => byId.get(entry.item.id.slice(2)).type === category);
    for (const entry of entries) {
      const value = byId.get(entry.item.id.slice(2)).text;
      entry.draft = h`<label class="plan-field-label" for="draft-${entry.item.id}">${cap(category)}</label><textarea class="plan-field" id="draft-${entry.item.id}" rows="2" readonly>${value}</textarea>`;
      draftOrder.push(entry.item);
    }
    if (!entries.length) {
      const blank = document.createElement('li'); blank.className = 'blank-category';
      blank.innerHTML = h`<label class="plan-field-label" for="draft-${category}">${cap(category)}</label><textarea class="plan-field" id="draft-${category}" rows="2" readonly></textarea>`;
      draftOrder.push(blank);
    }
  }
  let previous = null;
  return {
    setResults(result) {
      if (result === previous) return; previous = result;
      review.classList.toggle('review-results', result);
      originals.forEach(entry => { entry.text.innerHTML = result ? entry.html : entry.draft; });
      if (result) originals.forEach(entry => plan.append(entry.item)); else plan.append(...draftOrder);
      review.querySelector('.review-head h2').textContent = result ? 'The plan' : 'Draft Plan';
      if (!result) requestAnimationFrame(() => plan.querySelectorAll('.plan-field').forEach(field => {
        field.style.height = 'auto'; field.style.height = Math.max(66, field.scrollHeight + 2) + 'px';
      }));
    },
  };
}
