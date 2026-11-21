// Advanced Media's page part: "Keep title". The user may keep the title information the current
// playback states for itself in this module's library. Nothing is kept by itself: no viewing
// history, no background capture, no account writes.
//
// Uses only its `cinema` API:
//   playback.current (the request and the identity its listing stated)
//   player.state/onState/expand/collapse, slots, focus groups, shared components
//   storage (this module's own; its backend publishes kept titles in its manifests)
//
// One storage key per kept title ("kept.<record key>"): storage replaces one key at a time and
// keeps the others, so nothing is read and rewritten, and no other entry can be lost. The record
// key is this module's bookkeeping, not a claim about any media source. A request id,
// generation, token, observation or playable reference is never stored.

// Trailers (mountTrailers, at the end): a title's trailer inside its thumbnail after a rest on it, continuing in its card.
let trailers = null;
export const SCHEMA = 'cinema.kept-title/1';
export const ORDER = ['tmdb', 'imdb', 'tvdb', 'musicbrainz', 'isbn', 'asin'];
const KEY = /^[a-z0-9][a-z0-9_.-]{0,31}$/, MEDIA = /^[a-z][a-z0-9_.-]{0,31}$/, URLISH = /[a-z][a-z0-9+.-]*:[/][/]|magnet:/i;

// Safe display and storage text: control, zero-width, separator and bidi characters removed,
// whitespace collapsed, bounded; null when nothing is left or it looks like a link.
export function plain(value, limit = 200) {
  if (typeof value !== 'string') return null;
  let out = '';
  for (const ch of value) {
    const c = ch.codePointAt(0);
    const hidden = c < 0x20 || (c >= 0x7f && c <= 0x9f) || (c >= 0x200b && c <= 0x200f) || (c >= 0x2028 && c <= 0x202e) || (c >= 0x2066 && c <= 0x2069) || c === 0xfeff;
    out += hidden ? ' ' : ch;
  }
  out = out.replace(/\s+/g, ' ').trim();
  if (!out || URLISH.test(out)) return null;
  return out.slice(0, limit);
}

// The record a stated identity allows keeping, or null with the reason. Pure.
export function recordFor(identity) {
  if (!identity || typeof identity !== 'object') return {record: null, reason: 'no stated identity'};
  const title = plain(identity.title);
  if (!title) return {record: null, reason: 'no usable title'};
  const media = typeof identity.media_type === 'string' && MEDIA.test(identity.media_type) ? identity.media_type : 'unknown';
  const ids = {};
  for (const [scheme, value] of Object.entries(identity.ids && typeof identity.ids === 'object' ? identity.ids : {})) {
    const s = String(scheme).toLowerCase(), v = plain(typeof value === 'number' && Number.isInteger(value) ? String(value) : value, 64);
    if (KEY.test(s) && v && !['none', 'null', '0'].includes(v.toLowerCase()) && Object.keys(ids).length < 16) ids[s] = v;
  }
  const attributes = {};
  const source = {...(identity.attributes && typeof identity.attributes === 'object' ? identity.attributes : {})};
  for (const name of ['showtitle', 'season', 'episode']) if (identity[name] !== undefined) source[name] = identity[name];
  for (const [name, value] of Object.entries(source)) {
    const kept = typeof value === 'boolean' || (Number.isInteger(value) && Math.abs(value) < 1e12) ? value : plain(value);
    if (KEY.test(name) && kept !== null && kept !== undefined && Object.keys(attributes).length < 16) attributes[name] = kept;
  }
  const year = Number.isInteger(identity.year) && identity.year > 0 && identity.year < 10000 ? identity.year : null;
  const record = {schema: SCHEMA, media_type: media, title, ids, attributes, kept_from: 'playback-stated'};
  if (year !== null) record.year = year;
  // The attributed parent fact, if the playback states one: kept beside the raw ids, never merged into them
  // (the backend validates it again; anything unusable is kept as malformed, never repaired).
  if (identity.parent !== undefined && identity.parent !== null) record.parent = parentFor(identity.parent);
  return {record, reason: 'ok'};
}

// The descriptive snapshot a playback states BESIDE its identity (the core's cinema_curated_manifests.description:
// {state: "stated", origin: "listing", producer: {id, name, version}, facts: {overview, genre, runtime}, truncated,
// dropped}), bounded again, or null. Kept apart from the record's ids, attributes and parent: recordKey never reads
// it, so a description never changes which title a record is. Pure.
export const DESCRIPTION = {overview: 1000, genres: 8, genre: 40, runtime: 86400};
const TAG = /<[^<>]{0,200}>/g;
const FACT_NAMES = ['overview', 'genre', 'runtime'];
export function shorten(text, limit) {
  if (text.length <= limit) return text;
  const cut = text.slice(0, limit - 1), space = cut.lastIndexOf(' ');
  return (space >= 0 && space > limit - 60 ? cut.slice(0, space) : cut).trimEnd() + '…';  // no space: cut as is
}
export function descriptionFor(raw) {
  if (!raw || typeof raw !== 'object' || raw.state !== 'stated' || raw.origin !== 'listing') return null;
  const p = raw.producer && typeof raw.producer === 'object' ? raw.producer : {};
  if (typeof p.id !== 'string' || !/^[A-Za-z0-9_.-]{1,128}$/.test(p.id)) return null;
  const facts = raw.facts && typeof raw.facts === 'object' ? raw.facts : {};
  const truncated = new Set(Array.isArray(raw.truncated) ? raw.truncated.filter(n => FACT_NAMES.includes(n)) : []);
  const dropped = new Set(Array.isArray(raw.dropped) ? raw.dropped.filter(n => FACT_NAMES.includes(n)) : []);
  const out = {};
  // truncated: a kept fact not complete as stated (text shortened, list items cut, capped or unusable). A marker the
  // input already has is kept while its fact survives (idempotent: never upgraded to complete). As the core's rule.
  const clean = (value, limit) => {
    if (typeof value !== 'string') return [null, false];
    const text = plain(value.slice(0, limit * 8).replace(TAG, ' '), Infinity);
    if (!text) return [null, false];
    const short = shorten(text, limit);
    return [short, short !== text || value.length > limit * 8];
  };
  if (facts.overview !== undefined) {
    const [text, cut] = clean(facts.overview, DESCRIPTION.overview);
    if (text) { out.overview = text; if (cut) truncated.add('overview'); } else dropped.add('overview');
  }
  if (facts.genre !== undefined) {
    const values = Array.isArray(facts.genre) ? facts.genre : [facts.genre];
    let partial = values.length > DESCRIPTION.genres * 4;
    const list = [];
    for (const item of values.slice(0, DESCRIPTION.genres * 4)) {
      const [text, cut] = clean(item, DESCRIPTION.genre);
      if (!text) { partial = true; continue; }
      partial ||= cut;
      if (!list.includes(text)) list.push(text);
    }
    if (list.length) { out.genre = list.slice(0, DESCRIPTION.genres); if (partial || list.length > DESCRIPTION.genres) truncated.add('genre'); } else dropped.add('genre');
  }
  if (facts.runtime !== undefined) {
    if (Number.isInteger(facts.runtime) && facts.runtime > 0 && facts.runtime <= DESCRIPTION.runtime) out.runtime = facts.runtime; else dropped.add('runtime');
  }
  if (!Object.keys(out).length) return null;
  for (const name of Object.keys(out)) dropped.delete(name);
  return {state: 'stated', origin: 'listing', producer: {id: p.id, name: plain(p.name, 80) || p.id, version: plain(p.version, 32)}, facts: out,
          truncated: [...truncated].filter(n => n in out).sort(), dropped: [...dropped].sort()};
}
// The page's storage write is one request body of at most 4096 bytes (the server's limit), and the module's whole
// store is 64 KiB: a kept record's description is fitted to both, never the reason a Keep fails. Its overview is
// shortened (marked truncated) and then, only if that is not enough, its facts are left out (marked omitted).
export const WRITE_LIMIT = 4000, DESCRIPTION_STORE = 1536;
const bodyBytes = (key, value) => new TextEncoder().encode(JSON.stringify({key, value})).length;
const storeBytes = (value) => { const s = JSON.stringify(value ?? null); let n = s.length; for (const ch of s) if (ch.codePointAt(0) > 0x7e) n += ch.codePointAt(0) > 0xffff ? 10 : 5; return n; };
export function fitDescription(storageKey, value) {
  if (!value?.description) return value;
  let out = {...value, description: {...value.description, facts: {...value.description.facts}, truncated: [...value.description.truncated]}};
  const fits = () => bodyBytes(storageKey, out) <= WRITE_LIMIT && storeBytes(out.description) <= DESCRIPTION_STORE;
  while (!fits() && out.description.facts.overview && out.description.facts.overview.length > 80) {
    out.description.facts.overview = shorten(out.description.facts.overview, Math.floor(out.description.facts.overview.length * 0.75));
    if (!out.description.truncated.includes('overview')) out.description.truncated = [...out.description.truncated, 'overview'].sort();
  }
  for (const name of ['overview', 'genre', 'runtime']) {
    if (fits()) break;
    if (out.description.facts[name] === undefined) continue;
    delete out.description.facts[name];
    out.description.truncated = out.description.truncated.filter(n => n !== name);
    out.description.omitted = [...(out.description.omitted || []), name];
  }
  if (!Object.keys(out.description.facts).length || !fits()) { const {description, ...rest} = out; return rest; }
  return out;
}

// The core's generic parent fact shape (cinema_curated_manifests.asserted_parent), bounded. Pure.
export function parentFor(raw) {
  const bad = {state: 'malformed'};
  if (!raw || typeof raw !== 'object' || (raw.state ?? 'asserted') !== 'asserted') return bad;
  const ok = (v, re) => typeof v === 'string' && re.test(v);
  if (!ok(raw.producer, /^[A-Za-z0-9_.-]{1,128}$/) || !ok(raw.mapping, /^[A-Za-z0-9 ._:<>=+-]{1,96}$/) || !ok(raw.numbering, /^[a-z0-9][a-z0-9-]{0,31}$/)
      || raw.level !== 'show' || !raw.ids || typeof raw.ids !== 'object') return bad;
  const entries = Object.entries(raw.ids);
  if (entries.length < 1 || entries.length > 8) return bad;
  const ids = {};
  for (const [scheme, value] of entries) {
    if (!/^[a-z][a-z0-9_]{0,15}$/.test(scheme) || !ok(value, /^[A-Za-z0-9._:-]{1,64}$/) || ['none', 'null', '0'].includes(value.toLowerCase())) return bad;
    ids[scheme] = value;
  }
  return {state: 'asserted', producer: raw.producer, mapping: raw.mapping, level: 'show', numbering: raw.numbering, ids};
}

// FNV-1a, 64 bit: a short, stable key from stated ids (bookkeeping only).
function fnv(text) {
  let h = 0xcbf29ce484222325n;
  for (const byte of new TextEncoder().encode(text)) { h ^= BigInt(byte); h = (h * 0x100000001b3n) & 0xffffffffffffffffn; }
  return h.toString(16).padStart(16, '0');
}
// The record key: from the first stated id in the backend's order (the same title kept again
// replaces its own record), or a random module key when no id is stated (a name never deduplicates).
// An episode's id may be its series', so an episode is keyed by id only together with a usable stated
// position (episodePosition, the backend's rule); without one it takes the random key, as a title
// without ids: two unidentified episodes of one series never replace each other.
export function episodePosition(season, episode) {
  const whole = (v, low, high) => typeof v === 'number' && Number.isInteger(v) && v >= low && v <= high;
  return whole(season, 0, 9999) && whole(episode, 1, 99999) ? [season, episode] : null;
}
export function recordKey(record, random = () => globalThis.crypto.getRandomValues(new Uint8Array(8))) {
  const schemes = Object.keys(record.ids || {});
  let scheme = ORDER.find(s => schemes.includes(s)) || schemes.sort()[0];
  let position = [];
  if (record.media_type === 'episode') {
    position = episodePosition(record.attributes?.season, record.attributes?.episode);
    if (!position) scheme = undefined;  // no usable position: not identified by a series id alone
    const parent = record.parent;
    if (scheme && position && parent !== undefined && parent !== null) {
      // The backend's rule (group_key): the raw ids and position stay the key, and an asserted numbering scope is
      // added as a separator (another numbering sharing a bare series id never replaces this record); an unusable
      // assertion keys nothing. No assertion: the key is exactly as before (existing records keep their keys).
      if (parent.state !== 'asserted') scheme = undefined;
      else {
        const scope = ['numbering', parent.numbering, ...Object.entries(parent.ids || {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).flat()];
        return 'i' + fnv(JSON.stringify([record.media_type, scheme, record.ids[scheme], ...position, ...scope]));
      }
    }
  }
  if (scheme) return 'i' + fnv(JSON.stringify([record.media_type, scheme, record.ids[scheme], ...position]));
  return 'r' + [...random()].map(b => b.toString(16).padStart(2, '0')).join('');
}
export function rowsFor(record) {
  const rows = [['Title', record.title], ['Type', record.media_type === 'unknown' ? 'Not stated' : record.media_type]];
  if (record.year !== undefined) rows.push(['Year', String(record.year)]);
  const ids = Object.entries(record.ids);
  rows.push(['Ids', ids.length ? ids.map(([s, v]) => `${s} ${v}`).join(', ') : 'None stated']);
  for (const [name, value] of Object.entries(record.attributes)) rows.push([name, String(value)]);
  return rows;
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// The user's display label of a stored kept record, or null (only a well-formed user label counts).
export function labelOf(record) {
  const d = record && typeof record === 'object' ? record.display : null;
  return d && typeof d === 'object' && d.supplied === 'user' ? plain(d.title) : null;
}
// One line of safe facts for a kept record: its stated type (unknown says so), year and ids.
export function factsLine(row) {
  const parts = [row.media_type === 'unknown' || !MEDIA.test(row.media_type || '') ? 'Type not stated' : row.media_type];
  if (Number.isInteger(row.year)) parts.push(String(row.year));
  const ids = Object.entries(row.ids && typeof row.ids === 'object' ? row.ids : {}).map(([s, v]) => [plain(s, 32), plain(String(v), 64)]).filter(([s, v]) => s && v);
  parts.push(ids.length ? ids.map(([s, v]) => `${s} ${v}`).join(', ') : 'no ids stated');
  return parts.join(' · ');
}
const INPUT_NAMES = {library: 'your library records', reference: 'your saved items', kept: 'another kept title'};
// Which other inputs still state this title, in plain words, or null.
export function othersText(others) {
  const names = Object.entries(others && typeof others === 'object' ? others : {}).filter(([k, n]) => INPUT_NAMES[k] && Number.isInteger(n) && n > 0).map(([k]) => INPUT_NAMES[k]);
  return names.length ? names.join(', ') : null;
}

export function mount(cinema) {
  // Every storage write of this page part (Keep, Rename, Remove) runs on this one chain: each re-reads what
  // it changes first, so within this page nothing is written over a newer value. Another page or window
  // writing the same storage is not covered.
  let writes = Promise.resolve();
  const serial = (fn) => { const step = writes.then(fn); writes = step.catch(() => {}); return step; };
  let library = null;
  // Keep title in the player is a SEMANTIC surface pair (2026-11-17, review of bh): an entry point and a panel the player
  // expands; the active skin draws both. This module draws nothing of its own here.
  if (typeof cinema.surfaces?.player !== 'function') return;
  const view = {rows: [], status: '', offer: false};
  const strip = cinema.surfaces.player('player.below', {sections: [{items: [{kind: 'action', id: 'opener', label: 'Keep title'}]}]},
    {group: true, hidden: true, onAction: ({id}) => { if (id === 'opener') open(); }});
  const panel = cinema.surfaces.player('player.below', {title: 'Keep this title', sections: []}, {hidden: true,
    onAction: ({id}) => { if (id === 'keep') confirm();else if (id === 'cancel') close(); }});
  const draw = () => panel.update({title: 'Keep this title', status: view.status ? {state: busy ? 'pending' : 'ok', text: view.status} : null, sections: [
    ...(view.rows.length ? [{items: [{kind: 'facts', rows: view.rows.map(([label, value]) => ({label, value}))}]}] : []),
    {items: [...(view.offer ? [{kind: 'action', id: 'keep', label: 'Keep'}] : []), {kind: 'action', id: 'cancel', label: view.offer ? 'Cancel' : 'Close'}]}]});

  let epoch = 0, isOpen = false, pending = null, busy = false;
  const keptFor = new Map();  // request -> record key, this session only: not offered twice for one playback
  const say = (text) => { view.status = text;draw(); };
  const showRows = (list) => { view.rows = list;draw(); };
  const offer = (on) => {
    view.offer = on;draw();
    if (on && panel.element.contains(document.activeElement)) panel.focus('keep');
  };
  function close() { epoch += 1; pending = null; busy = false; if (isOpen) { isOpen = false; cinema.player.collapse(); } panel.hidden = true; }

  // The request the player shows, and what it states it is: only when both agree right now.
  async function read() {
    const current = await cinema.playback.current();
    const state = cinema.player.state();
    if (!current || !state || current.request !== state.id || current.state === 'ended') return {request: state?.id ?? null, record: null, reason: 'not the playing request'};
    const found = recordFor(current.identity);
    // The listing's descriptive snapshot bound to this same request (the core clears it for any other), kept beside
    // the record: never part of its key, ids or attributes.
    const description = found.record ? descriptionFor(current.description) : null;
    return {request: current.request, ...found, record: description ? {...found.record, description} : found.record};
  }

  async function open() {
    if (!cinema.player.state()) return;
    const mine = ++epoch;
    pending = null;showRows([]);offer(false);say('Checking what is playing...');
    panel.hidden = false;
    isOpen = panel.expand({onCollapse: () => { isOpen = false; epoch += 1; pending = null; panel.hidden = true; }});
    if (!isOpen) { panel.hidden = true; return; }
    let seen;
    try { seen = await read(); } catch (error) { if (epoch === mine && !String(error?.message || error).includes('unloaded')) say('Advanced Media could not check what is playing.'); return; }
    if (epoch !== mine) return;  // closed, or the video changed, meanwhile
    if (!seen.record) { say('This video doesn\'t say what it is, so there is nothing to keep.'); return; }
    showRows(rowsFor(seen.record));
    if (keptFor.has(seen.request)) { say('You kept this title already.'); return; }
    pending = seen;say('');offer(true);
  }

  async function confirm() {
    if (!pending || busy) return;
    const mine = epoch, chosen = pending;
    busy = true;offer(false);say('Keeping...');
    try {
      // Check again: never keep a different title because playback changed meanwhile.
      const now = await read();
      if (epoch !== mine) return;
      if (now.request !== chosen.request || !same(now.record, chosen.record)) { pending = null; say('This video changed, so nothing was kept.'); return; }
      const key = recordKey(chosen.record);
      // On the module's one write chain; keeping a title again keeps the user's own label for it.
      let fitted;
      await serial(async () => {
        const before = (await cinema.storage.get())[`kept.${key}`];
        const display = labelOf(before);
        fitted = fitDescription(`kept.${key}`, display ? {...chosen.record, key, display: {title: display, supplied: 'user'}} : {...chosen.record, key});
        await cinema.storage.set(`kept.${key}`, fitted);
      });
      library?.changed();
      if (epoch !== mine) return;  // written, but the panel has moved on: nothing is shown
      keptFor.set(chosen.request, key);pending = null;
      // The write succeeded; publication is the backend's, not claimed here.
      say(chosen.record.description && !fitted?.description ? 'Kept in Advanced Media. Its listing description was too long to keep.' : 'Kept in Advanced Media.');
    } catch (error) {
      if (epoch !== mine || String(error?.message || error).includes('unloaded')) return;
      pending = null;
      say('Advanced Media could not keep this title.');
    } finally {
      if (epoch === mine) busy = false;
      const a = document.activeElement;   // inside the panel, or lost with the Keep button it was on
      if (epoch === mine && !view.offer && (panel.element.contains(a) || !a?.isConnected || a === document.body)) panel.focus('cancel');
    }
  }

  let shownFor = null;
  cinema.player.onState((state) => {
    strip.hidden = !state;
    // Another request (or none): what the panel showed was for the previous one.
    if ((state?.id ?? null) !== shownFor) { if (shownFor !== null) close(); shownFor = state?.id ?? null; }
  });
  const initial = cinema.player.state();strip.hidden = !initial;shownFor = initial?.id ?? null;
  if (cinema.page && cinema.backend) library = mountLibrary(cinema, serial);
  if (cinema.media && cinema.backend) trailers = mountTrailers(cinema);
  if (cinema.cards) cinema.cards.template(presentTitle);
}

// Library: the titles the user kept, from this module's own backend read "library" (storage as it is now).
// Rename changes only the display label; Remove deletes only that kept record. Both commit only if the
// record still is exactly what the editor or confirmation was opened on.
export const FIND_POLL_FAILURES = 5;   // failed status reads in a row before Find sources gives up
export const FIND_LIMIT_MS = 120000;   // how long Find sources waits for its search at most
function mountLibrary(cinema, serial) {
  // The Library is a SEMANTIC surface view (2026-11-17, review of bh): each kept title with its facts and actions, rename
  // (a field), remove, Find sources and linking as steps in place, and the supplied chapters with Import (a file picker);
  // the active skin draws it. This module draws nothing of its own. (An older Cinema without surfaces: no Library view.)
  if (typeof cinema.surfaces?.view !== 'function') return null;
  let rows = [], epoch = 0, mode = null, loading = false, loaded = 0, returnTo = null, statusText = '', about = false, view = null;
  let finding = null;  // {key, request, answer, mine}: one Find sources for one kept title, never stored
  const say = (text) => { statusText = text;draw(); };
  const gone = (error) => String(error?.message || error).includes('unloaded');
  // Ends the current Find sources. A cancel of a call already inside the search service says so: the service
  // stops it when it can (a Kodi addon call cannot be interrupted).
  const stopFinding = (tell = false) => {
    const f = finding; finding = null;
    if (!f?.request) return;
    const asked = epoch;  // the late words belong to this moment only: not after another search or the view closing
    cinema.backend.read('find.cancel', {request: f.request}).then(answer => {
      if (tell && !finding && asked === epoch && answer?.ok && answer.value?.settling) say('Search cancelled. The search service may take a moment to finish what it had already started.');
    }).catch(() => {});
  };
  const close = () => { epoch += 1; mode = null; stopFinding(); };  // anything still out belongs to an opening that is over
  const sets = chapterSets(cinema, serial, () => draw(), key => view?.focus(key));
  // A new opening starts at the welcome; About is one press away again (nothing in it is lost). Opening reads again.
  view = cinema.surfaces.view({label: 'Library'}, {title: 'Library', sections: []}, {onAction: p => act(p),
    onOpen: () => { refresh();sets.refresh(); }, onClose: () => { close();about = false;draw(); }});
  const text = (t, tone = 'muted') => ({kind: 'text', text: t, tone}), action = (id, label, extra = {}) => ({kind: 'action', id, label, ...extra});
  function rowSection(row) {
    const shown = row.display || row.title, items = [];
    const facts = factsLine(row);if (facts) items.push(text(facts));
    const notes = [];
    if (row.display) notes.push(`Your label. Stated title: ${row.title}`);
    const also = othersText(row.others);if (also) notes.push(`Also stated by ${also}.`);
    if (notes.length) items.push(text(notes.join(' ')));
    if (row.link) {
      const c = row.link.candidate || {};
      items.push(text(row.link.state === 'valid'
        ? `Your link: ${c.title}${candidateFacts(c) ? ` · ${candidateFacts(c)}` : ''} · from ${fromText(row.link.provenance)} · edition, release and timeline unknown.`
        : 'A saved link no longer matches this title (it changed since you linked it).'));
    }
    if (mode?.key === row.key && mode.kind === 'rename') {
      items.push({kind: 'field', id: `name.${row.key}`, label: `New name for ${shown}`, value: shown, max: 200, submit: `save.${row.key}`},
        action(`save.${row.key}`, 'Save', {tone: 'primary'}), action(`cancel.${row.key}`, 'Cancel'));
    } else if (mode?.key === row.key && mode.kind === 'remove') {
      items.push(text(`Remove “${shown}” from your kept titles?`, 'warning'), action(`confirm.${row.key}`, 'Remove', {tone: 'danger'}), action(`cancel.${row.key}`, 'Keep it'));
    } else {
      items.push(action(`rename.${row.key}`, 'Rename'), action(`remove.${row.key}`, 'Remove'), action(`find.${row.key}`, 'Find sources'));
      if (row.link) items.push(action(`unlink.${row.key}`, 'Remove link'));
    }
    if (finding?.key === row.key) items.push(...findingItems(finding));
    return {heading: shown, items};
  }
  // Find sources: the kept title's stated title is the query, said plainly; one available search service is asked (the
  // backend chooses one that advertises search). Hits are discovery candidates: shown with where they came from, edition
  // and timeline unknown; nothing is kept, saved or added to Advanced Media. Choosing one asks to link it (confirmed).
  function findingItems(f) {
    const a = f.answer || {}, items = [];
    items.push(text(a.provider ? `Searching ${a.provider.label} for “${a.query}”.` : `Searching for “${a.query || ''}”.`, 'normal'));
    if (a.state === 'found') {
      items.push({kind: 'list', id: `cands.${f.key}`, label: 'Possible matches', items: (a.candidates || []).map((c, i) => ({id: 'c' + i, title: c.title,
        subtitle: `${candidateFacts(c)}${candidateFacts(c) ? ' · ' : ''}from ${fromText(c.provenance)} · edition and timeline unknown`,
        meta: typeof c.cid === 'string' && !f.confirm ? 'Link…' : null, disabled: typeof c.cid !== 'string' || !!f.confirm}))});
      if (f.confirm) {
        const c = f.confirm.candidate;
        items.push(text(`Link “${f.confirm.shown}” to this result?`, 'normal'), text(`${c.title}${candidateFacts(c) ? ` · ${candidateFacts(c)}` : ''} · from ${fromText(c.provenance)}.`),
          text('This is your link to a possible match. Your kept title and its ids stay as they are; edition, release and timeline stay unknown.'),
          action(`link-confirm.${f.key}`, 'Link', {tone: 'primary'}), action(`link-cancel.${f.key}`, 'Cancel'));
      }
    }
    let said = {searching: 'Searching…', found: `${(a.candidates || []).length} found${a.more ? ' (more were left out)' : ''}. These are possible matches, not added to Advanced Media.`,
                empty: a.message, unavailable: a.message, unsupported: a.message, failed: a.message || 'The search could not be completed.',
                cancelled: 'Search cancelled.'}[a.state || 'searching'] || '';
    const notes = (a.notes || []).filter(n => n.code !== 'no_results' || a.state !== 'empty').map(n => [n.source_label, n.message].filter(Boolean).join(': ')).filter(Boolean);
    if (notes.length) said += ` ${notes.join(' ')}`;
    if (said) items.push(text(said));
    items.push(action(`find-close.${f.key}`, a.state === 'searching' || !a.state ? 'Cancel search' : 'Close'));
    return items;
  }
  function draw() {
    if (!view) return;
    const way = cinema.page?.browseLabel?.() || '';
    const sections = [{items: [text(rows.length ? 'Titles you kept while watching.' : 'Titles you keep while watching appear here.', 'normal')]}, ...rows.map(rowSection)];
    // UX-05: a welcoming lead and, when empty, one way on (the page's own Search or Home).
    if (!rows.length) sections.push({items: [text('Nothing kept yet. While a video plays, choose “Keep title” to keep it here.'), ...(way ? [action('browse', way)] : [])]});
    // The label provenance and the supplied chapters (with Import) are one step away behind "About labels and chapters".
    sections.push({items: [action('about', 'About labels and chapters', {expanded: about})]});
    if (about) sections.push({items: [text('A new name here is your label, not information from a provider.')]}, sets.section());
    view.update({title: 'Library', status: statusText ? {state: 'ok', text: statusText} : null, sections});
  }
  function act({id, item, text: typed, file}) {
    if (id === 'browse') return cinema.page?.browse?.();
    if (id === 'about') { about = !about;draw();return; }
    if (sets.act({id, file})) return;
    const dot = id.indexOf('.'), verb = dot > 0 ? id.slice(0, dot) : id, key = dot > 0 ? id.slice(dot + 1) : null;
    const row = rows.find(r => r.key === key);
    if (verb === 'rename' && row) return begin(row, 'rename');
    if (verb === 'remove' && row) return begin(row, 'remove');
    if (verb === 'find' && row) return find(row);
    if (verb === 'unlink' && row) return unlink(row);
    if (verb === 'save') return commit('rename', typed ?? '');
    if (verb === 'confirm') return commit('remove');
    if (verb === 'cancel') return cancel();
    if (verb === 'cands' && item && finding?.answer?.state === 'found') { const c = finding.answer.candidates?.[Number(item.slice(1))];if (c) return beginLink(c); }
    if (verb === 'link-confirm') return commitLink();
    if (verb === 'link-cancel' && finding) { finding.confirm = null;say('Nothing was linked.');return; }
    if (verb === 'find-close' && finding) { const searching = finding.answer?.state === 'searching';stopFinding(searching);say(searching ? 'Search cancelled.' : '');return; }
  }
  // After a step ends: back to the same title's Rename when it is still there.
  const backTo = () => { if (returnTo) view?.focus(`rename.${returnTo}`); };
  const render = () => draw();
  let again = false;
  async function refresh(force = false) {
    if (loading) { again = again || force; return; }
    if (!force && Date.now() - loaded < 1500) return;
    loading = true;const mine = epoch;
    try {
      const answer = await cinema.backend.read('library');
      if (mine !== epoch || mode) return;  // closed, or an editor is open: never redraw under it
      rows = answer?.ok && Array.isArray(answer.value?.kept) ? answer.value.kept : [];
      loaded = Date.now();
      if (!answer?.ok) say('Your kept titles could not be read right now.');
      render();
    } catch (error) { if (!gone(error) && mine === epoch) say('Your kept titles could not be read right now.'); }
    finally { loading = false; if (again) { again = false; refresh(true); } }
  }
  // Rename or Remove opens on a snapshot of the stored record: the commit compares against it.
  async function begin(row, kind) {
    if (mode) return;
    const mine = ++epoch;
    let stored;
    try { stored = (await cinema.storage.get())[`kept.${row.key}`]; } catch (error) { if (!gone(error)) say('This title could not be read.'); return; }
    if (mine !== epoch) return;
    if (!stored) { say('This title is no longer kept.'); loaded = 0; refresh(true); return; }
    mode = {key: row.key, kind, snapshot: JSON.stringify(stored), row, mine};say('');
    view?.focus(kind === 'rename' ? `name.${row.key}` : `confirm.${row.key}`);
  }
  function cancel() { returnTo = mode?.key ?? null;mode = null;epoch += 1;say('Nothing was changed.');backTo(); }
  // Linking: the user confirms one candidate of the current result as this kept title's one link. Shown in full
  // before anything is written; the backend checks the request, the result and the title again (find.confirm),
  // and the write goes through the same serialized, snapshot-checked path as Rename and Remove.
  const candidateFacts = (c) => [c.year, c.media_type !== 'unknown' ? c.media_type : null, c.season != null && c.episode != null ? `S${c.season} E${c.episode}` : null,
                                 Object.entries(c.ids || {}).map(([s, v]) => `${s} ${v}`).join(', ') || null].filter(Boolean).join(' · ');
  const fromText = (p) => [p?.source_label, p?.provider_label].filter(Boolean).join(' via ') || 'the search service';
  async function beginLink(c) {
    const f = finding;if (!f || f.answer?.state !== 'found' || mode) return;
    let stored;
    try { stored = (await cinema.storage.get())[`kept.${f.key}`]; } catch (error) { if (!gone(error)) say('This title could not be read.'); return; }
    if (finding !== f) return;
    if (!stored) { say('This title is no longer kept.'); loaded = 0; refresh(true); return; }
    f.confirm = {candidate: c, snapshot: JSON.stringify(stored), shown: stored.display?.title || stored.title};say('');
    view?.focus(`link-confirm.${f.key}`);
  }
  async function commitLink() {
    const f = finding, pending = f?.confirm;if (!pending || pending.sent) return;
    pending.sent = true;  // one-shot: a second press of Link for this confirmation sends nothing
    say('Linking…');
    let answer;
    try { answer = await cinema.backend.read('find.confirm', {request: f.request, candidate: pending.candidate.cid, key: f.key}); } catch (error) { if (gone(error)) return; answer = null; }
    if (finding !== f || f.confirm !== pending) return;  // cancelled, another search, or the view closed meanwhile
    if (!answer?.ok) { f.confirm = null; say('The link could not be checked, so nothing was linked.'); render(); return; }
    if (answer.value.state !== 'ok') { f.confirm = null; say(answer.value.message || 'Nothing was linked.'); render(); return; }
    let outcome;
    try {
      outcome = await serial(async () => {
        const values = await cinema.storage.get();
        if (finding !== f || f.confirm !== pending) return 'closed';
        if (!values[`kept.${f.key}`]) return 'removed';
        if (JSON.stringify(values[`kept.${f.key}`]) !== pending.snapshot) return 'changed';
        await cinema.storage.set(`link.${f.key}`, answer.value.link);  // the kept record itself is not touched
        return 'ok';
      });
    } catch (error) { if (gone(error)) return; outcome = 'failed'; }
    if (outcome === 'closed') return;
    // A write already under way may finish after the user moved on (another Find sources, the view closed or
    // reopened). The link it wrote stands, and the fresh read below shows it; but only this confirmation's
    // own search and words are touched: a newer search, its cancellation and the view's current feedback are not.
    if (finding === f && f.confirm === pending) {
      const title = pending.candidate.title;
      if (outcome === 'ok') { stopFinding(); returnTo = f.key; }
      else f.confirm = null;
      say({ok: `Linked to “${title}” from ${fromText(pending.candidate.provenance)}.`, removed: 'This title was removed, so nothing was linked.',
           changed: 'This title changed since you searched, so nothing was linked. Find sources again.', failed: 'Advanced Media could not save this link.'}[outcome]);
    }
    loaded = 0;refresh(true);
  }
  async function unlink(row) {
    if (mode) return;
    const mine = ++epoch;say('Removing the link…');
    let outcome;
    try {
      outcome = await serial(async () => {
        const values = await cinema.storage.get();
        if (mine !== epoch) return 'closed';
        if (!values[`link.${row.key}`]) return 'gone';
        await cinema.storage.set(`link.${row.key}`, null);
        return 'ok';
      });
    } catch (error) { if (gone(error)) return; outcome = 'failed'; }
    if (outcome === 'closed' || mine !== epoch) return;
    returnTo = row.key;
    say({ok: 'Link removed. Your kept title is unchanged.', gone: 'This link was already removed.', failed: 'Advanced Media could not remove this link.'}[outcome]);
    loaded = 0;refresh(true);
  }
  // Starts go to the backend one at a time, in the order the user asked: the backend keeps the latest
  // reservation as the one search, so this order is also its order (an earlier start is withdrawn there).
  let starting = Promise.resolve();
  async function find(row) {
    if (mode) return;
    stopFinding();
    const mine = ++epoch, shown = row.display || row.title;
    const fail = (message) => { if (finding?.mine === mine) { const f = finding; stopFinding(); finding = {...f, request: null, answer: {...f.answer, state: 'failed', message}}; say(message); render(); } };
    finding = {key: row.key, request: null, answer: {state: 'searching', query: row.title}, mine};say(`Finding sources for “${shown}”…`);render();
    let answer;
    const start = starting.then(() => finding?.mine === mine ? cinema.backend.read('find', {key: row.key, query: row.title}) : null);
    starting = start.catch(() => {});
    try { answer = await start; } catch (error) { if (gone(error)) return; answer = undefined; }
    if (answer === null) return;  // replaced before its turn: never sent
    if (finding?.mine !== mine) { if (answer?.ok && answer.value?.request) cinema.backend.read('find.cancel', {request: answer.value.request}).catch(() => {}); return; }
    if (!answer?.ok) { fail('The search could not be started.'); return; }
    finding.request = answer.value.request || null;finding.answer = answer.value;render();
    // Bounded: a few failed status reads in a row, or a search that does not finish, end it with words.
    const deadline = Date.now() + FIND_LIMIT_MS;let failures = 0;
    while (finding?.mine === mine && finding.answer.state === 'searching') {
      await new Promise(resolve => setTimeout(resolve, 400));
      if (finding?.mine !== mine) return;
      if (Date.now() > deadline) { fail('The search took too long, so it was cancelled.'); return; }
      let now;
      try { now = await cinema.backend.read('find.poll', {request: finding.request}); } catch (error) { if (gone(error)) return; now = null; }
      if (finding?.mine !== mine) return;  // cancelled, another title, or the view closed: a late answer is dropped
      if (!now?.ok) { if (++failures >= FIND_POLL_FAILURES) { fail('The search status could not be read, so the search was cancelled.'); return; } continue; }
      failures = 0;
      if (now.value.state === 'stale') { finding.answer = {...finding.answer, state: 'cancelled'}; break; }
      finding.answer = now.value;
    }
    if (finding?.mine === mine) { say({found: 'Possible matches are listed under the title.', empty: 'No possible matches were found.'}[finding.answer.state] || ''); render(); }
  }
  async function commit(kind, typed = '') {
    const m = mode;if (!m || m.kind !== kind) return;
    const label = kind === 'rename' ? typed : null;
    const clean = kind === 'rename' ? plain(label) : null;
    if (kind === 'rename' && label.trim() && !clean) { say('Use a name without links or control characters.'); return; }
    returnTo = m.key;mode = null;const mine = ++epoch;say(kind === 'rename' ? 'Saving…' : 'Removing…');render();
    let outcome;
    try {
      outcome = await serial(async () => {
        const values = await cinema.storage.get();
        const now = values[`kept.${m.key}`];
        if (mine !== epoch) return 'closed';  // the view closed or moved on before this ran: nothing is written
        if (!now) return 'removed';
        if (JSON.stringify(now) !== m.snapshot) return 'changed';
        if (kind === 'remove') {
          // Its link goes first: if that write fails, the title stays kept (never a link left for a later keep).
          if (values[`link.${m.key}`]) await cinema.storage.set(`link.${m.key}`, null);
          await cinema.storage.set(`kept.${m.key}`, null);
          return 'ok';
        }
        const {display, ...stated} = now;  // the stated facts are kept exactly; only the label changes
        // A longer label must still fit the write: only the kept description gives way (fitDescription), never the label.
        await cinema.storage.set(`kept.${m.key}`, fitDescription(`kept.${m.key}`, clean && clean !== stated.title ? {...stated, display: {title: clean, supplied: 'user'}} : stated));
        return 'ok';
      });
    } catch (error) { if (gone(error)) return; outcome = 'failed'; }
    if (outcome === 'closed' || mine !== epoch) return;
    const shown = m.row.display || m.row.title;
    const also = othersText(m.row.others);
    say({ok: kind === 'remove' ? `Removed “${shown}”.${also ? ` It is still in Advanced Media through ${also}.` : ''}` : (clean && clean !== m.row.title ? 'Saved your label.' : 'Back to the stated title.'),
         removed: 'This title was already removed, so nothing was changed.', changed: 'This title changed since you opened it, so nothing was saved. Try again.',
         failed: 'Advanced Media could not save this change.'}[outcome]);
    backTo();   // the same title's Rename when it is still there (a removed title: the view's first control)
    loaded = 0;refresh(true);
  }
  // Read again whenever the view opens and right after a Keep, so a title kept a moment ago is never missing.
  refresh(true);
  return {changed() { loaded = 0; refresh(true); }};
}

// Supplied chapters: a small local JSON file the user chooses (cinema.chapter-set/1), bound to one exact file
// by its full SHA-256 and size. The backend checks it ("chapters.check") and lists the kept sets
// ("chapters.list"); this page writes or removes one storage key per set through the serialized path.
// Nothing is fetched; a set is never tied to a kept title, its ids or an edition. Its section sits above the
// Library's own status line, and a failed list read stays quiet (it never speaks over the Library).
export const CHAPTER_FILE_LIMIT = 3072;  // characters: the module's 4 KiB request limit, with room for the envelope
export function setSummary(set) {
  const n = set.chapters.length, who = set.provenance.producer;
  return `${n} chapter${n === 1 ? '' : 's'} from “${who}” for one file (${set.binding.bytes} bytes, SHA-256 ${set.binding.value.slice(0, 12)}…)`;
}
function chapterSets(cinema, serial, changed, focus) {
  let sets = [], pending = null, epoch = 0, loading = false, said = '';
  const say = (text) => { said = text;changed(); };
  const gone = (error) => String(error?.message || error).includes('unloaded');
  const render = () => changed();
  const importButton = {focus: () => focus('chapters-import')};
  const text = (t, tone = 'muted') => ({kind: 'text', text: t, tone});
  // Its part of the Library: what supplied chapters are, Import (a file the user picks), each kept set, a pending import
  // to confirm, and its own status line (quiet when a list read fails: it never speaks over the Library).
  function section() {
    const items = [text('Supplied chapters: chapter lists you bring for one exact file. They are shown only when that file plays. Their accuracy and the edition are not checked.'),
      {kind: 'file', id: 'chapters-import', label: 'Import chapters…', accept: '.json,application/json', max: CHAPTER_FILE_LIMIT, disabled: !!pending}];
    for (const s of sets) items.push(text(`${s.count} chapter${s.count === 1 ? '' : 's'} from “${s.producer}” · file of ${s.bytes} bytes · SHA-256 ${s.short}…`, 'normal'),
      {kind: 'action', id: `chapters-remove.${s.key}`, label: 'Remove'});
    if (pending) items.push(text(`Import ${setSummary(pending.set)}?${pending.replaces ? ' This replaces the set you have for that file.' : ''} They stay your supplied chapters: shown for that exact file only, never checked.`, 'normal'),
      {kind: 'action', id: 'chapters-confirm', label: 'Import', tone: 'primary'}, {kind: 'action', id: 'chapters-cancel', label: 'Cancel'});
    if (said) items.push(text(said, 'normal'));
    return {heading: 'Supplied chapters', items};
  }
  function act({id, file}) {
    if (id === 'chapters-import') { if (file) picked(file);return true; }
    if (id === 'chapters-confirm') { commit();return true; }
    if (id === 'chapters-cancel') { pending = null;epoch += 1;say('Nothing was imported.');importButton.focus();return true; }
    if (id.startsWith('chapters-remove.')) { const s = sets.find(x => x.key === id.slice('chapters-remove.'.length));if (s) remove(s);return true; }
    return false;
  }
  async function refresh() {
    if (loading || pending) return;
    loading = true;const mine = epoch;
    try {
      const answer = await cinema.backend.read('chapters.list');
      if (mine !== epoch || pending) return;
      sets = answer?.ok && Array.isArray(answer.value?.sets) ? answer.value.sets : [];
      render();
    } catch { /* quiet: the list stays as it was */ }
    finally { loading = false; }
  }
  async function picked(file) {
    if (pending) return;
    const mine = ++epoch;
    if (file.tooLarge || file.size > CHAPTER_FILE_LIMIT || typeof file.text !== 'string') { say(file.tooLarge || file.size > CHAPTER_FILE_LIMIT ? `This file is too large (at most ${CHAPTER_FILE_LIMIT} characters).` : 'This file could not be read.'); return; }
    let parsed;
    try { parsed = JSON.parse(file.text); } catch { if (mine === epoch) say('This file is not valid JSON.'); return; }
    let answer;
    try { answer = await cinema.backend.read('chapters.check', {set: parsed}); } catch (error) { if (!gone(error) && mine === epoch) say('This file could not be checked.'); return; }
    if (mine !== epoch) return;
    if (!answer?.ok) { say('This file could not be checked.'); return; }
    if (answer.value.state !== 'ok') { say(answer.value.message || 'This file was not imported.'); return; }
    pending = {key: answer.value.key, set: answer.value.set, replaces: !!answer.value.replaces, mine};say('');focus('chapters-confirm');
  }
  async function commit() {
    const p = pending;if (!p) return;
    pending = null;const mine = ++epoch;say('Importing…');render();
    let outcome;
    try {
      outcome = await serial(async () => {
        if (mine !== epoch) return 'closed';
        await cinema.storage.set(`chapters.${p.key}`, p.set);
        return 'ok';
      });
    } catch (error) { if (gone(error)) return; outcome = 'failed'; }
    if (outcome === 'closed' || mine !== epoch) return;
    say(outcome === 'ok' ? `Imported ${setSummary(p.set)}.` : 'Advanced Media could not keep this set (it may be too large for its storage).');
    importButton.focus?.();refresh();
  }
  async function remove(s) {
    if (pending) return;
    const mine = ++epoch;say('Removing…');
    let outcome;
    try {
      outcome = await serial(async () => {
        const values = await cinema.storage.get();
        if (mine !== epoch) return 'closed';
        if (!values[`chapters.${s.key}`]) return 'gone';
        await cinema.storage.set(`chapters.${s.key}`, null);
        return 'ok';
      });
    } catch (error) { if (gone(error)) return; outcome = 'failed'; }
    if (outcome === 'closed' || mine !== epoch) return;
    say({ok: 'Supplied chapters removed.', gone: 'These chapters were already removed.', failed: 'Advanced Media could not remove these chapters.'}[outcome]);
    importButton.focus?.();refresh();
  }
  refresh();
  return {refresh, section, act};
}

export function unmount() {
  // Cinema removes its UI, focus group and subscriptions; the trailer player also stops listening and goes now.
  trailers?.dispose();trailers = null;
}

// Trailers. A title's trailer plays inside its thumbnail when the user rests on it (DWELL: loaded at 2 s, playing at 5 s,
// by pointer or keyboard focus) and continues in the title's card from the same moment: the SAME iframe, in the page's shared media layer (capability page.media), only
// its geometry changes (the layer is never moved, so nothing reloads).
//
// What plays: the official privacy-enhanced YouTube embedded player for the id KinoCheck lists (backend read
// "trailer"; the core's cinema.trailers service). Nothing is downloaded, extracted or proxied. The player is driven
// only through its own postMessage protocol (the one the IFrame API speaks), from this page; YouTube's script is
// never loaded into Cinema's page. Messages are accepted only from that player's origin and window. Muted preview;
// sound only by the user's choice. One player at a time; it goes on leave, card close, another title, real playback
// starting, or this module being turned off; it pauses while the page is hidden or the player is mostly off screen.
export const EMBED_ORIGIN = 'https://www.youtube-nocookie.com';
export const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
// Resting on a thumbnail (pointer or keyboard focus): at 2 s the player loads inside the thumbnail, still invisible and
// not playing; at 5 s it appears there and plays, muted. Leaving earlier cancels it.
export const DWELL = {buffer: 2000, play: 5000};
export const LEAVE_GRACE = 220;                     // ms: pointer moving from the tile onto the preview itself
export const POLL = {every: 350, tries: 14};       // backend read while a lookup is pending
// In the card, a trailer takes room only once its player says it is ready (2026-11-15: a player that never loads left a large
// broken frame); not ready within this long (ms), it gives way to one short line.
export const READY_LIMIT = 8000;
// The card's geometry is the SKIN's (2026-11-18, review of bi): the stage's shape and the controls' size and arrangement
// come from the drawn media surface; this module measures the stage it was given (media.surface's rect) and lays its
// picture over it, as a preview lays it over the tile's art. No ratio, strip height or slot size is decided here.
export const ERRORS = {2: 'This trailer has an invalid id.', 5: 'This trailer cannot be played here.',
  100: 'This trailer was removed or made private.', 101: 'The trailer\'s owner does not allow it to be played outside YouTube.',
  150: 'The trailer\'s owner does not allow it to be played outside YouTube.', 153: 'YouTube refused this player (no client identity).'};

export function embedURL(video, {autoplay = true, start = 0, origin, muted = true}) {
  if (!VIDEO_ID.test(video)) throw new Error('not a video id');
  // As little of YouTube's own interface as its documented parameters allow: no control bar (controls=0), no keyboard
  // handling (disablekb), no fullscreen button (fs=0), no annotations, related videos from the same channel only (rel=0).
  // What YouTube still draws itself (its logo, a title at the start, pause and end screens) may not be covered, cropped
  // or hidden under its terms, and is not. Cinema's own controls (the card's strip) play, pause and unmute.
  const q = new URLSearchParams({enablejsapi: '1', autoplay: autoplay ? '1' : '0', mute: muted ? '1' : '0', controls: '0', disablekb: '1', fs: '0',
    playsinline: '1', rel: '0', iv_load_policy: '3', origin});
  if (start > 0) q.set('start', String(Math.floor(start)));
  return `${EMBED_ORIGIN}/embed/${video}?${q}`;
}

// YouTube's required minimum functionality: an embedded player's viewport is at least 200 x 200 px.
// A thumbnail smaller than that grows about its centre just enough to meet it (the player stays the thumbnail, at most
// MAX_GROW times its size); only one that would need more gets no embedded preview.
export const MIN_PLAYER = 200, MAX_GROW = 1.4;
export const grow = (box) => Math.max(1, MIN_PLAYER / Math.max(1, box.width), MIN_PLAYER / Math.max(1, box.height));
export const fits = (box) => box.width > 0 && box.height > 0 && grow(box) <= MAX_GROW;
// The thumbnail's box, grown when needed (rounded up so the minimum holds), kept inside the viewport.
export function thumbnailBox(art, viewport, margin = 8) {
  const k = grow(art), width = Math.ceil(art.width * k), height = Math.ceil(art.height * k);
  let left = art.left + art.width / 2 - width / 2, top = art.top + art.height / 2 - height / 2;
  left = Math.max(margin, Math.min(left, viewport.width - margin - width));
  top = Math.max(margin, Math.min(top, viewport.height - margin - height));
  return {left: Math.round(left), top: Math.round(top), width, video: height, grown: k > 1};
}

// How much of a rectangle is inside another (0..1): autoplay needs the player more than half visible.
export function visibleFraction(rect, clip) {
  const w = Math.max(0, Math.min(rect.right, clip.right) - Math.max(rect.left, clip.left));
  const h = Math.max(0, Math.min(rect.bottom, clip.bottom) - Math.max(rect.top, clip.top));
  return (w * h) / Math.max(1, rect.width * rect.height);
}

export function mountTrailers(cinema, {doc = globalThis.document, win = globalThis.window, frame = (fn) => win.requestAnimationFrame(fn),
  cancelFrame = (id) => win.cancelAnimationFrame(id), reducedMotion = () => !!win.matchMedia?.('(prefers-reduced-motion: reduce)').matches} = {}) {
  const media = cinema.media, timers = cinema.timers;
  // The card's one trailer (2026-11-18, R9): this module asks to be it; refused (another holds it), it shows previews only.
  const claimed = typeof media.claim === 'function' ? media.claim('trailer') === true : true;
  // The card's slot, or null (no card; or this module already unloaded: a frame or timer still pending never throws).
  const slotNow = () => { try { return media.slot(); } catch { return null; } };
  let intent = null, dwell = null, leaveTimer = null, polling = null, player = null, overLayer = false, raf = null, generation = 0;
  const answers = new Map();        // item token -> backend answer (this page session, bounded)
  const resumeAt = new Map();       // video -> seconds, so a preview reopened after the card continues
  const stats = {created: 0, destroyed: 0, lookups: 0, lastDestroy: null, tooSmall: 0};

  const remember = (item, answer) => { answers.set(item, answer); if (answers.size > 200) answers.delete(answers.keys().next().value); };
  // One command surface for both players: YouTube's embed by its own postMessage protocol, a direct trailer's <video>
  // element directly (its play() may be refused by the browser's autoplay policy: then it stays paused, truthfully).
  const send = (func, args = []) => {
    if (!player) return;
    if (player.direct) {
      const element = player.frame, state = player;
      try {
        if (func === 'playVideo') {
          const started = element.play();
          if (started && typeof started.catch === 'function') started.catch((error) => {
            if (player !== state) return;
            // With sound refused (the browser's autoplay policy): the card plays it muted rather than not at all.
            if (state.mode === 'card' && !element.muted) { element.muted = true;state.muted = true;state.label?.();send('playVideo');return; }
            state.blocked = true; state.label?.(); trace({op: 'blocked', name: error?.name || 'refused'});
          });
        } else if (func === 'pauseVideo') element.pause();
        else if (func === 'mute') element.muted = true;
        else if (func === 'unMute') element.muted = false;
      } catch { /* a detached element: nothing to do */ }
      return;
    }
    player.frame.contentWindow?.postMessage(JSON.stringify({event: 'command', func, args, id: 1, channel: 'widget'}), EMBED_ORIGIN);
  };
  // No captions (user request, 2026-11-14): YouTube turns them on by itself for some videos and for muted autoplay.
  const captionsOff = () => { if (player && !player.direct) { send('unloadModule', ['captions']); send('unloadModule', ['cc']); } };
  const playing = (state) => state.state === 1 || state.state === 3;
  let lastPointer = null;  // the last position this page saw (none while the pointer is inside the player's own document)
  const pointerInside = (el) => { if (!lastPointer || !el?.getBoundingClientRect) return false; const r = el.getBoundingClientRect();
    return lastPointer.x >= r.left - 2 && lastPointer.x <= r.right + 2 && lastPointer.y >= r.top - 2 && lastPointer.y <= r.bottom + 2; };
  // TEST diagnostics only: recorded when the page defines globalThis.__cinemaTrailerTrace as an array.
  const trace = (e) => { const T = globalThis.__cinemaTrailerTrace; if (Array.isArray(T) && T.length < 500) T.push({t: Math.round(globalThis.performance?.now?.() ?? 0), ...e}); };

  async function lookup(item, mine) {
    if (answers.has(item)) return answers.get(item);
    for (let i = 0; i < POLL.tries && mine === generation; i++) {
      let answer;
      try { answer = await cinema.backend.read('trailer', {item}); } catch { return null; }
      stats.lookups += 1;
      const value = answer?.ok === false ? null : (answer?.value ?? answer);
      if (!value) return null;
      if (value.state !== 'pending') { remember(item, value); return value; }
      await new Promise((resolve) => { polling = timers.setTimeout(resolve, POLL.every); });
    }
    return null;
  }

  // The card's controls for its trailer (or a note, or the listing's trailer): a SEMANTIC surface in the slot that the
  // page's skin draws (placement 'media', 2026-11-18, R16). This module draws nothing there of its own.
  let controls = null, drawn = '';
  const dropControls = () => { controls?.remove();controls = null;drawn = ''; };
  function showControls(descriptor, onAction) {
    const json = JSON.stringify(descriptor);
    if (controls && json === drawn) return controls;   // unchanged (the player reports often): no redraw
    drawn = json;
    if (controls) { controls.update(descriptor);return controls; }
    controls = media.surface?.(descriptor, {onAction: (payload) => onAction(payload)}) || null;
    return controls;
  }
  const stageOf = () => controls?.control?.('video') || null;

  function clearSlot() {
    const slot = slotNow();
    const had = !!slot && slot.contains(doc.activeElement);
    dropControls();
    if (slot && slot.dataset.owner === 'am-trailer') { slot.hidden = true;slot.replaceChildren();slot.removeAttribute('style');slot.removeAttribute('data-state');delete slot.dataset.owner; }
    slotFocusOut(null);
    if (had) media.releaseFocus?.();   // the video or its controls had focus: the page puts it on the card's action
  }

  // The slot's focusout listener (2026-11-16): Chromium/WebView2 has no `onfocusout` handler property (assigning one never
  // fired, so the trailer's controls never slid away when focus left them). One listener at a time, removed with the slot.
  // With it, its focusin listener (the stage taking focus: the ring and centring).
  let focusOutOn = null, focusOutFn = null, focusInFn = null;
  function slotFocusOut(slot, fn, inFn = null) {
    if (focusOutOn && focusOutFn) focusOutOn.removeEventListener?.('focusout', focusOutFn);
    if (focusOutOn && focusInFn) focusOutOn.removeEventListener?.('focusin', focusInFn);
    focusOutOn = slot && fn ? slot : null;focusOutFn = slot && fn ? fn : null;focusInFn = slot && inFn ? inFn : null;
    if (focusOutOn) focusOutOn.addEventListener?.('focusout', focusOutFn);
    if (focusOutOn && focusInFn) focusOutOn.addEventListener?.('focusin', focusInFn);
  }

  function destroy(reason) {
    if (!player) return;
    const was = player;player = null;
    if (was.readyTimer != null) timers.clear(was.readyTimer);
    if (was.time > 1) resumeAt.set(was.video, was.time);
    if (raf !== null) { cancelFrame(raf); raf = null; }
    if (was.direct) {
      // Stop the element's download now (no src, load()), and end the scoped media session.
      try { was.frame.pause(); was.frame.removeAttribute('src'); was.frame.load?.(); } catch { /* already gone */ }
      if (was.src) Promise.resolve(cinema.backend.read('trailer.release', {src: was.src})).catch(() => {});
    }
    was.remove();was.wrap.remove();
    if (was.mode === 'card') clearSlot();
    stats.destroyed += 1;stats.lastDestroy = reason;
    trace({op: 'destroy', reason, item: was.item, mode: was.mode});
    overLayer = false;
  }

  // The player said it is ready: in the card, its slot opens now (and the not-ready timer ends).
  function shown(state) {
    if (state.readyTimer != null) { timers.clear(state.readyTimer);state.readyTimer = null; }
    if (state.mode !== 'card' || player !== state) return;
    const slot = slotNow();
    if (slot && slot.dataset.owner === 'am-trailer') { slot.hidden = false;slot.removeAttribute('data-state'); }
    if (raf === null) raf = frame(place);
    // Video-first (approved playback UX, 2026-11-17): the actual video, now ready, is offered focus; the page grants it
    // only while the user has not acted in the card.
    const stage = stageOf();if (stage) media.offerFocus?.(stage);
  }

  const listingOffer = () => media.details?.listing_trailer && claimed
    ? {action: {kind: 'action', id: 'listing', label: 'Play the listing\'s trailer'}, run: () => media.listingTrailer?.()} : null;
  function note(text, offer = listingOffer()) {
    const slot = slotNow();
    if (!slot) return;
    dropControls();
    slot.dataset.owner = 'am-trailer';slot.dataset.state = 'note';slot.removeAttribute('style');slot.hidden = false;slot.replaceChildren();
    showControls({sections: [{items: [{kind: 'text', text, tone: 'muted'}, ...(offer ? [offer.action] : [])]}]}, ({id}) => { if (offer && id === offer.action.id) offer.run(); });
  }

  // A direct trailer (transport "direct"): a Cinema-drawn <video>, its source a scoped same-origin media session the
  // backend opens only now (read "trailer.media"; the provider's handle and URL never reach this page).
  async function prepareDirect(state) {
    let answer = null;
    try { answer = await cinema.backend.read('trailer.media', {item: state.item}); } catch { answer = null; }
    const value = answer?.ok === false ? null : (answer?.value ?? answer);
    if (player !== state) { if (value?.src) Promise.resolve(cinema.backend.read('trailer.release', {src: value.src})).catch(() => {}); return; }
    if (!value || value.state !== 'ready' || typeof value.src !== 'string' || !value.src.startsWith('/api/media/')) {
      const mode = state.mode;destroy('media unavailable');
      if (mode === 'card') note('This trailer cannot be played right now.');
      return;
    }
    state.src = value.src;state.frame.src = value.src;state.frame.load?.();
    trace({op: 'media', item: state.item});
    if (state.mode === 'card' && !reducedMotion()) send('playVideo');
  }

  function directElement(state, start) {
    const element = doc.createElement('video');
    element.className = 'c-media-frame';element.tabIndex = -1;element.muted = true;element.defaultMuted = true;
    element.playsInline = true;element.preload = 'auto';element.controls = false;element.disablePictureInPicture = true;
    element.setAttribute('aria-label', state.answer.title ? `Trailer: ${state.answer.title}` : 'Trailer');
    const on = (type, fn) => element.addEventListener(type, () => { if (player === state) { fn(); state.label?.(); } });
    on('playing', () => { state.state = 1;state.blocked = false; });
    on('pause', () => { state.state = 2; });
    on('waiting', () => { state.state = 3; });
    on('ended', () => { state.state = 0; });
    on('timeupdate', () => { state.time = element.currentTime; });
    on('volumechange', () => { state.muted = element.muted; });
    on('loadedmetadata', () => { const was = state.ready;state.ready = true;state.loads += 1; if (start > 0 && start < (element.duration || 0)) element.currentTime = start; if (!was) shown(state); });
    element.addEventListener('error', () => {
      if (player !== state) return;
      const mode = state.mode;destroy('media error');
      if (mode === 'card') note('This trailer could not be played.');
    });
    return element;
  }

  function build(item, answer, mode) {
    // The page's public media vocabulary (.c-media, .c-media-frame; 2026-11-18, R16): looks from the page and skin, the
    // geometry this module's own (inline, below). Embedded in the thumbnail, nothing is drawn beside or over it.
    const wrap = doc.createElement('div');wrap.className = 'c-media';wrap.dataset.mode = mode;
    if (answer.transport === 'direct') return buildDirect(item, answer, mode, wrap);
    const frameEl = doc.createElement('iframe');
    frameEl.className = 'c-media-frame';frameEl.tabIndex = -1;frameEl.title = answer.title ? `Trailer: ${answer.title}` : 'Trailer';
    frameEl.allow = 'autoplay; encrypted-media; picture-in-picture';frameEl.referrerPolicy = 'strict-origin-when-cross-origin';
    wrap.append(frameEl);
    const state = {item, video: answer.video, answer, mode, wrap, frame: frameEl, time: 0, state: -1, muted: mode !== 'card', loads: 0,
      ready: false, blocked: false, autoPaused: false, revealed: false, entry: null, label: null};
    frameEl.addEventListener('load', () => {
      state.loads += 1;
      frameEl.contentWindow?.postMessage(JSON.stringify({event: 'listening', id: 1, channel: 'widget'}), EMBED_ORIGIN);
    });
    wrap.addEventListener('pointerenter', () => { overLayer = true; if (leaveTimer !== null) { timers.clear(leaveTimer); leaveTimer = null; } });
    // A pointer moving INTO the player's own (cross-origin) document is a leave for this page, though it is still over the
    // preview: only a leave whose last known position is outside the preview counts (pointerInside).
    wrap.addEventListener('pointerleave', () => { if (pointerInside(wrap)) return; overLayer = false; if (state.mode === 'preview' && !(intent && intent.item === item)) scheduleLeave(item); });
    state.remove = media.mount(wrap);
    const start = resumeAt.get(answer.video) || 0;
    // A preview is muted and autoplays (only after the user's dwell); in the card it autoplays unless the user prefers
    // reduced motion, then it waits for Play.
    // A preview loads with autoplay off (it is revealed and played at the 5 s mark); in the card it autoplays unless the
    // user prefers reduced motion, then it waits for Play.
    // In the card it starts with sound (user request, 2026-11-14); a preview in a thumbnail stays silent.
    frameEl.src = embedURL(answer.video, {autoplay: mode === 'card' && !reducedMotion(), start, origin: win.location.origin, muted: mode !== 'card'});
    stats.created += 1;trace({op: 'create', item, mode, start});
    media.show();
    return state;
  }

  function buildDirect(item, answer, mode, wrap) {
    const key = 'direct:' + item;
    const state = {item, video: key, answer, mode, wrap, frame: null, time: 0, state: -1, muted: true, loads: 0,
      ready: false, blocked: false, autoPaused: false, revealed: false, entry: null, label: null, direct: true, src: null};
    const start = resumeAt.get(key) || 0;
    state.frame = directElement(state, start);
    if (mode === 'card') { state.frame.muted = false;state.frame.defaultMuted = false;state.muted = false; }
    wrap.append(state.frame);
    state.remove = media.mount(wrap);
    stats.created += 1;trace({op: 'create', item, mode, start, direct: true});
    media.show();
    prepareDirect(state);  // the session opens now (the 2 s mark for a preview): real buffering, still invisible
    return state;
  }

  function cardBar(state) {
    // The card's own controls for the trailer, in the slot's reserved strip under the player (in the card's focus
    // order, never over the player). The player's own controls stay usable as YouTube draws them.
    const slot = slotNow();
    if (!slot) return;
    // The video itself is a stop in the card's focus order (user request, 2026-11-14): Up from the card's actions reaches
    // it and centres it on screen; Down from it slides the controls out from under it (and focus onto them); leaving the
    // video and its controls slides them back. Nothing of the player's own is shown by default; Enter on the video
    // pauses or plays it.
    dropControls();
    slot.dataset.owner = 'am-trailer';slot.removeAttribute('data-state');slot.hidden = !state.ready;slot.replaceChildren();
    if (!state.ready) {
      slot.dataset.state = 'waiting';
      if (state.readyTimer == null) state.readyTimer = timers.setTimeout(() => {
        state.readyTimer = null;
        if (player !== state || state.ready) return;
        destroy('not ready');note('Trailer unavailable right now.');
      }, READY_LIMIT);
    }
    // Its parts, as meaning only: the video itself (a stage action: the focus stop over the picture; `expanded` says whether
    // its controls are out), then Pause/Play, Sound and the credit. The skin draws them, and shows or hides the controls
    // (and animates that) by the stage's expanded state; their order and focus are the page's.
    const who = state.answer.scope === 'show' ? 'The show\'s trailer' : 'Trailer';
    const credit = `${who} from ${state.answer.provider_label || 'KinoCheck'}${state.direct ? '' : ' · YouTube'}`;
    state.barOpen = false;
    const descriptor = () => ({sections: [
      {items: [{kind: 'action', id: 'video', stage: true, expanded: state.barOpen, label: `${who}. Enter pauses or plays; Down shows its controls.`}]},
      {items: [{kind: 'action', id: 'play', label: playing(state) ? 'Pause trailer' : 'Play trailer'},
        {kind: 'action', id: 'sound', label: state.muted ? 'Sound on' : 'Sound off'}, {kind: 'text', text: credit, tone: 'muted'}]}]});
    const toggle = () => { if (playing(state)) send('pauseVideo'); else send('playVideo'); state.autoPaused = false; };
    const act = ({id}) => {
      if (player !== state) return;
      if (id === 'video' || id === 'play') toggle();
      else if (id === 'sound') { const on = state.muted;send(on ? 'unMute' : 'mute');state.muted = !on;if (on) captionsOff();label(); }
    };
    const label = () => { if (player === state && slotNow()) showControls(descriptor(), act); };
    state.label = label;label();
    // The video (and, with them out, its controls) centred in the card's visible area, by the PAGE at the slot's own
    // drawn height (media.reveal): this module neither scrolls the card nor states its size.
    const center = () => frame(() => {
      if (!slotNow() || player !== state) return;
      media.reveal?.();
    });
    const open = (yes) => {
      if (state.barOpen === yes || player !== state) return;
      state.barOpen = yes;label();
      if (raf === null) raf = frame(place);
    };
    state.openBar = open;
    // Where focus is, read from the slot (this module's region of the page): the ring around the player while its stage
    // has focus (and it is centred); focus leaving the video and its controls puts the controls away.
    const keyOf = (node) => node?.dataset?.surfaceKey || null;
    state.onStage = () => keyOf(doc.activeElement) === 'video' && !!controls?.element?.contains?.(doc.activeElement);
    state.inControls = () => ['play', 'sound'].includes(keyOf(doc.activeElement)) && !!controls?.element?.contains?.(doc.activeElement);
    // Centred only when focus ARRIVES on the stage: a redraw that keeps focus there (e.g. the label changing when the
    // trailer pauses because the card was scrolled) never scrolls the user's card back (review of bi, offline_media).
    state.stageFocused = false;
    // A redraw (Chromium fires focusout as it removes the focused control, then focus is restored to the same key) is told
    // apart by looking once the redraw is over (a microtask): focus back inside means nothing changed for the user.
    slotFocusOut(slot, (event) => {
      const fromStage = keyOf(event.target) === 'video' && keyOf(event.relatedTarget) !== 'video', leaving = !slot.contains(event.relatedTarget);
      if (!fromStage && !leaving) return;
      queueMicrotask(() => {
        if (player !== state) return;
        const now = doc.activeElement, inside = !!now && slot.contains(now);
        if (fromStage && keyOf(now) !== 'video') { delete state.wrap.dataset.focused;state.stageFocused = false; }
        if (leaving && !inside) open(false);
      });
    }, (event) => {
      if (keyOf(event.target) !== 'video') return;
      const arrived = !state.stageFocused;state.stageFocused = true;state.wrap.dataset.focused = 'true';
      if (arrived) center();
    });
    state.reveal = () => { open(true);frame(() => { controls?.focus('play');center(); }); };
    // Already ready (a preview promoted into the card): offered focus now; the page grants it only on a fresh card.
    if (state.ready) { const stage = stageOf();if (stage) media.offerFocus?.(stage); }
  }

  function place() {
    raf = null;
    const state = player;
    if (!state) return;
    let box, clip;
    const vw = win.innerWidth, vh = win.innerHeight;
    if (state.mode === 'card') {
      const slot = slotNow();
      if (!slot) { destroy('no slot'); return; }
      // Over the stage the skin drew, at its measured place and size; until the player is ready (or the skin has drawn a
      // stage), out of sight at the page's default media box: no room is taken in the card.
      const r = state.ready ? controls?.rect?.('video') : null;
      if (!r || !(r.width > 0) || !(r.height > 0)) {
        const s = state.wrap.style;s.visibility = 'hidden';s.pointerEvents = 'none';raf = frame(place);return;
      }
      box = {left: r.left, top: r.top, width: r.width, video: Math.round(r.height)};
      clip = media.slotClip?.() || {left: 0, top: 0, right: vw, bottom: vh};   // where the page's card clips the slot
    } else {
      const entry = state.entry;
      if (!entry || !entry.connected) { destroy('tile gone'); return; }
      // Embedded in the thumbnail itself: exactly the thumbnail's box (the tile's art), nothing larger.
      const art = (entry.art || entry.rect)();
      clip = {left: 0, top: 0, right: vw, bottom: vh};
      if (visibleFraction(art, clip) < 0.5) { destroy('tile offscreen'); return; }
      if (state.direct) {
        box = {left: art.left, top: art.top, width: art.width, video: Math.round(art.height), grown: false};  // no embed minimum
      } else {
        if (!fits(art)) { destroy('thumbnail too small'); return; }
        box = thumbnailBox(art, {width: vw, height: vh});
      }
      state.wrap.dataset.grown = box.grown ? 'true' : 'false';
    }
    const s = state.wrap.style;
    s.left = `${Math.round(box.left)}px`;s.top = `${Math.round(box.top)}px`;s.width = `${Math.round(box.width)}px`;
    state.frame.style.height = `${box.video}px`;
    const rect = {left: box.left, top: box.top, right: box.left + box.width, bottom: box.top + box.video, width: box.width, height: box.video};
    const shown = visibleFraction(rect, clip) >= 0.5;
    // Playing only while more than half of the player is visible; hidden, it pauses and resumes when back.
    if (!shown && playing(state)) { send('pauseVideo'); state.autoPaused = true; }
    if (shown && state.autoPaused && doc.visibilityState !== 'hidden' && (state.revealed || state.mode === 'card')) { send('playVideo'); state.autoPaused = false; }
    s.visibility = shown ? 'visible' : 'hidden';
    // Loaded but not yet revealed (the 2 to 5 s window): invisible and not interactive; the thumbnail shows through.
    const hidden = state.mode === 'preview' && !state.revealed;
    // Never interactive: pointer input goes to Cinema (the tile, the card), never to YouTube, so YouTube's hover
    // overlay never appears; a click on a playing thumbnail selects the title. Cinema's own controls drive the player.
    s.opacity = hidden ? '0' : '1';s.pointerEvents = 'none';state.frame.style.pointerEvents = 'none';
    raf = frame(place);
  }

  function start(entry, answer, mode) {
    if (player && player.item === entry.item) return;
    const direct = answer?.transport === 'direct';
    if (mode === 'preview' && !direct) {
      const tile = entry.entry || entry, measure = tile.art || tile.rect;
      if (typeof measure !== 'function' || !fits(measure())) { stats.tooSmall += 1; return; }
    }
    destroy('replaced');
    if (!answer || answer.state !== 'found') return;
    if (!direct && (answer.transport !== 'youtube' || !VIDEO_ID.test(answer.video || ''))) return;
    if (direct && answer.container !== 'mp4') return;
    player = build(entry.item, answer, mode);player.entry = entry.entry || null;
    if (mode === 'card') cardBar(player);
    if (raf === null) raf = frame(place);
  }

  function reveal(item) {
    if (!player || player.item !== item || player.mode !== 'preview' || player.revealed) return;
    player.revealed = true;trace({op: 'reveal', item});
    if (!player.blocked) send('playVideo');
    if (raf === null) raf = frame(place);
  }

  function promote() {
    // The card opened for the previewed title: same player, new geometry, its controls in the card.
    player.revealed = true;player.mode = 'card';player.wrap.dataset.mode = 'card';
    // The card is the user's choice of this title: its trailer continues with sound, without captions.
    send('unMute');player.muted = false;captionsOff();
    cardBar(player);
    if (!playing(player) && !reducedMotion() && !player.blocked) send('playVideo');
    if (raf === null) raf = frame(place);
  }

  function scheduleLeave(item) {
    if (leaveTimer !== null) timers.clear(leaveTimer);
    leaveTimer = timers.setTimeout(() => {
      leaveTimer = null;
      if (player && player.item === item && player.mode === 'preview' && !overLayer && !pointerInside(player.wrap) && !(intent && intent.item === item)) destroy('left');
    }, LEAVE_GRACE);
  }

  media.onFocus((event) => {
    trace({op: 'focus', kind: event.kind, via: event.via, item: event.item, card: !!media.details});
    if (media.details) return;  // the card is open: the tiles behind it are not where the user is
    if (event.kind === 'enter') {
      if (dwell !== null) timers.clear(dwell);
      intent = event;const mine = ++generation;
      if (player && player.item !== event.item && player.mode === 'preview') destroy('another title');
      if (reducedMotion()) return;  // no moving previews; the card still offers the trailer
      const landed = Date.now();
      dwell = timers.setTimeout(async () => {
        dwell = null;
        const answer = await lookup(event.item, mine);
        if (mine !== generation || !intent || intent.item !== event.item || media.details) return;  // moved on meanwhile
        start(event, answer, 'preview');
        if (!player || player.item !== event.item) return;
        // Revealed at the 5 s mark (counted from landing on the thumbnail), or at once if the lookup took longer.
        dwell = timers.setTimeout(() => { dwell = null; if (mine === generation && intent?.item === event.item) reveal(event.item); },
                                  Math.max(0, DWELL.play - DWELL.buffer - Math.max(0, Date.now() - landed - DWELL.buffer)));
      }, DWELL.buffer);
    } else {
      if (intent && intent.item === event.item && intent.via === event.via) {
        intent = null;generation += 1;
        if (dwell !== null) { timers.clear(dwell); dwell = null; }
      }
      if (player && player.item === event.item && player.mode === 'preview') scheduleLeave(event.item);
    }
  });

  media.onDetails(async (value) => {
    trace({op: 'details', item: value?.item ?? null, player: player?.item ?? null});
    if (dwell !== null) { timers.clear(dwell); dwell = null; }
    if (!value || !(player && value.item && player.item === value.item)) dropControls();   // a note or offer belongs to its card
    if (!value) { destroy('card closed'); intent = null; generation += 1; return; }
    // An episode's card shows no trailer by default (approved episode presentation, 2026-11-17): the only trailer an episode
    // has is its SHOW's, which the show's own card plays. Nothing is inserted just to give the episode a video.
    if (value.media_type === 'episode') { destroy('episode card'); return; }
    // The card's ONE trailer (R9): only while this module holds the page's trailer claim; otherwise the page's own.
    if (!claimed) { destroy('not the card\'s trailer'); return; }
    if (player && value.item && player.item === value.item) { promote(); return; }
    destroy('card for another title');
    const mine = ++generation, answer = value.item ? await lookup(value.item, mine) : null;
    if (mine !== generation || media.details !== value) return;
    if (answer?.state === 'found') start({item: value.item, entry: null}, answer, 'card');
    // No trailer of its own here: the listing's trailer (the page's action for this title, when it states one) is offered
    // in the same place, so claiming the card's trailer never takes one away.
    if (!player && value.listing_trailer) note('This title\'s listing has a trailer.');
  });

  const onMessage = (event) => {
    const state = player;
    if (!state || event.origin !== EMBED_ORIGIN || event.source !== state.frame.contentWindow) return;
    let data = event.data;
    try { data = typeof data === 'string' ? JSON.parse(data) : data; } catch { return; }
    if (!data || typeof data !== 'object') return;
    if (data.event === 'onReady') { const was = state.ready;state.ready = true;captionsOff(); if (!was) shown(state); }
    if (data.event === 'infoDelivery' && !state.ready) { state.ready = true;shown(state); }   // the player answering is ready too
    if (data.event === 'infoDelivery' && data.info && typeof data.info === 'object') {
      if (typeof data.info.currentTime === 'number') state.time = data.info.currentTime;
      if (typeof data.info.playerState === 'number') state.state = data.info.playerState;
      if (typeof data.info.muted === 'boolean') state.muted = data.info.muted;
      state.label?.();
    }
    if (data.event === 'onStateChange' && typeof data.info === 'number') { state.state = data.info; state.label?.(); }
    if (data.event === 'onAutoplayBlocked') {
      // Sound refused by the browser's autoplay policy: the card plays it muted (Sound on stays offered).
      if (state.mode === 'card' && !state.muted) { send('mute');state.muted = true;send('playVideo');state.label?.();return; }
      state.blocked = true; state.label?.();
    }
    if (data.event === 'onError') {
      const text = ERRORS[data.info] || 'This trailer cannot be played here.';
      const mode = state.mode;destroy(`error ${data.info}`);
      if (mode === 'card') note(text);
    }
  };
  win.addEventListener('message', onMessage);
  // Where the pointer is, as this page sees it: a move outside the preview after being over it is the real leave.
  const onPointer = (event) => {
    lastPointer = {x: event.clientX, y: event.clientY};
    if (player && player.mode === 'preview' && overLayer && !pointerInside(player.wrap)) { overLayer = false; if (!(intent && intent.item === player.item)) scheduleLeave(player.item); }
  };
  doc.addEventListener('pointermove', onPointer, true);
  // The slot's keys, through the page (media.onSlotKey, 2026-11-17 modularity audit): seen only while focus is inside
  // the slot, before the card's own handling; true = taken. No document-wide listener.
  // Down on the card's video: its controls slide out and take focus. Back (Escape, Backspace, a remote's Back) with focus
  // on the open controls closes them first, focus back on the video; the card closes only on the next Back.
  const BACK = ['Escape', 'Backspace', 'BrowserBack', 'GoBack'];
  const onKey = ({key}) => {
    if (!player || player.mode !== 'card') return false;
    if (BACK.includes(key) && player.barOpen && player.inControls?.()) {
      player.openBar?.(false);controls?.focus('video');
      return true;
    }
    if (key !== 'ArrowDown' || !player.reveal || !player.onStage?.()) return false;
    player.reveal();
    return true;
  };
  const offKey = media.onSlotKey?.(onKey) || (() => {});
  const onHidden = () => { if (doc.visibilityState === 'hidden' && player && playing(player)) { send('pauseVideo'); player.autoPaused = true; } };
  doc.addEventListener('visibilitychange', onHidden);
  // Real playback taking the screen ends any trailer: never two videos or two sounds.
  const offState = cinema.player?.onState?.((state) => { if (state) destroy('playback'); });

  return {
    get player() { return player; },
    stats,
    dispose() {
      destroy('unmount');
      win.removeEventListener('message', onMessage);doc.removeEventListener('visibilitychange', onHidden);doc.removeEventListener('pointermove', onPointer, true);offKey();
      if (dwell !== null) timers.clear(dwell);
      if (leaveTimer !== null) timers.clear(leaveTimer);
      if (polling !== null) timers.clear(polling);
      if (typeof offState === 'function') offState();
    }
  };
}

// Advanced Media's title-card template (2026-11-15): the ONE presentation policy for every title in Cinema's shared card
// (capability page.cards): an ordinary movie, TV show, season or episode from Kodi listings, the bridge or a title hub,
// and a native video extension's title. Pure. Its input is the core's provider-neutral title description
// (cinema.title/1: data with explicit unknowns, opaque references for actions and cards, and the core-drawn blocks the
// title has data for); its output is a layout in the core's fixed vocabulary. The rules are the established card's: the
// kind-and-genres line (an episode: "S1 E2"), year · runtime · short rating (no year for an episode), one primary action
// (a grouped title's "Play with…" beside it) with everything else under "•••", the established section order, the
// Information rows and one credit line. Nobody else decides any of it: providers, listings and metadata adapters only
// supply the data.
export const TEMPLATE = 'cinema.advanced-media/card/1';
const TEMPLATE_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const templateClock = min => min >= 60 ? `${Math.floor(min / 60)}h${min % 60 ? ` ${min % 60}m` : ''}` : `${min} min`;
const templateDate = value => { const [y, mo, d] = String(value || '').split('-').map(Number); return y && mo >= 1 && mo <= 12 && d >= 1 ? `${TEMPLATE_MONTHS[mo - 1]} ${d}, ${y}` : ''; };
const KIND = {movie: 'Movie', tvshow: 'Show', season: 'Season'};

export function presentTitle(title) {
  if (title.kind === 'group') return presentGroup(title);
  const has = ref => (title.blocks || []).includes(ref), block = ref => ({kind: 'block', ref});
  const rated = title.rating ? title.rating.replace(/^rated\s+/i, '').trim() : '';
  const line = title.kind === 'episode'
    ? (Number.isInteger(title.season) && Number.isInteger(title.episode) && title.season >= 0 && title.episode > 0 ? `S${title.season} E${title.episode}` : '')
    : [KIND[title.kind], ...title.genres.slice(0, 2)].filter(Boolean).join(' · ');
  const facts = [title.kind !== 'episode' && title.year ? String(title.year) : '', title.runtime_minutes > 0 ? templateClock(title.runtime_minutes) : '',
    rated && rated.length <= 10 ? rated : ''].filter(Boolean).join(' · ');
  const play = title.actions.find(a => a.group === 'play');
  const inline = title.actions.filter(a => a.group === 'inline');
  if (title.kind === 'episode') return presentEpisode(title, {line, rated, play, inline});
  const sections = [];
  // Seasons first, as on a TV show's card: the core's own listing of the show, or a series' parts as poster cards
  // (no artwork is invented: the label shows).
  if (has('seasons')) sections.push(block('seasons'));
  // A franchise umbrella (2026-11-17): its Seasons first, as a show's card lists its seasons; a partial read says so above
  // them. Its OVAs & Specials, Movies, side stories and other versions follow the episodes (placed below). Each card opens
  // that entry as itself. Empty sections are omitted.
  const umbrella = title.franchise || null;
  const shelf = s => ({kind: 'shelf', heading: s.heading, shape: 'poster',
    cards: s.items.map(e => ({ref: e.ref, title: e.order != null ? `${e.order}. ${e.label}` : e.label, note: e.facts, current: e.current}))});
  if (umbrella?.note) sections.push({kind: 'note', text: umbrella.note});
  for (const s of umbrella?.sections || []) if (s.id === 'seasons') sections.push(shelf(s));
  if (title.parts.items.length) {
    sections.push({kind: 'shelf', heading: title.parts.ordered ? 'Seasons' : 'Parts', shape: 'poster',
      cards: title.parts.items.map(p => ({ref: p.ref, title: p.order != null ? `${p.order}. ${p.label}` : p.label, note: p.facts, current: p.current}))});
    if (title.parts.note) sections.push({kind: 'note', text: title.parts.note});
  }
  // Episodes: a part's episodes as wide cards (the next announced one shown, never playable), or an episode's season.
  if (title.episodes.state !== 'none') {
    // An episode row (2026-11-17): "<number>. <its own title>" when a title is stated, else the provider's label
    // ("Episode 3"); the note: Special, and the air date. Its picture: its own still when stated, else a numbered
    // placeholder ("E3"), never a frame or the show's poster.
    const e = title.episodes, cards = e.items.map(ep => ({ref: ep.ref,
      title: ep.name ? (Number.isInteger(ep.number) ? `${ep.number}. ${ep.name}` : ep.name) : ep.label,
      note: [ep.special ? 'Special' : '', templateDate(ep.aired)].filter(Boolean).join(' · '),
      art: ep.still ? 'still' : 'placeholder', mark: Number.isInteger(ep.number) ? `${ep.special ? 'SP' : 'E'}${ep.number}` : ''}));
    if (e.more) cards.push({ref: e.more, title: 'More episodes', note: ''});
    if (e.upcoming) cards.push({ref: 'upcoming', title: e.upcoming.label, note: e.upcoming.note});
    sections.push({kind: 'shelf', heading: 'Episodes', shape: 'wide', cards,
      empty: e.state === 'loading' ? 'Loading episodes…' : !cards.length ? (e.message || 'No episode is listed yet.') : ''});
    if (e.note && cards.length) sections.push({kind: 'note', text: e.note});    // why the rows show numbers only
  }
  for (const s of umbrella?.sections || []) if (s.id !== 'seasons') sections.push(shelf(s));
  for (const ref of ['siblings', 'trailer', 'cast', 'known-for', 'collection', 'more-like-this']) if (has(ref)) sections.push(block(ref));
  // Other titles of the same series (specials, films, other versions), apart, as "More like this" cards are drawn.
  if (title.related.length) sections.push({kind: 'shelf', heading: 'Related', shape: 'poster',
    cards: title.related.map(r => ({ref: r.ref, title: r.label, note: [r.relation, r.note].filter(Boolean).join(' · ')}))});
  for (const ref of ['show', 'related-folder']) if (has(ref)) sections.push(block(ref));
  // Information: each row only when stated.
  const rows = [];
  const add = (label, value) => { if (value) rows.push({label, value: String(value)}); };
  add(title.director.length > 1 ? 'Directors' : 'Director', title.director.join(', '));
  add('Studio', title.studio.join(', '));
  add('Rated', rated);
  add('Released', templateDate(title.premiered));
  add('Country', title.country.join(', '));
  add('Also known as', title.aliases.filter(a => a !== title.title).slice(0, 4).join(', '));
  add('Status', title.status || title.status_stated);
  add('Episodes', title.episode_count != null ? (title.aired_count != null ? `${title.aired_count} of ${title.episode_count} aired` : String(title.episode_count)) : '');
  add('Format', title.format);
  if (title.score) add('Score', `${title.score.value.toFixed(2).replace(/0$/, '')} / 10 (${title.score.source}${title.score.votes ? `, ${title.score.votes.toLocaleString('en-US')} ratings` : ''})`);
  add('Retrieved', templateDate(title.retrieved_at));
  if (rows.length) sections.push({kind: 'information', heading: 'Information', rows});
  // One credit line: the metadata source when something on the card came from it; a video extension's episodes too.
  const informed = title.provenance.information && (title.provenance.provider || rows.length || has('cast') || has('more-like-this') || title.synopsis_from === 'information');
  const credit = [informed ? `Information from ${title.provenance.information}` : '', title.provenance.provider ? `episodes from ${title.provenance.provider}` : '',
    title.provenance.episodes ? `episode titles and pictures from ${title.provenance.episodes}` : '']
    .filter(Boolean).join(' · ');
  if (credit) sections.push({kind: 'note', text: credit.charAt(0).toUpperCase() + credit.slice(1)});
  return {line, facts, primary: play ? play.ref : null, inline: inline.map(a => a.ref),
    more: title.actions.filter(a => a !== play && !inline.includes(a)).map(a => a.ref), sections};
}

// A GROUP's presentation (2026-11-18): a film collection, a studio's credits, a topic, or a search's blended groups, in the
// same card as every title. Advanced Media curates; the core's description says only what the metadata source stated:
// - Sections in a fixed order: Collections, Studios & companies, Topics (a blended search's named subgroups, each its own
//   card: never one merged "universe"), then Movies and Shows (members, each opening its own title card).
// - Duplicates are reconciled by EXACT id and kind only: the same movie listed twice (e.g. on two pages) is one card; a
//   movie and a show with the same number are two titles. A member without ids is never merged with anything.
// - What the group is, said once: a collection as listed, production credits (not ownership or a shared story), a
//   keyword's tags (incomplete, not a canon); a release-date sort is a sorting, not a viewing order.
// - No play action: a group is not a title. Its sort (and a retry, after a failure) sit inline.
const GROUP_LINE = {franchise: 'Franchise', collection: 'Collection', company: 'Studio', topic: 'Topic', search: 'Groups'};
const GROUP_ORDER = ['franchise', 'collection', 'company', 'topic', 'movie', 'tvshow'];
const COUNTRY_NAMES = {US: 'United States', GB: 'United Kingdom', JP: 'Japan', CA: 'Canada', FR: 'France', DE: 'Germany', KR: 'South Korea', AU: 'Australia'};
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
function readDate(seconds) {
  const d = new Date(seconds * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}
export function presentGroup(title) {
  const g = title.group || {}, sections = [];
  const source = g.source || 'the metadata source';
  if (g.state === 'loading') sections.push({kind: 'note', text: 'Loading…'});
  if (g.state === 'failed') sections.push({kind: 'note', text: g.message || 'This group could not be read just now.'});
  if (g.partial) sections.push({kind: 'note', text: 'Part of this group could not be read just now.'});
  if (Number.isInteger(g.as_of) && g.as_of > 0)    // a last good answer, shown while its source is read again
    sections.push({kind: 'note', text: `As read on ${readDate(g.as_of)}; it is being read again.`});
  if (g.openable === false) sections.push({kind: 'note', text: 'Its films and series are named, but no information plugin that opens titles (such as TMDB) is on.'});
  const seen = new Map();   // media_type -> Set("ns:value")
  const duplicate = e => {
    if (e.kind !== 'title') return false;
    const pairs = Object.entries(e.ids || {}).map(([ns, value]) => `${ns}:${value}`);
    if (!pairs.length) return false;
    const set = seen.get(e.media_type) || new Set();seen.set(e.media_type, set);
    if (pairs.some(p => set.has(p))) return true;
    for (const p of pairs) set.add(p);
    return false;
  };
  let shown = 0;   // a franchise's titles actually shown (after duplicates), for its coverage note
  const ordered = [...(title.franchise?.sections || [])].sort((a, b) => GROUP_ORDER.indexOf(a.id) - GROUP_ORDER.indexOf(b.id));
  for (const s of ordered) {
    // A FRANCHISE (2026-11-18): one card for every identified adaptation, never only one continuity. Films are shelved by
    // the film series each states (in release order of its first film), then every other film as "Other films and
    // adaptations"; each card says its year, its form (animated, fan film, ...) and, when it is in no series, what it is
    // based on. Series and TV keep one shelf. Nothing is left out for being another continuity.
    if (g.kind === 'franchise' && ['movie', 'tvshow'].includes(s.id)) {
      const kept = s.items.filter(e => !duplicate(e));
      shown += kept.length;
      const note = e => [e.year ? String(e.year) : '', e.form || '', !e.series && e.basis ? `based on ${e.basis}` : ''].filter(Boolean).join(' · ');
      const card = e => ({ref: e.ref, title: e.label, note: note(e)});
      if (s.id === 'movie') {
        const series = [...new Set(kept.filter(e => e.series).map(e => e.series))];
        for (const name of series) sections.push({kind: 'shelf', heading: name, shape: 'poster', cards: kept.filter(e => e.series === name).map(card)});
        const rest = kept.filter(e => !e.series);
        if (rest.length) sections.push({kind: 'shelf', heading: series.length ? 'Other films and adaptations' : 'Films', shape: 'poster', cards: rest.map(card)});
      } else sections.push({kind: 'shelf', heading: 'Series', shape: 'poster', cards: kept.map(card)});
      continue;
    }
    const cards = s.items.filter(e => !duplicate(e)).map(e => ({ref: e.ref, title: e.label, note: e.facts}));
    if (s.more) cards.push({ref: s.more, title: 'More', note: s.total ? `${s.total} listed` : ''});
    sections.push({kind: 'shelf', heading: s.heading, shape: 'poster', cards,
      empty: s.state === 'loading' ? 'Loading…' : s.state === 'failed' ? 'This part could not be read just now.' : 'Nothing is listed.'});
  }
  const rows = [];
  if (g.kind === 'company' && g.country) rows.push({label: 'Country', value: COUNTRY_NAMES[g.country] || g.country});
  if (g.parent) rows.push({label: 'Parent company', value: g.parent});
  if (g.sort) rows.push({label: 'Sorted by', value: g.sort === 'released' ? 'Release date' : 'Popularity'});
  if (g.kind === 'franchise' && g.roots?.length) rows.push({label: 'Linked through', value: g.roots.join(', ')});
  if (rows.length) sections.push({kind: 'information', heading: 'Information', rows});
  const basis = {
    franchise: `Films and series ${source} links to this franchise's source works and film series: different adaptations and continuities, listed together, with no shared story order.`,
    collection: `A film collection as ${source} lists it.`,
    company: `Titles ${source} lists with a production credit for this company. A production credit is not ownership, distribution or a shared story.`,
    topic: `Titles ${source} tags with this keyword. Tags are incomplete and do not make a canon.`,
    search: `Groups ${source} names for “${g.query || title.title}”. Collections, studios and topics are different kinds of groups and are kept apart.`,
  }[g.kind] || '';
  const sorted = g.sort === 'released' ? 'Sorted by release date: a sorting, not a viewing order.' : '';
  sections.push({kind: 'note', text: [basis, sorted].filter(Boolean).join(' ')});
  if (g.kind === 'franchise' && Number.isInteger(g.linked))   // coverage, said: what the source links and what is shown
    sections.push({kind: 'note', text: `${source} links ${g.linked} works in all (books, games and more); the ${shown} here have a TMDB id and open as titles.`});
  const inline = title.actions.filter(a => a.group === 'inline').map(a => a.ref);
  return {line: GROUP_LINE[g.kind] || '', facts: '', primary: null, inline, more: [], sections};
}

// The EPISODE presentation (approved, 2026-11-17): the same visual system, an episode's own card. At the top (drawn by the
// card): its still or honest placeholder, its own title or "Episode N", the show's name as context, its line "S1 E2", its
// synopsis, and ONE obvious play action (Play/Resume, or Choose a source). Below, only what is the episode's: a compact
// way to its show (one card; the show's own card has its seasons, trailer and recommendations), its own Information, and
// its stated cast and guest stars, secondary. Never by default here: a season selector, the show's cast, the show's
// recommendations or information, or the show's trailer. Unstated facts stay unstated.
function presentEpisode(title, {line, rated, play, inline}) {
  const has = ref => (title.blocks || []).includes(ref);
  const facts = [title.runtime_minutes > 0 ? templateClock(title.runtime_minutes) : '', title.premiered ? templateDate(title.premiered) : '',
    rated && rated.length <= 10 ? rated : ''].filter(Boolean).join(' · ');
  const sections = [];
  if (has('series')) sections.push({kind: 'block', ref: 'series'});
  const rows = [];
  const add = (label, value) => { if (value) rows.push({label, value: String(value)}); };
  add(title.director.length > 1 ? 'Directors' : 'Director', title.director.join(', '));
  add('Aired', templateDate(title.premiered));
  add('Rated', rated);
  if (title.score) add('Score', `${title.score.value.toFixed(2).replace(/0$/, '')} / 10 (${title.score.source}${title.score.votes ? `, ${title.score.votes.toLocaleString('en-US')} ratings` : ''})`);
  if (rows.length) sections.push({kind: 'information', heading: 'Information', rows});
  if (has('cast')) sections.push({kind: 'block', ref: 'cast'});
  const credit = [title.provenance.information ? `Information from ${title.provenance.information}` : '',
    title.provenance.provider ? `episode from ${title.provenance.provider}` : ''].filter(Boolean).join(' · ');
  if (credit) sections.push({kind: 'note', text: credit.charAt(0).toUpperCase() + credit.slice(1)});
  return {line, facts, primary: play ? play.ref : null, inline: inline.map(a => a.ref),
    more: title.actions.filter(a => a !== play && !inline.includes(a)).map(a => a.ref), sections};
}
