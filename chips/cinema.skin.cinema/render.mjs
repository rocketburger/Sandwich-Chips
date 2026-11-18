// The Cinema skin's renderer for cinema.surface/1 (2026-11-17). The skin owns drawing: this turns a validated semantic
// surface into the same quiet stacked rows Cinema's Settings and views always used (utility rows: a label and a note,
// fact lists, plain paragraphs), so the default look is unchanged. It holds no feature policy: every word and action
// comes from the descriptor; actions go back through the runtime's kit (ids only).
const make = (doc, tag, cls, text) => { const e = doc.createElement(tag);if (cls) e.className = cls;if (text != null) e.textContent = text;return e; };

// The skin's own small line icons for the descriptor's neutral icon hints (2026-11-18, review of bi: the Controller panel's
// approved icons, now drawn by the skin). Unknown names draw nothing. Built as SVG elements, never parsed markup.
const ICONS = {
  gamepad: [['path', {d: 'M7 8h10a4 4 0 0 1 4 4.5l-.6 3.3a2.5 2.5 0 0 1-4.3 1.2L14.5 15h-5l-1.6 2a2.5 2.5 0 0 1-4.3-1.2L3 12.5A4 4 0 0 1 7 8z'}],
    ['path', {d: 'M8 11v3M6.5 12.5h3'}], ['circle', {cx: 15.5, cy: 11.5, r: .6}], ['circle', {cx: 17, cy: 13.5, r: .6}]],
  buttons: [['circle', {cx: 12, cy: 6, r: 2.3}], ['circle', {cx: 12, cy: 18, r: 2.3}], ['circle', {cx: 6, cy: 12, r: 2.3}], ['circle', {cx: 18, cy: 12, r: 2.3}]],
  touchpad: [['rect', {x: 4, y: 6, width: 16, height: 12, rx: 3}], ['circle', {cx: 12, cy: 12, r: .9}]],
  motion: [['circle', {cx: 12, cy: 12, r: 6}], ['circle', {cx: 12, cy: 12, r: 1}], ['path', {d: 'M12 3v3M12 18v3M3 12h3M18 12h3'}]],
  settings: [['circle', {cx: 12, cy: 12, r: 3}], ['path', {d: 'M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8'}]],
  forward: [['path', {d: 'M9 6l6 6-6 6'}]],
  back: [['path', {d: 'M15 6l-6 6 6 6'}]],
};
const SVG = 'http://www.w3.org/2000/svg';
function iconOf(doc, name, cls = 'cs-icon') {
  const parts = ICONS[name];
  if (!parts || typeof doc.createElementNS !== 'function') return null;
  const span = make(doc, 'span', cls);span.setAttribute('aria-hidden', 'true');
  const svg = doc.createElementNS(SVG, 'svg');
  for (const [k, v] of Object.entries({viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.7', 'stroke-linecap': 'round', 'stroke-linejoin': 'round'})) svg.setAttribute(k, v);
  for (const [tag, attrs] of parts) { const e = doc.createElementNS(SVG, tag);for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));svg.append(e); }
  span.append(svg);return span;
}

function row(doc, kit, key, label, note, extra = {}) {
  const b = kit.button(key, '');b.className = 'utility-row plain' + (extra.tone ? ` cs-${extra.tone}` : '');
  const copy = make(doc, 'span');copy.append(make(doc, 'strong', '', label));
  if (note) copy.append(make(doc, 'small', '', note));
  b.replaceChildren(copy);
  // Hints: an icon before the words, and a chevron for an action that opens a screen of its own.
  const before = extra.icon ? iconOf(doc, extra.icon) : null, after = extra.opens ? iconOf(doc, 'forward', 'cs-icon cs-chevron') : null;
  if (before) { b.prepend(before);b.classList.add('cs-iconed'); }
  if (after) { b.append(after);b.classList.add('cs-iconed'); }
  if (extra.disabled) { b.disabled = true;b.setAttribute('aria-disabled', 'true'); }
  return b;
}

// In the player's slots (a module's entry points or a panel the player expands; 2026-11-17, review of bh): Cinema's player
// look. A surface with a title is a panel (its title, then its body); one without is a row of entry points.
function renderPlayer(container, surface, kit) {
  const doc = container.ownerDocument, panel = !!surface.title;
  container.classList.add(panel ? 'c-panel' : 'c-strip', panel ? 'cs-player-panel' : 'cs-player-strip');
  if (surface.status?.state === 'pending') container.setAttribute('aria-busy', 'true');else container.removeAttribute('aria-busy');
  let body = container;
  if (panel) { container.append(make(doc, 'h3', '', surface.title));container.setAttribute('aria-label', surface.title);body = make(doc, 'div', 'c-panel-body');container.append(body); }
  else { container.dataset.surfaceRow = '';container.setAttribute('role', 'group'); }
  if (surface.lead) body.append(make(doc, 'p', 'cs-player-note', surface.lead));
  for (const section of surface.sections) {
    if (section.heading) body.append(make(doc, 'h4', 'cs-player-heading', section.heading));
    let row = null;
    const actions = () => { if (!panel) return body;if (!row) { row = make(doc, 'div', 'cs-player-actions');row.dataset.surfaceRow = '';body.append(row); } return row; };
    for (const item of section.items) {
      if (item.kind === 'action' || item.kind === 'choice') {
        const label = item.kind === 'choice' ? `${item.label}: ${item.options.find(o => o.value === item.value)?.label || ''}` : item.label;
        const b = kit.button(item.id, label);b.className = 'c-button';b.disabled = item.disabled;if (item.note) b.title = item.note;
        if (item.expanded !== null && item.expanded !== undefined) b.setAttribute('aria-expanded', String(item.expanded));actions().append(b);continue;
      }
      row = null;
      if (item.kind === 'text') { const p = make(doc, 'p', 'cs-player-note', item.text);p.dataset.tone = item.tone;body.append(p); }
      else if (item.kind === 'facts') { const g = make(doc, 'div', 'cs-player-facts');for (const r of item.rows) g.append(make(doc, 'span', 'cs-fact-label', r.label), make(doc, 'span', 'cs-fact-value', r.value));body.append(g); }
      else if (item.kind === 'image') { const f = make(doc, 'div', 'cs-player-picture');f.dataset.shape = item.shape;f.setAttribute('aria-hidden', 'true');kit.artwork(f, item.art);body.append(f); }
      else if (item.kind === 'list' && item.layout === 'people') {
        const people = make(doc, 'div', 'cs-people');
        for (const e of item.items) {
          const person = make(doc, 'div', 'cs-person'), face = make(doc, 'span', 'cs-face');face.setAttribute('aria-hidden', 'true');
          if (e.art) kit.artwork(face, e.art);else face.textContent = e.badge || '';
          person.append(face, make(doc, 'span', 'cs-person-name', e.title));if (e.subtitle) person.append(make(doc, 'span', 'cs-person-role', e.subtitle));people.append(person);
        }
        body.append(people);
      } else if (item.kind === 'list') {
        const list = make(doc, 'div', 'cs-entries' + (item.layout === 'tiles' ? ' cs-entry-tiles' : ''));list.setAttribute('role', 'group');if (item.label) list.setAttribute('aria-label', item.label);
        for (const e of item.items) {
          const b = kit.button(`${item.id}:${e.id}`, '');b.className = 'c-button cs-entry';b.disabled = e.disabled;
          if (e.art) { const t = make(doc, 'div', 'cs-entry-thumb');t.setAttribute('aria-hidden', 'true');kit.artwork(t, e.art);b.append(t); }
          if (e.badge) b.append(make(doc, 'span', 'cs-entry-badge', e.badge));
          b.append(make(doc, 'span', 'cs-entry-title', e.title));
          if (e.subtitle) b.append(make(doc, 'span', 'cs-entry-time', e.subtitle));
          if (e.meta) b.append(make(doc, 'span', 'cs-entry-meta', e.meta));
          b.setAttribute('aria-current', String(e.current));b.setAttribute('aria-label', [e.title, e.subtitle, e.meta].filter(Boolean).join(', '));
          list.append(b);
        }
        body.append(list);
        if (!item.items.length && item.empty) body.append(make(doc, 'p', 'cs-player-note', item.empty));
        if (item.more) actions().append(Object.assign(kit.button(item.more, item.moreLabel), {className: 'c-button'}));
      }
    }
  }
  if (surface.status?.text) { const s = make(doc, 'p', 'cs-player-note cs-player-status', surface.status.text);s.setAttribute('role', 'status');s.dataset.state = surface.status.state;body.append(s); }
}

// In the open card's media slot (2026-11-18, R16): a stage action is a focus stop over the picture (nothing drawn over it
// but the focus ring); the other actions are a strip under the picture that slides in when it appears; text is one short
// line in the slot (a note when nothing plays).
function renderMedia(container, surface, kit) {
  const doc = container.ownerDocument;container.classList.add('cs-media');
  let strip = null;
  for (const item of surface.sections.flatMap(s => s.items)) {
    if (item.kind === 'action' && item.stage) {
      const b = kit.button(item.id, '');b.className = 'cs-media-stage';b.setAttribute('aria-label', item.label);
      if (item.expanded !== null && item.expanded !== undefined) b.setAttribute('aria-expanded', String(item.expanded));container.append(b);continue;
    }
    if (item.kind === 'action' || (item.kind === 'text' && strip)) {
      if (!strip) { strip = make(doc, 'div', 'cs-media-strip');strip.dataset.surfaceRow = '';strip.dataset.navRow = '';container.append(strip); }
      if (item.kind === 'action') { const b = kit.button(item.id, item.label);b.className = 'c-button';b.disabled = item.disabled;strip.append(b); }
      else strip.append(make(doc, 'span', 'cs-media-credit', item.text));
      continue;
    }
    if (item.kind === 'text') container.append(make(doc, 'p', 'cs-media-note', item.text));
  }
}

export function render(container, surface, kit) {
  const doc = container.ownerDocument;
  if (container.dataset.surfacePlacement === 'media') return renderMedia(container, surface, kit);
  if (container.dataset.surfacePlacement === 'player.below' || container.dataset.surfacePlacement === 'player.timeline') return renderPlayer(container, surface, kit);
  // On Home (a module's rows): each list is one of Home's shelves, its entries Home's cards (an art plate saying the
  // entry's badge, its title and lines), its "more" the last card; text is the shelf's note. The page marks focus.
  if (container.dataset.surfacePlacement === 'home') {
    for (const section of surface.sections) {
      const shelf = make(doc, 'section', 'shelf');shelf.dataset.kind = 'module';shelf.dataset.style = 'auto';
      if (section.heading) { shelf.append(make(doc, 'h2', '', section.heading));shelf.setAttribute('aria-label', section.heading); }
      for (const item of section.items) {
        if (item.kind === 'text') { shelf.append(make(doc, 'p', 'shelf-note', item.text));continue; }
        if (item.kind !== 'list') continue;
        const row = make(doc, 'div', 'shelf-row');row.dataset.surfaceRow = '';
        const card = (key, badge, title, lines, tone, disabled) => {
          const b = kit.button(key, '');b.className = 'card';b.dataset.kind = 'module-tile';b.dataset.tone = tone;
          const art = make(doc, 'div', 'art');art.setAttribute('aria-hidden', 'true');if (badge) art.append(make(doc, 'span', 'tile-badge', badge));
          b.append(art, make(doc, 'strong', '', title));for (const l of lines.filter(Boolean)) b.append(make(doc, 'small', '', l));
          b.setAttribute('aria-label', [title, badge, ...lines].filter(Boolean).join('. '));
          if (disabled) b.setAttribute('aria-disabled', 'true');
          return b;
        };
        for (const e of item.items) row.append(card(`${item.id}:${e.id}`, e.badge, e.title, [e.subtitle, e.meta], e.tone, e.disabled));
        if (item.more) row.append(card(item.more, 'See all', item.moreLabel, [item.moreNote], 'normal', false));
        if (!item.items.length && item.empty) shelf.append(make(doc, 'p', 'shelf-note', item.empty));
        shelf.append(row);
      }
      container.append(shelf);
    }
    return;
  }
  // In the player (a module's offer over the video): Cinema's prompt, its message and a row of its pill buttons.
  if (container.dataset.surfacePlacement === 'player') {
    if (surface.lead) container.append(make(doc, 'p', 'c-prompt-message', surface.lead));
    const row = make(doc, 'div', 'c-prompt-actions');row.dataset.surfaceRow = '';
    for (const item of surface.sections.flatMap(s => s.items)) if (item.kind === 'action') { const b = kit.button(item.id, item.label);b.className = 'c-button';b.disabled = item.disabled;row.append(b); }
    container.append(row);return;
  }
  container.classList.add('cs-surface');
  if (surface.title && container.dataset.surfacePlacement !== 'page') container.append(make(doc, 'h3', 'utility-group', surface.title));   // a page: its header says it
  if (surface.lead) container.append(make(doc, 'p', 'utility-help', surface.lead));
  if (surface.status?.text) {
    const p = make(doc, 'p', 'utility-empty', surface.status.text);p.dataset.state = surface.status.state;p.setAttribute('role', 'status');
    if (surface.status.state === 'pending') p.setAttribute('aria-busy', 'true');
    container.append(p);
  }
  for (const section of surface.sections) {
    if (section.heading) container.append(make(doc, 'h3', 'utility-group', section.heading));
    // A section of three or more plain actions reads as Cinema's chip bar (one focus row: Left/Right along it), the one
    // whose tone is primary pressed, as the Browse and Search chips are.
    // A set of alternatives (every action states `selected`): Cinema's option rows, the current one ringed and checked; one
    // focus column (Up/Down), as Cinema's settings choices are drawn.
    if (section.items.length >= 2 && section.items.every(i => i.kind === 'action' && i.selected !== null)) {
      const list = make(doc, 'div', 'cs-options');list.setAttribute('role', 'group');
      for (const item of section.items) {
        const b = row(doc, kit, item.id, item.label, item.note, {disabled: item.disabled});b.classList.add('cs-option');b.setAttribute('aria-pressed', String(item.selected));
        list.append(b);
      }
      container.append(list);continue;
    }
    if (section.items.length >= 3 && section.items.every(i => i.kind === 'action' && !i.note)) {   // (an action with a note is an entry row)
      const bar = make(doc, 'div', 'chip-bar cs-chips');bar.dataset.surfaceRow = '';bar.setAttribute('role', 'group');
      for (const item of section.items) {
        const b = kit.button(item.id, item.label);b.className = 'shelf-cat';b.setAttribute('aria-pressed', String(item.tone === 'primary'));b.disabled = item.disabled;bar.append(b);
      }
      container.append(bar);continue;
    }
    for (const item of section.items) {
      if (item.kind === 'text') { const p = make(doc, 'p', item.tone === 'muted' ? 'utility-empty' : 'utility-help', item.text);p.dataset.tone = item.tone;container.append(p); }
      else if (item.kind === 'facts') {
        const dl = make(doc, 'dl', 'utility-facts');
        for (const r of item.rows) dl.append(make(doc, 'dt', '', r.label), make(doc, 'dd', '', r.value));
        const mark = item.icon ? iconOf(doc, item.icon) : null;
        if (mark) { const card = make(doc, 'div', 'cs-facts-card');card.append(mark, dl);container.append(card); } else container.append(dl);
      } else if (item.kind === 'action') {
        const b = row(doc, kit, item.id, item.label, item.note, {tone: item.tone !== 'normal' ? item.tone : '', disabled: item.disabled, icon: item.icon, opens: item.opens});
        if (item.expanded !== null && item.expanded !== undefined) b.setAttribute('aria-expanded', String(item.expanded));container.append(b);
      }
      else if (item.kind === 'choice') {
        const now = item.options.find(o => o.value === item.value)?.label || '';
        container.append(row(doc, kit, item.id, item.label, `${now} · Enter to change`, {disabled: item.disabled}));
      } else if (item.kind === 'secret') {
        const label = make(doc, 'label', 'utility-field');label.append(make(doc, 'span', '', item.label));
        const input = kit.secretInput(item);input.className = 'utility-input';label.append(input);container.append(label);
        if (item.hint) container.append(make(doc, 'p', 'utility-help', item.hint));
      } else if (item.kind === 'field') {
        const label = make(doc, 'label', 'utility-field');label.append(make(doc, 'span', '', item.label));
        const input = kit.fieldInput(item);input.className = 'utility-input';label.append(input);container.append(label);
        if (item.hint) container.append(make(doc, 'p', 'utility-help', item.hint));
      } else if (item.kind === 'file') {
        const b = kit.fileButton(item, '');b.className = 'utility-row plain';const copy = make(doc, 'span');copy.append(make(doc, 'strong', '', item.label));
        b.prepend(copy);container.append(b);
      } else if (item.kind === 'list') {
        if (item.label) container.append(make(doc, 'h3', 'utility-group', item.label));
        if (!item.items.length && item.empty) container.append(make(doc, 'p', 'utility-empty', item.empty));
        // Tiles: Cinema's poster cards (art through the page's artwork pipeline, title and caption below); rows: utility rows.
        const grid = item.layout === 'tiles' ? make(doc, 'div', 'cs-tiles') : null;
        for (const entry of item.items) {
          if (grid) {
            const b = kit.button(`${item.id}:${entry.id}`, '');b.className = 'cs-tile';b.disabled = entry.disabled;
            const art = make(doc, 'div', 'c-artwork');art.setAttribute('aria-hidden', 'true');kit.artwork(art, entry.art);
            const copy = make(doc, 'span');copy.append(make(doc, 'strong', '', entry.badge ? `${entry.badge} · ${entry.title}` : entry.title));
            const sub = [entry.subtitle, entry.meta].filter(Boolean).join(' · ');if (sub) copy.append(make(doc, 'small', '', sub));
            b.append(art, copy);grid.append(b);continue;
          }
          container.append(row(doc, kit, `${item.id}:${entry.id}`, entry.badge ? `${entry.badge} · ${entry.title}` : entry.title,
            [entry.subtitle, entry.meta].filter(Boolean).join(' · '), {disabled: entry.disabled}));
        }
        if (grid) container.append(grid);
        if (item.more) container.append(row(doc, kit, item.more, item.moreLabel, ''));
      }
    }
  }
}

// The title card's parts (2026-11-17, review of bh): Cinema's card look, exactly as the page drew it before; the page
// keeps the card's meaning, order, actions, artwork and navigation, and marks what it relies on itself.
export const cardParts = {
  section(doc, {heading, rowed}) {
    const root = make(doc, 'section', 'detail-section'), h = make(doc, 'h3', '', heading);
    const row = make(doc, rowed ? 'div' : 'dl', rowed ? 'detail-shelf' : 'detail-info');
    root.append(h, row);return {root, row, heading: h};
  },
  tile(doc, {title, note}) {
    const button = make(doc, 'button', 'detail-card'), art = make(doc, 'div', 'art');
    button.append(art, make(doc, 'strong', '', title), make(doc, 'small', '', note || ''));
    return {button, art};
  },
  fact(doc, row, label, value) { row.append(make(doc, 'dt', '', label), make(doc, 'dd', '', value)); },
};
