// The "atrium" skin's renderer for cinema.surface/1 (2026-11-17): a structurally different layout from Cinema's, from
// the SAME descriptors. Each section is a card in a grid; a section's actions and choices form one horizontal toolbar
// (one focus row: Left/Right along it); facts are chips; a secret field sits inline with its submit; lists are a
// two-column grid of lines, or poster tiles when the descriptor asks for tiles. No feature policy: every word and action
// comes from the descriptor, and actions go back through the runtime's kit.
const make = (doc, tag, cls, text) => { const e = doc.createElement(tag);if (cls) e.className = cls;if (text != null) e.textContent = text;return e; };
// A card through the kit's one card contract (2026-11-19, stage A); an older Cinema without kit.card gets a plain button.
const cardOf = (kit, key, art) => { if (kit.card) return kit.card(key, art);const b = kit.button(key, '');if (art) b.append(art);return b; };

export function render(container, surface, kit) {
  const doc = container.ownerDocument;
  if (container.dataset.surfacePlacement === 'media') {   // the open card's media slot: a stage over the picture, a toolbar
    container.classList.add('atr-surface', 'atr-media');let bar = null;
    for (const item of surface.sections.flatMap(s => s.items)) {
      if (item.kind === 'action' && item.stage) { const b = kit.button(item.id, '');b.className = 'atr-media-stage';b.setAttribute('aria-label', item.label);
        if (item.expanded !== null && item.expanded !== undefined) b.setAttribute('aria-expanded', String(item.expanded));container.append(b);continue; }
      if (item.kind === 'action' || (item.kind === 'text' && bar)) {
        if (!bar) { bar = make(doc, 'div', 'atr-media-bar atr-toolbar');bar.dataset.surfaceRow = '';bar.dataset.navRow = '';container.append(bar); }
        if (item.kind === 'action') { const b = kit.button(item.id, item.label);b.disabled = item.disabled;bar.append(b); } else bar.append(make(doc, 'small', '', item.text));
        continue;
      }
      if (item.kind === 'text') container.append(make(doc, 'p', '', item.text));
    }
    return;
  }
  container.classList.add('atr-surface');
  if (surface.title || surface.lead) {
    const head = make(doc, 'div', 'atr-head');
    if (surface.title) head.append(make(doc, 'h3', '', surface.title));
    if (surface.lead) head.append(make(doc, 'p', '', surface.lead));
    container.append(head);
  }
  if (surface.status?.text) { const s = make(doc, 'div', 'atr-status', surface.status.text);s.dataset.state = surface.status.state;container.append(s); }
  const grid = make(doc, 'div', 'atr-grid');container.append(grid);
  for (const section of surface.sections) {
    const card = make(doc, 'section', 'atr-card');
    if (section.heading) card.append(make(doc, 'h4', '', section.heading));
    let toolbar = null;
    const bar = () => { if (!toolbar) { toolbar = make(doc, 'div', 'atr-toolbar');toolbar.dataset.surfaceRow = '';card.append(toolbar); } return toolbar; };
    for (const item of section.items) {
      if (item.kind === 'text') { toolbar = null;const p = make(doc, 'p', '', item.text);p.dataset.tone = item.tone;card.append(p); }
      else if (item.kind === 'image') { toolbar = null;const f = make(doc, 'div', 'c-artwork atr-picture');f.dataset.shape = item.shape;f.setAttribute('aria-hidden', 'true');kit.artwork(f, item.art);card.append(f); }
      else if (item.kind === 'facts') {
        toolbar = null;const chips = make(doc, 'div', 'atr-chips');
        for (const r of item.rows) { const c = make(doc, 'span', 'atr-chip');c.append(make(doc, 'b', '', r.label), doc.createTextNode(r.value));chips.append(c); }
        card.append(chips);
      } else if (item.kind === 'action') {
        const b = kit.button(item.id, item.note ? `${item.label} — ${item.note}` : item.label);b.dataset.tone = item.tone;b.disabled = item.disabled;
        if (item.expanded !== null && item.expanded !== undefined) b.setAttribute('aria-expanded', String(item.expanded));
        if (item.selected !== null && item.selected !== undefined) b.setAttribute('aria-pressed', String(item.selected));bar().append(b);
      } else if (item.kind === 'choice') {
        const now = item.options.find(o => o.value === item.value)?.label || '';
        const b = kit.button(item.id, `${item.label}: ${now} ▸`);b.disabled = item.disabled;bar().append(b);
      } else if (item.kind === 'secret') {
        toolbar = null;const line = make(doc, 'div', 'atr-secret');line.dataset.surfaceRow = '';
        const input = kit.secretInput(item);input.placeholder = item.label;
        line.append(input, kit.button(item.submit, 'Use'));card.append(line);
        if (item.hint) card.append(make(doc, 'p', '', item.hint));
      } else if (item.kind === 'field') {
        toolbar = null;const line = make(doc, 'div', 'atr-secret');line.dataset.surfaceRow = '';
        const input = kit.fieldInput(item);input.placeholder = item.label;line.append(input);card.append(line);   // its submit is the module's own declared action
      } else if (item.kind === 'file') {
        bar().append(kit.fileButton(item));
      } else if (item.kind === 'list') {
        toolbar = null;
        if (item.label) card.append(make(doc, 'h4', '', item.label));
        if (!item.items.length && item.empty) card.append(make(doc, 'p', '', item.empty));
        const box = make(doc, 'div', item.layout === 'tiles' ? 'atr-tiles' : 'atr-lines');
        for (const entry of item.items) {
          const tile = item.layout === 'tiles', art = item.layout !== 'rows' || entry.art ? make(doc, 'div', 'c-artwork') : null;
          if (art) { art.setAttribute('aria-hidden', 'true');kit.artwork(art, entry.art); }
          // Tiles are cards (the kit's one card contract); rows stay rows.
          const b = tile ? cardOf(kit, `${item.id}:${entry.id}`, art) : kit.button(`${item.id}:${entry.id}`, '');
          b.className = tile ? 'atr-tile' : 'atr-line';b.disabled = entry.disabled;if (art && !tile) b.append(art);
          if (entry.current) b.setAttribute('aria-current', 'true');
          b.append(make(doc, 'span', '', entry.badge ? `${entry.badge} ${entry.title}` : entry.title));
          const sub = [entry.subtitle, entry.meta].filter(Boolean).join(' · ');if (sub) b.append(make(doc, 'small', '', sub));
          box.append(b);
        }
        card.append(box);
        if (item.more) { const more = make(doc, 'div', 'atr-toolbar');more.dataset.surfaceRow = '';more.append(kit.button(item.more, item.moreLabel));card.append(more); }
      }
    }
    grid.append(card);
  }
}

// The title card's parts in Atrium's own structure (2026-11-17, review of bh): each section a bordered panel with a small
// heading; a shelf a wrapping grid of compact tiles (art beside the words); facts as label chips. Same meaning, actions,
// order and navigation (the page's), materially different drawing.
export const cardParts = {
  section(doc, {heading, rowed}) {
    const root = make(doc, 'section', 'atr-card-section'), h = make(doc, 'h4', '', heading);
    const row = make(doc, 'div', rowed ? 'atr-card-row' : 'atr-chips');
    root.append(h, row);return {root, row, heading: h};
  },
  tile(doc, {title, note, shape}) {
    const button = make(doc, 'button', 'atr-card-tile');button.dataset.atrShape = shape;
    const art = make(doc, 'div', 'atr-card-art'), words = make(doc, 'span', 'atr-card-words');
    words.append(make(doc, 'b', '', title));if (note) words.append(make(doc, 'small', '', note));
    button.append(art, words);return {button, art};
  },
  fact(doc, row, label, value) { const c = make(doc, 'span', 'atr-chip');c.append(make(doc, 'b', '', label), doc.createTextNode(value));row.append(c); },
};
