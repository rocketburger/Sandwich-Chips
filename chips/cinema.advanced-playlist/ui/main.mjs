// Advanced Playlist, first increment: the playing video's own chapters, observed, as a list below
// the player and as marks on the timeline. Choosing a chapter moves there through Cinema's
// request-checked seek. It never seeks by itself.
//
// Uses only its `cinema` API:
//   playback.current/onEvent (the observation: request, generation, token)
//   playback.chapters (the snapshot)
//   playback.seek (absolute, source-bound)
//   player.state/capabilities/onState/expand/collapse/marks
//   slots, focus groups, shared components, timers
// No state at module level, so it can be mounted again after being turned off. Cinema removes
// everything it handed out when the module goes.

const TERMINAL = new Set(['ended', 'stopped', 'failed', 'replaced', 'closed']);
const CHANGED = new Set(['stale_request', 'stale_source', 'source_unknown', 'not_ready', 'not_a_chapter']);
const FAST = 400, SLOW = 3000, FAST_TRIES = 40, TITLE_LIMIT = 80;
// Control, zero-width, line/paragraph separator and bidirectional-override characters: never shown.
// Built from code points, so the source holds none of them.
const INVISIBLE = new RegExp('[' + [[0x0, 0x1f], [0x7f, 0x9f], [0x200b, 0x200f], [0x2028, 0x202e], [0x2066, 0x2069], [0xfeff, 0xfeff]]
  .map(([a, b]) => String.fromCodePoint(a) + '-' + String.fromCodePoint(b)).join('') + ']', 'gu');

// Safe display text for a chapter title, or null (then "Chapter N" is shown).
export function cleanTitle(value) {
  if (typeof value !== 'string') return null;
  const text = value.replace(INVISIBLE, ' ').replace(/\s+/g, ' ').trim();
  if (!text) return null;
  return text.length > TITLE_LIMIT ? text.slice(0, TITLE_LIMIT - 1).trimEnd() + '…' : text;
}
export const nameOf = (entry) => entry.title ?? (entry.interval ? `Interval ${entry.number}` : `Chapter ${entry.number}`);
// A decoded frame's own time, to the hundredth of a second (m:ss.cc). Pure.
export function preciseClock(ms) {
  const whole = clock(ms), hundredths = String(Math.floor((ms % 1000) / 10)).padStart(2, '0');
  return `${whole}.${hundredths}`;
}
// Why a point has no thumbnail, in a few plain words (the engine's refusal reason); '' when there is nothing useful to say.
const REFUSAL_WORDS = {timed_out: 'took too long', deadline: 'took too long', byte_budget: 'too much to read', child_budget: 'too much to read', packet_budget: 'too much to read', decode_budget: 'too much to read',
  coherence_unknown: 'source not confirmed', changed: 'source changed', no_frame: 'no picture there', out_of_range: 'past the end',
  connect: 'server not reached', low_speed: 'server too slow', http_error: 'server refused'};
export const refusalWords = (reason) => REFUSAL_WORDS[reason] || '';
const PREVIEW_DEBOUNCE = 150, PREVIEW_POLL = 200, PREVIEW_TRIES = 40, PREVIEW_HOLD = 1000;
// Speculative neighbours of a settled scrub target: one point NEAR_MS either side, after NEAR_SETTLE of no new target.
export const NEAR_MS = 10000, NEAR_SETTLE = 500;
export function clock(ms) {
  const s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, r = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${r}` : `${m}:${r}`;
}
const samePair = (a, b) => !!a && !!b && a.request === b.request && a.generation === b.generation && a.token === b.token;
// The observation a current() answer describes, only when it is valid. A verified local file, or an unverified source
// (a stream): for the latter the core decides whether its embedded chapters are read at all (snapshot source_class),
// and nothing chosen from them seeks (navigable, below).
export function observed(current) {
  return current && current.token && current.source_state === 'valid' && (current.source_scope === 'local_file' || current.source_scope === 'unverified')
    ? {request: current.request, generation: current.generation, token: current.token} : null;
}
// Whether choosing an entry of this snapshot may ask for the request-checked seek: a verified local file, or a
// progressive HTTP stream the core reads chapters of (it admits only this item's own embedded chapter starts there,
// re-read at the choice; the source stays unverified for everything else). Pure.
export const navigable = (snap) => snap?.source_class === 'local_file' || snap?.source_class === 'progressive_http';

// Interval points (fallback when the video has no chapters of its own): evenly spaced navigation points every N
// minutes, a setting the user chooses (stored in this module's own storage, "interval_minutes"). They are generated
// positions, never chapters: no names, scenes or analysis are claimed. Only a finite, known, non-live duration gets
// any; a point within END_GUARD of the end is left out. Large media are paged (INTERVAL_PAGE tiles at a time, never
// a list of every point), at most INTERVAL_MAX_POINTS per plan, timeline marks only up to INTERVAL_MARKS_MAX.
export const INTERVAL_CHOICES = Object.freeze([1, 2, 3, 5, 10, 15, 20, 30, 45, 60]);  // minutes
export const INTERVAL_DEFAULT = 5, INTERVAL_PAGE = 6, INTERVAL_MAX_POINTS = 1000, INTERVAL_MARKS_MAX = 100, INTERVAL_END_GUARD = 1000;
export const cleanInterval = (value) => INTERVAL_CHOICES.includes(value) ? value : INTERVAL_DEFAULT;
// The plan for one duration and setting: {count, step_ms, duration_ms} or {count: 0, reason}. Pure.
export function intervalPlan(durationMs, minutes) {
  if (!Number.isInteger(durationMs) || durationMs <= 0) return {count: 0, reason: 'no finite duration'};
  if (!INTERVAL_CHOICES.includes(minutes)) return {count: 0, reason: 'invalid interval'};
  const step = minutes * 60000, usable = durationMs - INTERVAL_END_GUARD;
  const count = usable > 0 ? Math.floor((usable - 1) / step) + 1 : 0;  // the starts k*step < usable
  if (count < 2) return {count: 0, reason: 'no point after the start'};
  if (count > INTERVAL_MAX_POINTS) return {count: 0, reason: 'too many points'};
  return {count, step_ms: step, duration_ms: durationMs};
}
// One page of points (0-based page), each {number, start_ms, interval: true}. Pure; never more than INTERVAL_PAGE.
export function intervalPage(plan, page) {
  if (!plan?.count) return [];
  const pages = Math.ceil(plan.count / INTERVAL_PAGE), p = Math.min(Math.max(0, Math.trunc(page) || 0), pages - 1), out = [];
  for (let i = p * INTERVAL_PAGE; i < Math.min(plan.count, (p + 1) * INTERVAL_PAGE); i++) out.push({number: i + 1, start_ms: i * plan.step_ms, title: null, interval: true});
  return out;
}
export const intervalPageOf = (plan, positionMs) => plan?.count && Number.isFinite(positionMs) && positionMs >= 0
  ? Math.floor(Math.min(plan.count - 1, Math.floor(positionMs / plan.step_ms)) / INTERVAL_PAGE) : 0;
// Whether this observation gets interval points instead of chapters, and what they can do: null, or {duration_ms,
// source_class, previewable, navigable}. Only when the core said definitively that there are no usable chapters (a ready
// snapshot without a chapter after the start), or that it reads none for an HLS stream; never while loading or unknown.
// navigable: a verified local file only (the core admits any request-checked target there; a stream admits only its own
// embedded chapter starts, so interval points on a stream stay preview-only). previewable: the engine's time-addressed
// frames exist for progressive HTTP and HLS VOD, and for a local file the core reports previewable (snapshot.previews). Pure.
export function intervalBasis(snap, pair, state, caps) {
  if (!snap || !pair || snap.request !== pair.request || snap.generation !== pair.generation || snap.token !== pair.token) return null;
  if (!state || state.id !== snap.request) return null;
  if (!caps || caps.kind !== 'video' || !caps.timed || caps.live === true) return null;  // live: no finite points are invented
  if (!Number.isFinite(caps.duration) || caps.duration <= 0) return null;
  let source = null, chapters = 'none';
  const known = snap.source_class === 'local_file' || snap.source_class === 'progressive_http';
  if (snap.state === 'ready' && Array.isArray(snap.chapters) && known) source = snap.source_class;
  // The core reads chapters only from Matroska and AVI (as Kodi does); any other container of a known class (an MP4)
  // answers origin_unsupported: its chapters, if any, are not read (defect closure, review of 5adb1d9).
  else if (snap.state === 'unsupported' && snap.reason === 'origin_unsupported' && known) { source = snap.source_class; chapters = 'not_read'; }
  else if (snap.state === 'unsupported' && snap.reason === 'hls_no_chapters') { source = 'hls_vod'; chapters = 'not_read'; }
  // An unverified plain local file (the core's local_preview class, 2026-11-15; for example a local MP4): its chapters
  // are not read; thumbnails only on the core's own previews fact. Navigable (0.9.9) ONLY on the core's explicit native
  // navigation-eligibility fact (interval_seek: native freshly reports this owned item as plain local disk, known length
  // and duration, no DRM, an uncorrected player clock); the core re-checks it at dispatch and natively again at the seek.
  // That fact is not file or edition identity: chapters, editions and source assets stay unverified.
  else if (snap.state === 'unsupported' && snap.reason === 'local_unverified' && snap.source_class === 'local_preview') { source = 'local_preview'; chapters = 'unverified'; }
  if (!source) return null;
  if (source === 'local_preview') return {duration_ms: Math.floor(caps.duration * 1000), source_class: source, previewable: snap.previews === true,
                                          navigable: snap.interval_seek === true && caps.seekable === true, chapters};
  // navigable: a verified local file (any request-checked target), or a progressive HTTP stream, where the core admits a
  // declared interval point (kind 'interval', its step) after re-reading the source. HLS only where the core's snapshot
  // carries its native eligibility fact (interval_seek: the normal player's repaired seek on a verified direct plain
  // MPEG-TS VOD playlist, research/HLS-INTERVAL-SEEK-REPAIR-2026-11-12.md); the module never decides that itself, and the
  // core re-checks it at the seek. Every other HLS stays preview-only.
  const hlsNavigable = source === 'hls_vod' && snap.interval_seek === true;
  // previewable: streams as before; a local file only where the core's snapshot carries its native local previewability
  // fact (previews: the player published a plain local disk path with its own length; alpha g slice 4.1). Never by name.
  const previewable = source === 'local_file' ? snap.previews === true : true;
  return {duration_ms: Math.floor(caps.duration * 1000), source_class: source, previewable,
          navigable: (source === 'local_file' || source === 'progressive_http' || hlsNavigable) && caps.seekable === true, chapters};
}
// Plain words for why an interval point does not move the video. Pure.
export function intervalRefusal(basis) {
  if (basis?.source_class === 'progressive_http') return 'Going to an interval point isn\'t available for this stream: it does not report that it can seek. This point is preview-only.';
  if (basis?.source_class === 'hls_vod') return 'Going to an interval point isn\'t available for this HLS stream. This point is preview-only.';
  if (basis?.source_class === 'local_preview') return 'Going to an interval point isn\'t available for this file right now: the player doesn\'t report it as a plain local file it can seek. This point is preview-only.';
  return 'Going to an interval point isn\'t available for this video.';
}
export function intervalNote(basis, minutes) {
  const lead = basis?.chapters === 'unverified' ? 'Cinema hasn\'t verified this file, so it doesn\'t read its chapters.'
    : basis?.chapters === 'not_read' ? 'Cinema doesn\'t read chapters from this kind of file or stream.' : 'This video has no chapters of its own.';
  const what = `${lead} These are points every ${minutes} minute${minutes === 1 ? '' : 's'}, not chapters: nothing about the scenes is known.`;
  const thumbs = basis?.previewable ? ' Each thumbnail is the frame the engine decoded at or after its point, shown with that frame\'s own time.' : ' Thumbnails for interval points aren\'t available for this file.';
  const go = basis?.source_class === 'local_preview'
    ? ' Choose a point to go there. Cinema still hasn\'t identified which file or edition this is.' : ' Choose a point to go there.';
  return what + thumbs + (basis?.navigable ? go : ' ' + intervalRefusal(basis));
}

// The chapters a snapshot makes actionable right now, or none with the reason. Pure.
export function chaptersFor(snap, pair, state, caps) {
  const no = (reason) => ({entries: [], reason});
  if (!snap || snap.state !== 'ready') return no(snap ? `${snap.state}${snap.reason ? '/' + snap.reason : ''}` : 'no snapshot');
  if (!pair || snap.request !== pair.request || snap.generation !== pair.generation || snap.token !== pair.token) return no('not the current observation');
  if (!state || state.id !== snap.request) return no('not the playing request');
  if (!caps || caps.kind !== 'video' || !caps.timed || !caps.seekable || caps.live === true) return no('not timed, seekable video');
  if (!Number.isFinite(caps.duration) || caps.duration <= 0) return no('no duration');
  const total = snap.duration_ms;
  if (!Number.isInteger(total) || total <= 0) return no('snapshot duration unknown');
  if (Math.abs(total / 1000 - caps.duration) > 1) return no('durations disagree');
  if (!Array.isArray(snap.chapters)) return no('no chapter list');
  const entries = [];
  for (const chapter of snap.chapters) {
    const start = chapter?.start_ms;
    if (!Number.isInteger(start) || start < 0 || start >= total) continue;
    if (entries.length && start <= entries.at(-1).start_ms) continue;  // in order, no two at one time
    entries.push({number: entries.length + 1, start_ms: start, title: cleanTitle(chapter.title)});
  }
  if (!entries.some(e => e.start_ms > 0)) return no(entries.length ? 'no chapter after the start' : 'no chapters');
  return {entries, reason: 'shown'};
}
// The chapter the reported position is in (index), or -1.
export function currentIndex(entries, positionSeconds) {
  if (!Number.isFinite(positionSeconds)) return -1;
  let found = -1;
  for (const [i, e] of entries.entries()) if (e.start_ms <= positionSeconds * 1000) found = i;
  return found;
}
// Plain words for a seek's outcome. `changed`: the video moved on, so the list must be refreshed.
export function outcomeText(result, name) {
  const outcome = result?.outcome;
  if (outcome === 'dispatched') return {text: `Moving to “${name}”.`, changed: false};
  if (outcome === 'refused' && CHANGED.has(result.code)) return {text: 'This video changed, so the chapter list was refreshed.', changed: true};
  if (outcome === 'uncertain') return {text: 'The player didn\'t confirm the move, so it wasn\'t repeated.', changed: false};
  if (outcome === 'cancelled' || outcome === 'superseded') return {text: 'The move was cancelled.', changed: false};
  return {text: 'That chapter can\'t be reached right now.', changed: false};
}

const INFO_EVERY = 5000;
// A user-supplied chapter set's rows (backend read "supplied_chapters", contract cinema.chapter-sets), or [].
// Display only: in order, inside the duration, safe titles; never seeks. Pure.
export function suppliedRows(value, durationMs) {
  if (value?.state !== 'matched' || value.supplier !== 'user' || value.confidence !== 'supplied' || value.accuracy !== 'unverified' ||
      value.edition?.state !== 'unknown' || !Array.isArray(value.chapters) || typeof value.provenance?.producer !== 'string') return [];
  const rows = [];
  for (const chapter of value.chapters.slice(0, 32)) {
    const start = chapter?.start_ms;
    if (!Number.isInteger(start) || start < 0 || (Number.isInteger(durationMs) && start >= durationMs) || (rows.length && start <= rows.at(-1).start_ms)) return [];
    rows.push({number: rows.length + 1, start_ms: start, title: cleanTitle(chapter.title)});
  }
  return rows;
}
export function suppliedNote(value) {
  const who = cleanTitle(value?.provenance?.producer) || 'someone', how = cleanTitle(value?.provenance?.method);
  return `You imported these chapters, from “${who}”${how ? ` (${how})` : ''}. They are bound to this exact file only; their accuracy and the edition are not checked. Choosing one moves to its supplied time.`;
}
// What one exact set is (its rows and its supplier's words): a choice applies only while the set is still this.
export const suppliedSignature = (rows, value) => JSON.stringify([rows.map(r => [r.start_ms, r.title]), value?.provenance?.producer ?? null, value?.provenance?.method ?? null]);
// Plain words for a supplied choice's seek: "dispatched" means the player was told; a later position, when the
// seek contract reports one, is only "near". The supplied time itself is never called accurate.
export function suppliedOutcome(result, name, startMs) {
  if (result?.outcome === 'dispatched') {
    const near = Number.isInteger(result.landed_ms) ? ` Now near ${clock(result.landed_ms)}.` : '';
    return `Moving to “${name}” at its supplied time, ${clock(startMs)}.${near} That time is not checked.`;
  }
  return outcomeText(result, name).text;
}
// The Information panel's header (2026-11-14 proposal, option A): how the title can be SHOWN, from the curated match's
// presentation (artwork handles, cast, genres, runtime, tagline; attributed) and its descriptive facts. Formatting only;
// every fact and every "not known" stays in the Details rows below (infoRowsFor), unchanged.
const TYPE_NAMES = {movie: 'Movie', tvshow: 'Show', episode: 'Episode'};
// Enrichment that is unavailable because no metadata plugin is ready: the manifest's CODED reason (cinema.curated-manifest
// enrichment.code, 2026-11-18, audit R11), from whichever publisher; its words are never matched.
const NO_METADATA = ['no_service', 'not_ready'];
export function presentationView(info) {
  if (!info || typeof info.title !== 'string' || !info.title) return null;
  const text = (v, n) => (typeof v === 'string' ? v.replace(INVISIBLE, ' ').replace(/\s+/g, ' ').trim().slice(0, n) : '') || null;
  const p = info.presentation && typeof info.presentation === 'object' ? info.presentation : null;
  const attr = name => (Array.isArray(info.attributes) ? info.attributes : []).find(([k]) => k === name)?.[1] ?? null;
  const listed = info.description?.origin === 'listing' ? info.description.facts || {} : {};
  const genres = (p?.genres?.length ? p.genres : Array.isArray(listed.genre) ? listed.genre : []).slice(0, 3).map(g => text(g, 40)).filter(Boolean);
  const minutes = Number.isInteger(p?.runtime_minutes) ? p.runtime_minutes
    : Number.isInteger(listed.runtime) && listed.runtime >= 60 ? Math.round(listed.runtime / 60) : null;
  const facts = [TYPE_NAMES[info.media_type] || null, Number.isInteger(info.year) ? String(info.year) : null,
                 genres.length ? genres.join(', ') : null, minutes ? `${minutes} min` : null, text(attr('rated'), 32)].filter(Boolean);
  const handle = v => (typeof v === 'string' && /^\/api\/artwork\/[A-Za-z0-9_-]{24}$/.test(v) ? v : null);
  const cast = (Array.isArray(p?.cast) ? p.cast : []).slice(0, 8).map(c => ({name: text(c?.name, 60), role: text(c?.role, 60), portrait: handle(c?.portrait)}))
    .filter(c => c.name);
  const overview = text(attr('overview'), 1000) || text(listed.overview, 1000);
  const unavailable = !p && info.enrichment?.state === 'unavailable' && NO_METADATA.includes(info.enrichment?.code);
  return {title: text(info.title, 200), facts, tagline: text(p?.tagline, 200), overview,
          picture: handle(p?.backdrop) || handle(p?.poster), pictureKind: handle(p?.backdrop) ? 'backdrop' : handle(p?.poster) ? 'poster' : null,
          cast, provider: p ? text(p.provider_label, 80) : null,
          hint: unavailable ? 'Turn on a metadata plugin in Settings → Extensions for artwork, cast and an overview.' : null};
}
const initials = name => name.split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase();

// The Information panel's rows from a curated match: known facts, then plain statements of what is
// not known. Formatting only; the facts come from the publisher. chapters: 'video' (this video's own
// chapters are listed), 'none' (it has none), 'unknown'.
export function infoRowsFor(info, {chapters = 'unknown'} = {}) {
  const text = (v, n = 200) => cleanTitle(typeof v === 'number' ? String(v) : v)?.slice(0, n) ?? null;
  const rows = [['Title', text(info.title)]];
  if (Number.isInteger(info.year)) rows.push(['Year', String(info.year)]);
  if (typeof info.media_type === 'string' && info.media_type !== 'unknown') rows.push(['Type', text(info.media_type, 40)]);
  for (const [name, value] of (Array.isArray(info.attributes) ? info.attributes : []).slice(0, 6)) {
    const label = text(name, 40), shown = text(value);
    if (label && shown) rows.push([label.charAt(0).toUpperCase() + label.slice(1), shown]);
  }
  // Where the descriptive details above came from (automatic enrichment), or why there are none.
  const details = info.enrichment;
  if (details?.state === 'available' && text(details.provider_label, 80)) rows.push(['Details', `From ${text(details.provider_label, 80)}`]);
  else if (details?.state === 'pending') rows.push(['Details', 'Being looked up']);
  else if (details?.state === 'unavailable') rows.push(['Details', text(details.reason, 120) || 'Not available']);
  for (const row of descriptionRows(info)) rows.push(row);
  for (const marker of markerRows(info)) rows.push(marker);
  for (const candidate of candidateRows(info)) rows.push(candidate);
  for (const edition of editionRows(info)) rows.push(edition);
  rows.push(['Edition', info.edition?.state === 'stated' && text(info.edition.label) ? text(info.edition.label) : 'Not known']);
  rows.push(['Release', info.sources === 'observed' ? 'Identified' : 'Not identified']);
  rows.push(['Chapters', chapters === 'video' ? 'From the video itself' : chapters === 'none' ? 'None in this video' : 'Not available']);
  for (const link of associationRows(info)) rows.push(link);
  return rows.filter(([, value]) => value);
}

// A listing's descriptive facts (Advanced Media's "description": only fields nothing better states), as plain
// attributed rows: the listing's words as it stated them, never checked, never an identity, a match or a seek.
// A runtime is the listing's statement, not this video's duration.
export const OVERVIEW_LIMIT = 1000;
export function descriptionRows(info) {
  const d = info?.description;
  if (!d || d.origin !== 'listing' || !d.facts || typeof d.facts !== 'object') return [];
  const clean = (v, n) => (typeof v === 'string' ? v.replace(INVISIBLE, ' ').replace(/\s+/g, ' ').trim().slice(0, n) : '') || null;
  const who = clean(d.producer_label, 80);
  if (!who) return [];
  const rows = [];
  const overview = clean(d.facts.overview, OVERVIEW_LIMIT);
  if (overview) rows.push(['Overview', overview]);
  const genres = (Array.isArray(d.facts.genre) ? d.facts.genre : []).slice(0, 8).map(g => clean(g, 40)).filter(Boolean);
  if (genres.length) rows.push(['Genre', genres.join(', ')]);
  const s = d.facts.runtime;
  if (Number.isInteger(s) && s > 0 && s <= 86400) rows.push(['Runtime', s < 60 ? `${s} s (as listed)` : `${Math.round(s / 60)} min (as listed)`]);
  if (!rows.length) return [];
  const version = clean(d.version, 32), shortened = Array.isArray(d.truncated) && d.truncated.length;
  rows.push(['Description', `From the ${who} listing${version ? ` (version ${version})` : ''}; not checked${shortened ? '; shortened' : ''}`]);
  return rows;
}

// Title-level marker candidates (Advanced Media's "markers": binding "title", timeline "unverified"), as attributed
// information rows only: never timeline marks, seek buttons or skip prompts. Times as the provider gave them; a
// missing start is "the start", a missing end "the end". The provider's own confidence stays the provider's.
export const MARKER_LABELS = {intro: 'Intro', recap: 'Recap', credits: 'Credits', preview: 'Preview'};
export function markerRows(info) {
  const m = info?.markers;
  if (!m || m.binding !== 'title' || m.timeline !== 'unverified') return [];
  const text = (v, n) => cleanTitle(typeof v === 'string' ? v : null)?.slice(0, n) ?? null;
  if (m.state === 'pending') return [['Markers', 'Being looked up']];
  if (m.state !== 'available') return m.state === 'unavailable' && text(m.reason, 160) ? [['Markers', text(m.reason, 160)]] : [];
  const label = text(m.provider_label, 80);
  if (!label) return [];
  const rows = [];
  for (const item of (Array.isArray(m.items) ? m.items : []).slice(0, 8)) {
    const name = MARKER_LABELS[item?.kind];
    const ok = (v) => v === null || (Number.isInteger(v) && v >= 0);
    if (!name || !ok(item.start_ms) || !ok(item.end_ms) || (item.start_ms === null && item.end_ms === null)) continue;
    const from = item.start_ms === null ? 'the start' : clock(item.start_ms), to = item.end_ms === null ? 'the end' : clock(item.end_ms);
    const extra = [Number.isFinite(item.provider_confidence) ? `provider confidence ${Math.round(item.provider_confidence * 100)}%` : null,
      Number.isInteger(item.submissions) ? `${item.submissions} submission${item.submissions === 1 ? '' : 's'}` : null].filter(Boolean);
    rows.push([name, `${from} – ${to}${extra.length ? ` (${extra.join(', ')})` : ''}`]);
  }
  if (!rows.length) return [];
  const measured = Number.isInteger(m.measured_ms) && m.measured_ms > 0 ? `, measured on a ${clock(m.measured_ms)} release` : '';
  rows.push(['Markers', `From ${label}${measured}; not verified for this video`]);
  return rows;
}

// Title-level conventional chapter SET candidates (Advanced Media's "chapter_candidates": binding "title", timeline
// "unverified"). Each set is shown on its own, with its provider, note and votes as the provider's provenance, and
// its chapters as "time  name" text rows. Never merged with another set or the video's own chapters, never a seek,
// mark or skip, and never labelled as this video's chapters.
export const CANDIDATE_ROWS = 40;
export function candidateRows(info) {
  const m = info?.chapter_candidates;
  if (!m || m.binding !== 'title' || m.timeline !== 'unverified') return [];
  const text = (v, n) => cleanTitle(typeof v === 'string' ? v : null)?.slice(0, n) ?? null;
  if (m.state === 'pending') return [['Candidate chapters', 'Being looked up']];
  if (m.state !== 'available') return m.state === 'unavailable' && text(m.reason, 160) ? [['Candidate chapters', text(m.reason, 160)]] : [];
  const label = text(m.provider_label, 80), sets = Array.isArray(m.sets) ? m.sets : [];
  if (!label || !sets.length) return [];
  const rows = [];
  sets.forEach((set, index) => {
    const chapters = (Array.isArray(set?.chapters) ? set.chapters : []).filter(c => Number.isInteger(c?.start_ms) && c.start_ms >= 0);
    if (!chapters.length) return;
    const note = text(set.note, 60), votes = set.votes && Number.isInteger(set.votes.up) && Number.isInteger(set.votes.down) ? `${set.votes.up} up, ${set.votes.down} down` : null;
    const about = [`Set ${index + 1} of ${sets.length} from ${label}`, note, votes].filter(Boolean).join(' · ');
    rows.push(['Candidate chapters', `${about}; release not verified for this video`]);
    for (const c of chapters.slice(0, CANDIDATE_ROWS)) rows.push([clock(c.start_ms), text(c.title, 80) || 'Untitled']);
    if (chapters.length > CANDIDATE_ROWS) rows.push(['…', `${chapters.length - CANDIDATE_ROWS} more in this set`]);
  });
  return rows;
}

// Title-level EDITION candidates (Advanced Media's "edition_candidates": binding "title", timeline "unverified"):
// each candidate on its own with what the provider states (version name only if supplied, duration, exact frame rate),
// then its timed annotations as "time | concept" text rows (an unresolved concept says so). Never the video's edition,
// release or timeline; never a seek, mark or skip; no refs or fingerprints.
export const EDITION_POINT_ROWS = 24;
export function frameRate(text) {
  const m = /^([1-9][0-9]{0,6})\/([1-9][0-9]{0,6})$/.exec(typeof text === 'string' ? text : '');
  if (!m) return null;
  const fps = Number(m[1]) / Number(m[2]);
  const shown = (Math.round(fps * 1000) / 1000).toString();
  return m[2] === '1' ? `${shown} fps` : `${shown} fps (${m[1]}/${m[2]})`;
}
export function editionRows(info) {
  const m = info?.edition_candidates;
  if (!m || m.binding !== 'title' || m.timeline !== 'unverified') return [];
  const text = (v, n) => cleanTitle(typeof v === 'string' ? v : null)?.slice(0, n) ?? null;
  if (m.state === 'pending') return [['Edition candidates', 'Being looked up']];
  if (m.state !== 'available') return m.state === 'unavailable' && text(m.reason, 160) ? [['Edition candidates', text(m.reason, 160)]] : [];
  const label = text(m.provider_label, 80), editions = Array.isArray(m.editions) ? m.editions : [];
  if (!label || !editions.length) return [];
  const rows = [];
  editions.forEach((e, index) => {
    const facts = [text(e?.version_label, 60), Number.isInteger(e?.duration_ms) && e.duration_ms > 0 ? clock(e.duration_ms) : null, frameRate(e?.frame_rate)].filter(Boolean);
    rows.push(['Edition candidate', `${index + 1} of ${editions.length} from ${label}${facts.length ? `: ${facts.join(' · ')}` : ''}; not verified for this video`]);
    const points = (Array.isArray(e?.points) ? e.points : []).filter(p => Number.isInteger(p?.at_ms) && p.at_ms >= 0);
    for (const p of points.slice(0, EDITION_POINT_ROWS)) rows.push([clock(p.at_ms), text(p.concept_label, 80) || 'Unknown concept']);
    if (points.length > EDITION_POINT_ROWS) rows.push(['…', `${points.length - EDITION_POINT_ROWS} more annotations`]);
  });
  if (m.complete === false) rows.push(['Edition candidates', 'The provider lists more than was read.']);
  return rows;
}

// A user-confirmed association, as attributed information only: who confirmed it, what it names and where
// that came from, and that edition, release and timeline are not known. Nothing here plays, seeks or previews.
export function associationRows(info) {
  const text = (v, n = 200) => cleanTitle(typeof v === 'number' ? String(v) : v)?.slice(0, n) ?? null;
  const rows = [];
  for (const link of (Array.isArray(info?.associations) ? info.associations : []).slice(0, 2)) {
    if (link?.confirmed !== 'user' || link.kind !== 'discovery_link' || !text(link.title)) continue;
    const kind = typeof link.media_type === 'string' && link.media_type !== 'unknown' ? text(link.media_type, 40) : null;
    const named = [text(link.title), Number.isInteger(link.year) ? String(link.year) : null, kind].filter(Boolean).join(' · ');
    const from = [text(link.source_label, 80), text(link.provider_label, 80)].filter(Boolean);  // as Advanced Media says it: source via service
    rows.push(['Linked by you', from.length ? `${named}, from ${from.join(' via ')}` : named]);
    const unknown = ['edition', 'release', 'timeline'].every(k => link[k] === 'unknown');
    rows.push(['Link', unknown ? 'Edition, release and timeline not known' : 'Not checked']);
  }
  return rows;
}

export function mount(cinema) {
  // This module's player parts are SEMANTIC surfaces (2026-11-17, review of bh): it says what each holds (its entry points,
  // the chapter or interval list, supplied chapters, the information); the active skin draws them in the player's slot;
  // the player expands a panel. It draws nothing itself. (An older Cinema without player surfaces: nothing is shown.)
  if (typeof cinema.surfaces?.player !== 'function') return;
  const ui = {status: '', supplied: {say: '', busy: false}, info: {more: false, details: false, head: null}};
  const strip = cinema.surfaces.player('player.below', {sections: []}, {group: true, hidden: true,
    onAction: ({id}) => id === 'opener' ? open() : id === 'supplied-opener' ? openSupplied() : id === 'info-opener' ? openInfo() : null});
  const panel = cinema.surfaces.player('player.below', {title: 'Chapters', sections: []}, {hidden: true, onAction: p => panelAction(p)});
  let minutes = INTERVAL_DEFAULT;  // this module's own setting (storage "interval_minutes"), the default until read
  // Interval thumbnails (lazy LOD): frames only for the points on the visible page, held for that page's plan only
  // (key: observation, duration, setting, page). One job at a time, through the same one preview read as hover.
  let tiles = {key: null, frames: new Map(), refused: new Map(), job: null}, tileTimer = null;

  let pair = null;            // the latest observation this module knows of
  let shown = null;           // what the list and marks were made from
  let epoch = 0;              // moves whenever what is shown is cleared or replaced
  let busy = false, again = false, timer = null, tries = 0, triesFor = null, seeking = false, isOpen = false;
  const say = (text) => { ui.status = text;drawPanel(); };
  // Fast retries for one thing that is still coming, slowing after FAST_TRIES.
  const fast = (key) => { if (triesFor !== key) { triesFor = key; tries = 0; } return ++tries > FAST_TRIES ? SLOW : FAST; };

  const close = () => { if (isOpen) { isOpen = false; cinema.player.collapse(); } panel.hidden = true; };
  // Supplied images for this observation's exact bytes (this module's backend read "assets"): chapter
  // thumbnails and timeline frames. Display only: a frame is shown on pointer hover, never seeks.
  let assets = null, assetsBusy = false, assetsTimer = null, assetsFor = null;
  const ASSET = /^\/x\/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\/ui\/(?:[A-Za-z0-9_-]{1,64}\/){0,4}[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}\.(?:png|webp)$/;
  // Also a frame the engine extracted from this observation (cinema.source-assets "extracted"): an in-memory route.
  const FRAME = /^\/x\/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\/frames\/[0-9a-f]{24}\.(?:jpg|png)$/;
  const safe = (v) => typeof v === 'string' && !v.includes('..') && !v.includes('//') && (ASSET.test(v) || FRAME.test(v)) ? v : null;
  const applyThumbs = () => drawPanel();   // thumbnails and decoded-frame times are part of the panel's drawing
  const dropAssets = () => {
    assets = null;assetsFor = null;
    if (assetsTimer !== null) { cinema.timers.clear(assetsTimer); assetsTimer = null; }
    hidePackaged();
    applyThumbs();
  };
  const scheduleAssets = (ms) => {
    if (assetsTimer !== null) cinema.timers.clear(assetsTimer);
    try { assetsTimer = cinema.timers.setTimeout(() => { assetsTimer = null; requestAssets(); }, ms); }
    catch { assetsTimer = null; }
  };
  async function requestAssets() {
    if (!reads || !shown || assetsBusy) return;
    const target = shown, mine = epoch;
    assetsBusy = true;
    try {
      const answer = await reads.read('assets', {request: target.pair.request, generation: target.pair.generation, token: target.pair.token,
                                                 duration_ms: target.snap.duration_ms, chapters: target.entries.map(e => e.start_ms)});
      if (epoch !== mine || shown !== target) return;  // for an earlier observation: nothing applies
      const v = answer?.ok ? answer.value : null;
      if (v?.state === 'matched' && v.request === target.pair.request && v.generation === target.pair.generation && v.token === target.pair.token) {
        const chapters = (v.chapters || []).filter(c => Number.isInteger(c?.start_ms) && safe(c.image) && target.entries.some(e => e.start_ms === c.start_ms));
        const frames = (v.frames || []).filter(f => Number.isInteger(f?.at_ms) && safe(f.image)).sort((a, b) => a.at_ms - b.at_ms);
        assets = {chapters, frames};assetsFor = target;
      } else { assets = null;assetsFor = null;hidePackaged(); }
      applyThumbs();
    } catch (error) {
      if (String(error?.message || error).includes('unloaded')) return;
      if (epoch === mine) dropAssets();
    } finally {
      assetsBusy = false;
      if (epoch === mine && shown === target) scheduleAssets(INFO_EVERY);  // a publisher may come or go
    }
  }
  // Time-addressed previews (backend read "preview", published by Advanced Media for a progressive stream): the
  // frame the engine decoded at or after the hovered second, labelled with its own time. Hover is debounced, only
  // the latest hovered time is asked about, one read is out at a time, and an answer is shown only while it is for
  // the time still hovered on the same observation. Leaving the timeline, another observation, the view closing or
  // this module unloading clears it. Packaged timeline frames (supplied sets) keep their own behaviour first.
  let hoverAt = null, previewEpoch = 0, previewTimer = null, previewBusy = false, lastPreview = null, packagedShown = false;
  // hoverSettled: the hovered span's own read has ended (a frame, or nothing to show): only then may lower-priority frame
  // work (visible interval tiles, then neighbours) use the one preview read. The engine keeps one preview per module and
  // observation in flight and a newer one replaces it, so asking anything else while the target is coming would cancel it.
  let hoverSettled = true;
  // Neighbours of a settled target (speculative, lowest priority): at most NEAR_COUNT points NEAR_MS either side, asked
  // once each, one at a time, only after the target's frame came and the pointer stayed NEAR_SETTLE; a new target drops
  // them (the engine's own replacement cancels one of them still in flight: only this module's own demand).
  const near = {target: null, done: new Set(), job: null, since: 0};
  // TEST diagnostics (hover residual): only when a driver set globalThis.__cinemaHoverTrace; bounded, path-free.
  const htrace = (e) => { const T = globalThis.__cinemaHoverTrace; if (Array.isArray(T) && T.length < 2000) T.push({t: Math.round(globalThis.performance?.now?.() ?? 0), o: 'ap', ...e}); };
  // Packaged-asset reads hide only a packaged preview, never a time-addressed one they know nothing of.
  function hidePackaged() { if (packagedShown) { packagedShown = false; cinema.player.hidePreview(); } }
  const clearPreviewTimer = () => { if (previewTimer !== null) { cinema.timers.clear(previewTimer); previewTimer = null; } };
  const dropPreview = () => { previewEpoch += 1; hoverAt = null; lastPreview = null; packagedShown = false; hoverSettled = true; near.target = null; near.job = null; clearPreviewTimer(); cinema.player.hidePreview(); kickFrames(0); };
  const schedulePreview = (fn, ms) => { clearPreviewTimer(); try { previewTimer = cinema.timers.setTimeout(() => { previewTimer = null; fn(); }, ms); } catch (error) { previewTimer = null; htrace({k: 'ap-schedule-failed', ms, unloaded: String(error?.message || error).includes('unloaded')}); } };
  const showPreview = (p) => cinema.player.preview({at: hoverAt, label: `Frame at ${preciseClock(p.at_ms)}`, image: p.image});
  async function requestPreview(mine, tries = 0) {
    if (mine !== previewEpoch || hoverAt === null || !pair || !reads) { htrace({k: 'ap-request-gated', stale: mine !== previewEpoch, hover: hoverAt !== null, pair: !!pair, reads: !!reads, tries}); return; }
    if (previewBusy) { htrace({k: 'ap-request-busy', tries}); schedulePreview(() => requestPreview(mine, tries), PREVIEW_POLL); return; }  // one read out at a time
    const target = pair, atMs = Math.floor(hoverAt * 1000), caps = cinema.player.capabilities();
    const durationMs = Number.isFinite(caps?.duration) && caps.duration > 0 ? Math.floor(caps.duration * 1000) : null;
    if (durationMs === null || atMs >= durationMs) { htrace({k: 'ap-request-no-duration', duration: durationMs !== null, at: atMs}); cinema.player.hidePreview(); hoverSettled = true; kickFrames(0); return; }
    previewBusy = true;
    htrace({k: 'ap-read', at: atMs, tries});
    let again = false, hold = false;
    try {
      const answer = await reads.read('preview', {request: target.request, generation: target.generation, token: target.token, duration_ms: durationMs, at_ms: atMs});
      if (mine !== previewEpoch || !samePair(target, pair)) { htrace({k: 'ap-answer-dropped', stale: mine !== previewEpoch}); return; }  // a newer hover, or another observation: not shown
      const v = answer?.ok ? answer.value : null;
      const mineNow = v && samePair(v, target) && Array.isArray(v.covers) && v.covers[0] <= atMs && atMs < v.covers[1];
      if (mineNow && v.state === 'ready' && FRAME.test(v.image || '') && Number.isInteger(v.at_ms)) {
        lastPreview = {pair: target, covers: v.covers, image: v.image, at_ms: v.at_ms};
        showPreview(lastPreview);
        hold = true;  // asked again while it stays shown: the service or its frame going hides it within PREVIEW_HOLD
      } else if (mineNow && v.state === 'pending' && tries < PREVIEW_TRIES) again = true;
      else { lastPreview = null; cinema.player.hidePreview(); }  // unavailable (no supported evidence, a local file, the service gone...)
    } catch (error) {
      if (String(error?.message || error).includes('unloaded')) return;
      if (mine === previewEpoch) cinema.player.hidePreview();
    } finally {
      previewBusy = false;
      htrace({k: 'ap-read-settled', again, hold});
      if (again && mine === previewEpoch) schedulePreview(() => requestPreview(mine, tries + 1), PREVIEW_POLL);
      else if (hold && mine === previewEpoch) schedulePreview(() => requestPreview(mine, 0), PREVIEW_HOLD);
      if (mine === previewEpoch && !again) {
        // The target's own read ended: tiles may go now; neighbours only once the pointer has stayed (NEAR_SETTLE).
        if (!hoverSettled && lastPreview) { near.target = lastPreview.covers[0]; near.done = new Set([lastPreview.covers[0]]); near.since = previewEpoch; }
        hoverSettled = true;
        kickFrames(lastPreview ? NEAR_SETTLE : 0);
      }
    }
  }
  const scrubbed = (at) => {
    if (!Number.isFinite(at)) { htrace({k: 'ap-scrub-leave'}); dropPreview(); return; }  // the pointer left the timeline
    if (assets?.frames?.length && assetsFor === shown) {
      htrace({k: 'ap-scrub-packaged'});
      const frame = [...assets.frames].reverse().find(f => f.at_ms <= at * 1000);
      if (!frame) { hidePackaged(); return; }
      packagedShown = true;
      cinema.player.preview({at, label: clock(Math.round(at * 1000)), image: frame.image});
      return;
    }
    if (!pair || !reads) { htrace({k: 'ap-scrub-gated', pair: !!pair, reads: !!reads}); cinema.player.hidePreview(); return; }
    hoverAt = at;
    previewEpoch += 1;  // any answer still out is for an earlier hover
    const atMs = Math.floor(at * 1000);
    const mine = previewEpoch;
    if (lastPreview && samePair(lastPreview.pair, pair) && lastPreview.covers[0] <= atMs && atMs < lastPreview.covers[1]) {
      htrace({k: 'ap-scrub-same-span', at: atMs});
      showPreview(lastPreview);  // the same span: the same frame, checked again within PREVIEW_HOLD
      schedulePreview(() => requestPreview(mine), PREVIEW_HOLD);
      return;
    }
    htrace({k: 'ap-scrub-schedule', at: atMs, busy: previewBusy});
    hoverSettled = false; near.target = null; near.job = null;  // a new target: it goes first; the old neighbours are obsolete
    cinema.player.hidePreview();  // never another time's frame while the right one is coming
    schedulePreview(() => requestPreview(mine), PREVIEW_DEBOUNCE);
  };
  if (cinema.player.onScrub) cinema.player.onScrub(scrubbed);

  // Lower-priority frame work, after the scrub target: first the current interval page (in page order),
  // then a settled target's neighbours. One job at a time and only while no preview read is out and the target is
  // settled, so it never replaces the target in the engine's one preview slot. Each answer is used only while it is
  // still the job wanted (same observation, page, setting, target); a tile's frame carries its own decoded time.
  function kickFrames(ms) {
    if (tileTimer !== null) { cinema.timers.clear(tileTimer); tileTimer = null; }
    try { tileTimer = cinema.timers.setTimeout(() => { tileTimer = null; frameStep(); }, ms); } catch { tileTimer = null; }
  }
  function nextFrameJob(durationMs) {
    if (shown?.mode === 'intervals' && shown.basis.previewable && samePair(shown.pair, pair) && tiles.key === shown.key) {
      if (tiles.job) return tiles.job;
      const at = shown.entries.map(e => e.start_ms).find(s => s < durationMs && !tiles.frames.has(s) && !tiles.refused.has(s));
      if (at !== undefined) return (tiles.job = {kind: 'tile', at, tries: 0, key: tiles.key});
    }
    if (hoverAt !== null && hoverSettled && near.target !== null) {
      if (near.job) return near.job;
      for (const at of [near.target - NEAR_MS, near.target + NEAR_MS]) {
        if (at >= 0 && at < durationMs && !near.done.has(at)) { near.done.add(at); return (near.job = {kind: 'near', at, tries: 0}); }
      }
    }
    return null;
  }
  async function frameStep() {
    if (previewBusy) { kickFrames(PREVIEW_POLL); return; }
    if (hoverAt !== null && !hoverSettled) return;  // the target's own read kicks this again when it ends
    const caps = cinema.player.capabilities();
    const durationMs = Number.isFinite(caps?.duration) && caps.duration > 0 ? Math.floor(caps.duration * 1000) : null;
    if (!pair || !reads || durationMs === null) return;
    const job = nextFrameJob(durationMs);
    if (!job) return;
    const target = pair, wanted = () => samePair(target, pair) && (job.kind === 'tile' ? tiles.job === job && tiles.key === job.key : near.job === job);
    const done = (reason) => {
      if (job.kind === 'tile' && tiles.job === job) { if (reason) { tiles.refused.set(job.at, reason); applyThumbs(); } tiles.job = null; }
      else if (near.job === job) near.job = null;
    };
    previewBusy = true;
    let next = null;
    try {
      const answer = await reads.read('preview', {request: target.request, generation: target.generation, token: target.token, duration_ms: durationMs, at_ms: job.at});
      if (!wanted()) { next = 0; return; }  // the page, setting, target or observation moved on: the next job decides
      const v = answer?.ok ? answer.value : null;
      const mineNow = v && samePair(v, target) && Array.isArray(v.covers) && v.covers[0] <= job.at && job.at < v.covers[1];
      if (mineNow && v.state === 'ready' && FRAME.test(v.image || '') && Number.isInteger(v.at_ms)) {
        if (job.kind === 'tile') { tiles.frames.set(job.at, {image: v.image, at_ms: v.at_ms}); applyThumbs(); }
        done(null);next = 0;
      } else if (mineNow && v.state === 'pending' && job.tries < PREVIEW_TRIES) { job.tries += 1; next = PREVIEW_POLL; }
      else { done(v?.reason || v?.state || 'unavailable'); next = 0; }  // not asked again for this page and setting
    } catch (error) {
      if (String(error?.message || error).includes('unloaded')) return;
      done('failed');next = 0;
    } finally {
      previewBusy = false;
      if (next !== null) kickFrames(next);
    }
  }

  // Interval points: the plan for this observation's duration and the setting, one page at a time.
  function showIntervals(basis, snap, caps, page = null) {
    const plan = intervalPlan(basis.duration_ms, minutes);
    if (!plan.count && plan.reason !== 'too many points') { clear(); return; }  // a short video: nothing to page through
    if (shown && (!samePair(shown.pair, pair) || shown.mode !== 'intervals')) clear();
    const signature = `intervals|${basis.source_class}|${basis.navigable}|${basis.duration_ms}|${minutes}`;
    const same = shown?.signature === signature;
    if (same && page === null) { Object.assign(shown, {snap, duration: caps.duration}); updateStrip(); markCurrent(); return; }
    const pages = plan.count ? Math.ceil(plan.count / INTERVAL_PAGE) : 0;
    const at = same ? page : intervalPageOf(plan, (cinema.player.state()?.position ?? 0) * 1000);
    const p = pages ? Math.min(Math.max(0, at), pages - 1) : 0;
    const entries = intervalPage(plan, p);
    const key = `${pair.request}|${pair.generation}|${pair.token}|${signature}|${p}`;
    if (!same) cinema.player.marks(plan.count && plan.count <= INTERVAL_MARKS_MAX ? Array.from({length: plan.count - 1}, (_, i) => ({at: (i + 1) * plan.step_ms / 1000, kind: 'interval'})) : []);
    shown = {mode: 'intervals', pair, signature, entries, snap, duration: caps.duration, basis, plan, page: p, pages, key};
    if (tiles.key !== key) tiles = {key, frames: new Map(), refused: new Map(), job: null};  // another page or plan: its own frames only
    render(entries);renderExtra();updateStrip();markCurrent();applyThumbs();
    kickFrames(0);
  }
  function renderExtra() { drawPanel(); }
  function turnPage(by) {
    if (shown?.mode !== 'intervals') return;
    showIntervals(shown.basis, shown.snap, cinema.player.capabilities() || {duration: shown.duration}, shown.page + by);
  }
  function changeInterval(by) {
    if (shown?.mode !== 'intervals') return;
    const next = INTERVAL_CHOICES[INTERVAL_CHOICES.indexOf(minutes) + by];
    if (!next) return;
    minutes = next;chosen += 1;
    try { Promise.resolve(cinema.storage?.set?.('interval_minutes', minutes)).catch(() => {}); } catch { /* unloaded or no storage: this playback only */ }
    showIntervals(shown.basis, shown.snap, cinema.player.capabilities() || {duration: shown.duration});
    say(`Interval points every ${minutes} minute${minutes === 1 ? '' : 's'}.`);
  }
  // The stored setting (no storage capability: the default, for this session only). Read at mount and again each time
  // the Chapters panel opens, so a change made in Settings › Extensions applies the next time Chapters opens (2026-11-14).
  // Only the latest read applies, and never over a choice made here after it started (stale reads cannot win).
  let chosen = 0, intervalReads = 0;
  function readInterval() {
    try {
      if (!cinema.storage?.get) return;
      const ticket = ++intervalReads, at = chosen;
      Promise.resolve(cinema.storage.get()).then((values) => {
        if (ticket !== intervalReads || at !== chosen) return;
        const stored = cleanInterval(values?.interval_minutes);
        if (stored === minutes) return;
        minutes = stored;
        if (shown?.mode === 'intervals') showIntervals(shown.basis, shown.snap, cinema.player.capabilities() || {duration: shown.duration});
      }).catch(() => {});
    } catch { /* unloaded */ }
  }
  readInterval();

  const clear = () => {
    epoch += 1;
    if (assets || assetsFor) dropAssets();
    if (!shown) return;
    shown = null;seeking = false;
    tiles = {key: null, frames: new Map(), refused: new Map(), job: null};  // interval frames belong to what was shown
    cinema.player.marks([]);
    close();
    ui.status = '';updateStrip();
  };
  const schedule = (ms) => {
    if (timer !== null) cinema.timers.clear(timer);
    try { timer = cinema.timers.setTimeout(() => { timer = null; refresh(); }, ms); }
    catch { timer = null; }  // unloaded: Cinema has already cleared everything this module had
  };
  const adopt = (next) => {
    // Another observation (even of the same file) clears everything before anything is asked again.
    if (!samePair(next, pair)) {
      pair = next; dropPreview();  // a preview belongs to the observation it was asked for
      if (shown && !samePair(shown.pair, next)) clear();
      dropSupplied(); if (next) scheduleSupplied(0);
    }
  };
  // Supplied chapters: a set the user imported (Advanced Media), for this observation's exact bytes only. Listed
  // with its supplier and limits. Choosing one is the user's explicit choice of that supplied time (its accuracy
  // unverified): the observation and the set are read again first, and only the same set still holding that entry
  // sends the request-checked seek. Asked again slowly (a set may be removed or its publisher go); any other
  // observation drops it first. The video's own chapters are independent of all of this.
  const suppliedPanel = cinema.surfaces.player('player.below', {title: 'Supplied chapters', sections: []}, {hidden: true, onAction: ({id, item}) => {
    const row = id === 'rows' && item ? supplied?.rows.find(r => 'n' + r.number === item) : null;if (row) chooseSupplied(row);
  }});
  let supplied = null, suppliedEpoch = 0, suppliedBusy = false, suppliedTimer = null, suppliedOpen = false, suppliedChoosing = false, drawnFor = null;
  function showSupplied() {
    const items = supplied ? [{kind: 'list', id: 'rows', label: 'Supplied chapters', items: supplied.rows.map(row => ({id: 'n' + row.number, badge: String(row.number),
        title: nameOf(row), subtitle: clock(row.start_ms)}))}, {kind: 'text', text: suppliedNote(supplied.value), tone: 'muted'}]
      : [{kind: 'text', text: 'Supplied chapters aren\'t available for this file right now.', tone: 'muted'}];
    suppliedPanel.update({title: 'Supplied chapters', status: ui.supplied.busy || ui.supplied.say ? {state: ui.supplied.busy ? 'pending' : 'ok', text: ui.supplied.say} : null,
      sections: [{items}]});
  }
  const saySupplied = (text) => { ui.supplied.say = text;showSupplied(); };
  async function chooseSupplied(row) {
    if (!supplied || suppliedChoosing || !reads) return;
    const target = supplied, mine = suppliedEpoch;
    // Late work is void once this observation, this exact set or this panel is no longer what the user chose from.
    const stillThis = () => mine === suppliedEpoch && supplied?.signature === target.signature && samePair(target.pair, pair) && suppliedOpen;
    const name = nameOf(row);
    suppliedChoosing = true;ui.supplied.busy = true;saySupplied(`Checking “${name}”…`);
    try {
      const now = observed(await cinema.playback.current());
      if (!stillThis()) return;
      const state = cinema.player.state(), caps = cinema.player.capabilities();
      if (!samePair(now, target.pair) || state?.id !== target.pair.request || !caps || caps.kind !== 'video' || !caps.seekable || !Number.isFinite(caps.duration)) {
        saySupplied('This video changed, so nothing was sent.');dropSupplied();scheduleSupplied(0);return;
      }
      const fresh = await reads.read('supplied_chapters', {request: target.pair.request, generation: target.pair.generation, token: target.pair.token, duration_ms: target.durationMs});
      if (!stillThis()) return;
      const v = fresh?.ok ? fresh.value : null;
      const same = v?.request === target.pair.request && v?.generation === target.pair.generation && v?.token === target.pair.token;
      const rows = same ? suppliedRows(v, target.durationMs) : [];
      const signature = rows.length ? suppliedSignature(rows, v) : null;
      if (signature !== target.signature || !rows.some(r => r.start_ms === row.start_ms && r.title === row.title)) {
        supplied = signature ? {rows, value: v, pair: target.pair, durationMs: target.durationMs, signature} : null;
        updateStrip();showSupplied();
        saySupplied(supplied ? 'These supplied chapters changed, so nothing was sent. Choose again.' : 'These supplied chapters are no longer available, so nothing was sent.');
        return;
      }
      const result = await cinema.playback.seek({request: target.pair.request, generation: target.pair.generation, to_ms: row.start_ms});
      if (!stillThis()) return;  // a late answer after a removal, replacement, unload or close: nothing is said
      saySupplied(suppliedOutcome(result, name, row.start_ms));
      if (outcomeText(result, name).changed) { dropSupplied(); scheduleSupplied(0); }  // the video moved on: asked again
    } catch (error) {
      if (String(error?.message || error).includes('unloaded') || !stillThis()) return;
      saySupplied(outcomeText(null).text);
    } finally {
      suppliedChoosing = false;ui.supplied.busy = false;showSupplied();
    }
  }
  function dropSupplied() {
    suppliedEpoch += 1;supplied = null;
    if (suppliedTimer !== null) { cinema.timers.clear(suppliedTimer); suppliedTimer = null; }
    if (suppliedOpen && !pair) { suppliedOpen = false; cinema.player.collapse(); suppliedPanel.hidden = true; }
    else if (suppliedOpen) showSupplied();
    updateStrip();
  }
  function scheduleSupplied(ms) {
    if (!reads) return;
    if (suppliedTimer !== null) cinema.timers.clear(suppliedTimer);
    try { suppliedTimer = cinema.timers.setTimeout(() => { suppliedTimer = null; requestSupplied(); }, ms); }
    catch { suppliedTimer = null; }
  }
  async function requestSupplied() {
    if (suppliedBusy || !pair) return;
    const target = pair, mine = suppliedEpoch, state = cinema.player.state(), caps = cinema.player.capabilities();
    if (!state || state.id !== target.request || !Number.isFinite(caps?.duration) || caps.duration <= 0) { scheduleSupplied(FAST); return; }
    const durationMs = shown && samePair(shown.pair, target) && Number.isInteger(shown.snap?.duration_ms) ? shown.snap.duration_ms : Math.floor(caps.duration * 1000);
    suppliedBusy = true;
    try {
      const answer = await reads.read('supplied_chapters', {request: target.request, generation: target.generation, token: target.token, duration_ms: durationMs});
      if (mine !== suppliedEpoch || !samePair(target, pair)) return;  // for an earlier observation: nothing applies
      const v = answer?.ok ? answer.value : null;
      const same = v?.request === target.request && v?.generation === target.generation && v?.token === target.token;
      const rows = same ? suppliedRows(v, durationMs) : [];
      supplied = rows.length ? {rows, value: v, pair: target, durationMs, signature: suppliedSignature(rows, v)} : null;
      updateStrip();
      if (suppliedOpen) showSupplied();
    } catch (error) {
      if (String(error?.message || error).includes('unloaded')) return;
      if (mine === suppliedEpoch) { supplied = null; updateStrip(); if (suppliedOpen) showSupplied(); }
    } finally {
      suppliedBusy = false;
      if (mine === suppliedEpoch && samePair(target, pair)) scheduleSupplied(INFO_EVERY);
    }
  }
  function openSupplied() {
    if (!supplied) return;
    suppliedPanel.hidden = false;ui.supplied.say = '';showSupplied();
    suppliedOpen = suppliedPanel.expand({onCollapse: () => { suppliedOpen = false; suppliedPanel.hidden = true; }});
    if (!suppliedOpen) suppliedPanel.hidden = true;
  }
  // Information: curated facts for the playing title, from this module's own backend (which reads
  // published manifests and matches only on exact stated ids). Read-only; never used to seek.
  // Optional: without the backend.reads capability (or its backend), there is simply no button.
  const reads = cinema.backend;
  const infoPanel = cinema.surfaces.player('player.below', {title: 'Information', sections: []}, {hidden: true, onAction: ({id}) => {
    if (id === 'info-more') { ui.info.more = !ui.info.more;showInfo(); }
    else if (id === 'info-details') { ui.info.details = !ui.info.details;showInfo(); }
  }});
  let info = null, infoFor = null, infoEpoch = 0, infoBusy = false, infoAgain = false, infoTimer = null, infoOpen = false, following = null;
  // TEST diagnostics (status-row residual): only when a driver set globalThis.__cinemaInfoTrace; bounded, path-free. Keyed
  // by a short prefix of the opaque request id and the info epoch; the Markers status only (state and reason), no content.
  const itrace = (e) => { const T = globalThis.__cinemaInfoTrace; if (Array.isArray(T) && T.length < 2000) T.push({t: Math.round(globalThis.performance?.now?.() ?? 0), ...e}); };
  const rq = (id) => (typeof id === 'string' ? id.slice(0, 8) : null);
  const mk = (i) => (i?.markers ? [i.markers.state ?? null, i.markers.reason ?? null] : null);
  let stripDrawn = null;
  function updateStrip() {
    const items = [];   // generated points are never called chapters
    if (shown) items.push({kind: 'action', id: 'opener', label: shown.mode === 'intervals' ? 'Interval points' : 'Chapters'});
    if (supplied) items.push({kind: 'action', id: 'supplied-opener', label: 'Supplied chapters'});
    if (info) items.push({kind: 'action', id: 'info-opener', label: 'Information'});
    // Drawn again only when its entry points change: the same buttons stay (focus, and the player's way back to them).
    const json = JSON.stringify(items);if (json !== stripDrawn) { stripDrawn = json;strip.update({sections: items.length ? [{items}] : []}); }
    strip.hidden = !items.length;
    drawPanel();
  }
  const scheduleInfo = (ms) => {
    if (infoTimer !== null) cinema.timers.clear(infoTimer);
    try { infoTimer = cinema.timers.setTimeout(() => { infoTimer = null; refreshInfo(); }, ms); }
    catch { infoTimer = null; }
  };
  // Drawn again only when what it shows changes: the panel re-reads its facts every few seconds, and a picture must not
  // reload (or flicker) each time.
  let infoDrawn = '';
  const showInfo = () => {
    itrace({ev: 'paint', req: rq(infoFor), epoch: infoEpoch, info: !!info, markers: mk(info)});
    const view = presentationView(info), text = (t, tone = 'muted') => ({kind: 'text', text: t, tone}), sections = [];
    const head = view ? JSON.stringify(view) : null;
    if (head !== ui.info.head) { ui.info.head = head;ui.info.more = false; }
    if (view) {
      const words = [];
      if (view.picture) words.push({kind: 'image', art: view.picture, label: view.title, shape: view.pictureKind === 'poster' ? 'poster' : 'wide'});
      if (view.facts.length) words.push(text(view.facts.join(' · ')));
      if (view.tagline) words.push(text(view.tagline));
      if (view.overview) {
        const long = view.overview.length > 220;
        words.push(text(long && !ui.info.more ? `${view.overview.slice(0, 220).trimEnd()}…` : view.overview, 'normal'));
        if (long) words.push({kind: 'action', id: 'info-more', label: ui.info.more ? 'Show less' : 'Read more', expanded: ui.info.more});
      }
      if (view.hint) words.push(text(view.hint));
      if (view.provider) words.push(text(`Pictures, cast and details from ${view.provider}.`));
      sections.push({heading: view.title, items: words});
      if (view.cast.length) sections.push({items: [{kind: 'list', id: 'cast', layout: 'people', label: 'Cast',
        items: view.cast.map((person, i) => ({id: 'p' + i, title: person.name, subtitle: person.role || null, art: person.portrait || null, badge: initials(person.name)}))}]});
    }
    if (info) {
      // Every existing row, unchanged, one keyboard step away under "Details and sources" (collapsed by default).
      sections.push({items: [{kind: 'action', id: 'info-details', label: 'Details and sources', expanded: ui.info.details}]});
      if (ui.info.details) sections.push({items: [{kind: 'facts', rows: infoRowsFor(info, {chapters: shown ? 'video' : pair ? 'none' : 'unknown'}).map(([label, value]) => ({label, value}))}]});
    }
    // Credited to the publisher that stated it; a publisher without a label is not named (never assumed Advanced Media).
    const note = info ? `From ${info.source_label || 'curated information'}. Which release is playing has not been checked.` : 'Information isn\'t available for this video right now.';
    const descriptor = {title: 'Information', status: {state: 'ok', text: note}, sections};
    const json = JSON.stringify(descriptor);if (json === infoDrawn) return;infoDrawn = json;infoPanel.update(descriptor);
  };
  // The panel is cleared whether or not it is open: a closed (hidden) panel never keeps another request's rows (found by
  // the status-row diagnosis, 2026-11-12: Akira's provider-off row survived hidden into the next title's playback).
  const dropInfo = () => { infoEpoch += 1; info = null; infoFor = null; updateStrip(); showInfo(); };
  async function refreshInfo() {
    if (!reads) return;
    if (infoBusy) { infoAgain = true; return; }
    const request = cinema.player.state()?.id ?? null;
    if (!request) { dropInfo(); return; }
    infoBusy = true;
    const mine = infoEpoch;
    itrace({ev: 'read', req: rq(request), epoch: mine});
    try {
      const answer = await reads.read('information', {request});
      const now = cinema.player.state()?.id ?? null;
      if (mine !== infoEpoch || now !== request) { itrace({ev: 'discard', req: rq(request), epoch: mine, current: rq(now), epochNow: infoEpoch}); return; }  // for an earlier request
      const value = answer?.ok ? answer.value : null;
      const matched = value?.state === 'matched' && value.request === request && typeof value.info?.title === 'string' && value.info.title;
      itrace({ev: 'answer', req: rq(request), epoch: mine, ok: !!answer?.ok, state: value?.state ?? null, answerReq: rq(value?.request), matched: !!matched, markers: mk(value?.info)});
      info = matched ? value.info : null;infoFor = matched ? request : null;
      updateStrip();
      if (infoOpen) showInfo();
    } catch (error) {
      itrace({ev: 'error', req: rq(request), epoch: mine, code: String(error?.code || error?.message || error).slice(0, 40)});
      if (String(error?.message || error).includes('unloaded')) return;
      if (mine === infoEpoch) { info = null; updateStrip(); if (infoOpen) showInfo(); }
    } finally {
      infoBusy = false;
      // The request changed while this read was out: read for the new one now, not in INFO_EVERY.
      if (infoAgain || mine !== infoEpoch) { infoAgain = false; scheduleInfo(0); }
      else if (cinema.player.state()) scheduleInfo(INFO_EVERY);
    }
  }
  function openInfo() {
    infoPanel.hidden = false;showInfo();
    infoOpen = infoPanel.expand({onCollapse: () => { infoOpen = false; infoPanel.hidden = true; }});
    if (!infoOpen) { infoPanel.hidden = true; return; }
    scheduleInfo(0);  // fresh facts on opening
  }
  // The playing request changed (or playback ended): what was shown belonged to the previous one.
  function infoFollow(state) {
    const id = state?.id ?? null;
    if (id === following) return;
    itrace({ev: 'follow', from: rq(following), to: rq(id), epoch: infoEpoch + 1});
    following = id;
    dropInfo();
    if (infoOpen && !id) { infoOpen = false; cinema.player.collapse(); infoPanel.hidden = true; }
    if (id) scheduleInfo(0);
  }

  const markCurrent = () => { if (shown) drawPanel(); };
  const render = () => drawPanel();
  // The chapter or interval panel, from what is shown: each entry (its number, name, time, a thumbnail when one exists and,
  // for interval points, its frame's own decoded time or why there is none), the current one marked, the last outcome,
  // and for interval points their page and setting. Drawn again only when that changes.
  let panelDrawn = '';
  function drawPanel() {
    const intervals = shown?.mode === 'intervals', label = intervals ? 'Interval points' : 'Chapters', entries = shown?.entries || [];
    const byStart = intervals ? new Map([...tiles.frames].map(([at, f]) => [at, f.image])) : new Map((assets?.chapters || []).map(c => [c.start_ms, c.image]));
    const position = cinema.player.state()?.position;
    const current = !shown ? -1 : !intervals ? currentIndex(entries, position)
      : Number.isFinite(position) ? entries.findIndex(e => e.start_ms <= position * 1000 && position * 1000 < e.start_ms + shown.plan.step_ms) : -1;
    const items = entries.map((entry, i) => {
      let meta = null;
      if (intervals) {   // the decoded frame's own time goes with it, never the point's time alone; a refusal is said
        const frame = tiles.frames.get(entry.start_ms), refused = !frame && tiles.refused.has(entry.start_ms);
        const why = refused ? refusalWords(tiles.refused.get(entry.start_ms)) : '';
        meta = frame ? `frame ${preciseClock(frame.at_ms)}` : refused ? `no thumbnail${why ? ` (${why})` : ''}` : null;
      }
      return {id: 'n' + entry.number, badge: String(entry.number), title: nameOf(entry), subtitle: clock(entry.start_ms), meta, art: byStart.get(entry.start_ms) || null, current: i === current};
    });
    const sections = [{items: [{kind: 'list', id: 'entries', label, layout: byStart.size > 0 || (intervals && shown.basis.previewable) ? 'tiles' : 'rows', items}]}];
    if (intervals) {
      const s = shown, at = INTERVAL_CHOICES.indexOf(minutes);
      sections.push({items: [
        {kind: 'text', tone: 'muted', text: s.plan.count ? `Points ${s.entries[0].number}–${s.entries.at(-1).number} of ${s.plan.count}, every ${minutes} min`
          : `Every ${minutes} min would make more than ${INTERVAL_MAX_POINTS} points: choose a longer interval.`},
        {kind: 'action', id: 'interval-earlier', label: 'Earlier points', disabled: s.page <= 0},
        {kind: 'action', id: 'interval-later', label: 'Later points', disabled: s.page >= s.pages - 1},
        {kind: 'action', id: 'interval-shorter', label: 'Shorter interval', disabled: at <= 0},
        {kind: 'action', id: 'interval-longer', label: 'Longer interval', disabled: at >= INTERVAL_CHOICES.length - 1},
        {kind: 'text', tone: 'muted', text: intervalNote(s.basis, minutes)}]});
    }
    const descriptor = {title: label, status: seeking || ui.status ? {state: seeking ? 'pending' : 'ok', text: ui.status} : null, sections};
    const json = JSON.stringify(descriptor);if (json === panelDrawn) return;panelDrawn = json;panel.update(descriptor);
  }
  function panelAction({id, item}) {
    if (id === 'entries' && item) { const entry = shown?.entries.find(e => 'n' + e.number === item);if (entry) choose(entry);return; }
    if (id === 'interval-earlier') turnPage(-1);else if (id === 'interval-later') turnPage(1);
    else if (id === 'interval-shorter') changeInterval(-1);else if (id === 'interval-longer') changeInterval(1);
  }
  function open() {
    if (!shown) return;
    if (shown.mode === 'intervals') readInterval();  // a preference changed in Settings since the last read
    panel.hidden = false;say('');
    isOpen = panel.expand({onCollapse: () => { isOpen = false; panel.hidden = true; }});
    if (!isOpen) { panel.hidden = true; return; }
    const current = panel.element.querySelector('[aria-current="true"]')?.dataset.surfaceKey;
    if (current && panel.element.contains(document.activeElement)) panel.focus(current);
  }

  async function choose(entry) {
    if (!shown || seeking) return;
    const intervals = shown.mode === 'intervals';
    // Interval points move the video only where the core already admits any request-checked target (a verified local
    // file); on a stream they stay preview-only, with the reason said (never a bypass of the stream seek rule).
    if (intervals && !shown.basis.navigable) { say(intervalRefusal(shown.basis)); return; }
    if (!intervals && !navigable(shown.snap)) { say('Moving to a chapter isn\'t available for this video.'); return; }
    const mine = epoch, target = shown;
    seeking = true;say(`Going to “${nameOf(entry)}”…`);
    const stillThis = () => epoch === mine && shown === target;
    const changed = () => { if (stillThis()) { clear(); schedule(0); } };
    try {
      // The video may have moved on since the list was drawn: check again before asking to move.
      const now = observed(await cinema.playback.current());
      if (!stillThis()) return;
      const state = cinema.player.state(), caps = cinema.player.capabilities();
      const again = intervals ? intervalBasis(target.snap, now, state, caps) : null;
      const valid = samePair(now, target.pair) && state?.id === target.pair.request && caps?.duration === target.duration &&
        (intervals ? !!again?.navigable && again.duration_ms === target.basis.duration_ms && entry.start_ms % target.plan.step_ms === 0 && entry.start_ms < target.basis.duration_ms
          : chaptersFor(target.snap, now, state, caps).entries.some(e => e.start_ms === entry.start_ms));
      if (!valid) { changed(); return; }  // nothing is sent; the list goes and is asked for again
      // An interval point is declared as one (its step), so the core can admit it on a progressive stream after checking.
      const result = await cinema.playback.seek(intervals
        ? {request: target.pair.request, generation: target.pair.generation, to_ms: entry.start_ms, kind: 'interval', step_ms: target.plan.step_ms}
        : {request: target.pair.request, generation: target.pair.generation, to_ms: entry.start_ms});
      if (!stillThis()) return;  // a late answer after the video changed: nothing acts on another video
      const {text, changed: moved} = outcomeText(result, nameOf(entry));
      if (moved) { changed(); return; }
      // The player's own position after the move, when the core could observe it (never assumed from the target).
      say(intervals && result?.outcome === 'dispatched' && Number.isInteger(result.landed_ms) ? `${text} Now at ${clock(result.landed_ms)}.` : text);
    } catch (error) {
      if (String(error?.message || error).includes('unloaded') || !stillThis()) return;
      say(outcomeText(null).text);
    } finally {
      if (stillThis()) { seeking = false;drawPanel(); }
    }
  }

  async function refresh() {
    if (busy) { again = true; return; }
    busy = true;
    let next = SLOW;
    try {
      const current = await cinema.playback.current();
      const live = observed(current);
      adopt(live);
      if (!live) {
        clear();
        // A playing request whose observation is momentarily missing is asked again soon (bounded);
        // nothing playing, or a kind of source this module does not handle, slowly.
        next = current && !current.token ? fast('none') : SLOW;
        return;
      }
      const asked = pair;
      const snap = await cinema.playback.chapters({request: asked.request, generation: asked.generation});
      if (!samePair(asked, pair)) { next = 0; return; }  // an answer for an earlier observation
      const state = cinema.player.state(), caps = cinema.player.capabilities();
      const {entries} = chaptersFor(snap, pair, state, caps);
      if (entries.length) {
        const signature = JSON.stringify(entries.map(e => [e.start_ms, e.title]));
        if (!shown || !samePair(shown.pair, pair) || shown.signature !== signature) {
          if (shown && !samePair(shown.pair, pair)) clear();
          if (shown?.mode === 'intervals') clear();  // the video's own chapters came: they replace the interval points
          cinema.player.marks(entries.filter(e => e.start_ms > 0).map(e => ({at: e.start_ms / 1000, kind: 'chapter'})));
          shown = {mode: 'chapters', pair, signature, entries, snap, duration: caps.duration};  // a new list: a choice still out is for the old one
          render();
          if (assets) dropAssets();
          scheduleAssets(0);  // one request for this observation's supplied images
        } else Object.assign(shown, {snap, duration: caps.duration});      // the same list: kept, so a choice still out applies
        updateStrip();markCurrent();applyThumbs();
      } else {
        // No chapters of its own: interval points, only when the core said so definitively (never while loading).
        const basis = intervalBasis(snap, pair, state, caps);
        if (basis) showIntervals(basis, snap, caps); else clear();
      }
      if (snap.state === 'loading' || snap.state === 'unknown') next = fast(`${pair.request}/${pair.generation}`);
    } catch (error) {
      if (String(error?.message || error).includes('unloaded')) return;
      clear();
      next = SLOW;
    } finally {
      busy = false;
      if (again) { again = false; schedule(0); } else schedule(next);
    }
  }

  cinema.playback.onEvent((event) => {
    if (TERMINAL.has(event.type)) { adopt(null); clear(); schedule(0); return; }
    if (event.type === 'source') {
      adopt(event.token ? {request: event.request, generation: event.generation, token: event.token} : null);
      if (!event.token) clear();
      schedule(0);
    } else if (event.type === 'requested' || event.type === 'av_started') {
      if (pair && pair.request !== event.request) { adopt(null); clear(); }
      schedule(0);
    }
  });
  cinema.player.onState((state) => {
    infoFollow(state);
    if (!state) { dropPreview(); clear(); return; }  // the player view closed: nothing late is shown
    if (pair && state.id !== pair.request) dropPreview();
    // The list was made for one request and one duration; anything else clears it until asked again.
    if (shown && (state.id !== shown.pair.request || state.duration !== shown.duration)) { clear(); schedule(0); return; }
    markCurrent();
  });
  schedule(0);
  infoFollow(cinema.player.state());
  // ONE continuation owner (2026-11-17, architectural correction): the manual queue and the detected sequence share it.
  // A deliberately armed queue run (or a Play queue start being checked) takes precedence; the detected sequence then
  // shows nothing, so a playback never gets two prompts or two continuations.
  const continuation = {queueActive: () => false, yieldSequence: () => {}};
  if (cinema.items && cinema.page && cinema.storage) mountQueue(cinema, continuation);
  if (typeof cinema.player.sequence === 'function') mountSequence(cinema, continuation);
}

// Detected sequence: Next Up without a manual queue (approved playback UX item 4; 2026-11-17 correction) ---------------
// What plays now may BE part of an explicitly ordered sequence: a season's episodes, the work's installments in their
// stated order (the next season, or the next film of an ordered series), a franchise's Seasons read completely with
// explicit order numbers, a listing's episodes by season and number. The core states those facts for the playing request
// (player.sequence(), opaque refs); this module recognizes which applies and the ONE next entry, or none:
// - the next listed episode of the same entry (an OVA/special entry's episodes stay in that entry); after the last
//   LISTED episode while more are unread: none (unknown);
// - after the last episode (or a film playable as itself): the next installment of an ESTABLISHED, LINEAR work at
//   exactly the next order number, if it has aired; else a COMPLETE franchise Seasons read with explicit orders;
// - a listed episode: the same season's episode numbered exactly one more, once only.
// Never a recommendation, a release-year guess, a title match, a branching/unestablished order, a gap, a duplicate, an
// unaired entry, or another season of a listing. The choice opens that entry's OWN card through the core's validated
// continueTo(ref): its sources stay the user's choice; nothing plays by itself.
export const NEXT_UP_LEAD = 30;   // seconds before a known end
const whole = v => Number.isInteger(v) ? v : null;
export function sequenceNext(facts) {
  const f = facts || {};
  if (Array.isArray(f.listing)) {
    const season = whole(f.current?.season), number = whole(f.current?.number);
    if (season == null || number == null) return {none: 'This episode states no season and episode number.'};
    const found = f.listing.filter(e => !e.current && whole(e.season) === season && whole(e.episode) === number + 1);
    if (found.length !== 1) return {none: found.length ? 'The next episode is listed more than once.' : 'The next episode is not in this list.'};
    const name = typeof found[0].label === 'string' && found[0].label.trim() && !/^episode\s+\d+$/i.test(found[0].label.trim()) ? found[0].label.trim() : '';
    return {ref: found[0].ref, kind: 'episode', label: `Episode ${number + 1}${name ? ` · ${name}` : ''}`, special: false, source: 'listing'};
  }
  if (f.current?.kind === 'episode') {
    const list = Array.isArray(f.episodes) ? f.episodes : [], at = list.findIndex(e => e.current);
    if (at < 0) return {none: 'The playing episode is not in the listed episodes.'};
    const next = list[at + 1];
    if (next) {
      const n = whole(next.number), name = typeof next.name === 'string' && next.name.trim() ? next.name.trim() : '';
      return {ref: next.ref, kind: 'episode', special: !!next.special,
        label: `${next.special ? 'Special' : 'Episode'}${n != null ? ' ' + n : ''}${name ? ` · ${name}` : ''}`, source: 'episodes'};
    }
    if (!f.episodes_complete) return {none: 'Later episodes have not been read yet.'};
  }
  const later = (items) => {
    const here = items.find(p => p.current);
    if (!here || whole(here.order) == null) return null;
    const after = items.filter(p => whole(p.order) === here.order + 1);
    if (after.length !== 1) return {none: after.length ? 'The next part is not one entry.' : 'No later part is stated.'};
    const p = after[0];
    if (p.status === 'not_yet_released' || (whole(p.aired) === 0 && p.status !== 'finished')) return {none: `${p.label || 'The next part'} has not aired yet.`};
    return {ref: p.ref, kind: 'installment', label: p.label || 'The next part', special: false, source: 'order'};
  };
  const w = f.work;
  if (w && w.identity === 'established' && w.order === 'linear' && Array.isArray(w.installments)) {
    const found = later(w.installments);
    if (found) return found;
  }
  const s = f.seasons;
  if (s && s.state === 'complete' && Array.isArray(s.items) && s.items.length && s.items.every(e => whole(e.order) != null)) {
    const found = later(s.items);
    if (found) return found;
  }
  return {none: 'No next entry is established for this one.'};
}
// One Up Next owner per playback (R12): an add-on that has its own Up Next keeps it; when Cinema cannot tell, Advanced
// Playlist offers nothing rather than risk two prompts. The fact comes with the sequence (continuation.up_next: none |
// addon | unknown); an older Cinema states none: a listing then counts as unknown, Cinema's own player as none.
export function sourceUpNext(facts) {
  const stated = facts?.continuation?.up_next;
  return ['none', 'addon', 'unknown'].includes(stated) ? stated : facts?.origin === 'listing' ? 'unknown' : 'none';
}
export function nextUpReason({next, request, state, caps, yielding, queueActive, sourceUpNext: source = 'none'}) {
  if (queueActive) return 'the queue is playing';
  if (source === 'addon') return 'its addon has its own Up Next';
  if (source !== 'none') return 'not known whether its addon has its own Up Next';
  if (!next || next.none) return 'no next entry';
  if (!state || state.id !== request) return 'not the playing request';
  if (!caps || !caps.timed || caps.live === true || !Number.isFinite(caps.duration) || caps.duration <= 0) return 'no known finite duration';
  if (!Number.isFinite(state.position) || state.position < caps.duration - NEXT_UP_LEAD) return 'not near the end';
  if (yielding) return 'an addon dialog is open';
  return 'ready';
}
export function nextUpMessage(next) {
  return {message: `Up next: ${next.label}`, action: next.kind === 'installment' ? 'Next part' : next.special ? 'Next special' : 'Next episode'};
}
function mountSequence(cinema, continuation) {
  let handle = null, shownFor = null, dismissedFor = null;
  const close = () => { const h = handle; handle = null; try { h?.close(); } catch { /* already gone */ } };
  continuation.yieldSequence = close;   // the armed queue's own prompt replaces this one
  const check = (state) => {
    const facts = cinema.player.sequence();
    if (!facts || !state || state.id !== facts.request) { close(); return; }
    if (handle || shownFor === facts.request || dismissedFor === facts.request) return;   // once per playback; "Not now" is final for it
    const next = sequenceNext(facts);
    if (nextUpReason({next, request: facts.request, state, caps: cinema.player.capabilities(), yielding: cinema.player.yielding,
      queueActive: continuation.queueActive(), sourceUpNext: sourceUpNext(facts)}) !== 'ready') return;
    const {message, action} = nextUpMessage(next);
    const request = facts.request;
    handle = cinema.player.prompt({message, actions: [
      {label: action, onPress: () => { close(); cinema.player.continueTo(next.ref); }},
      {label: 'Not now', onPress: () => { close(); dismissedFor = request; }}],
      onDismiss: () => { handle = null; dismissedFor = request; }});
    if (!handle) return;
    handle.element.dataset.playlist = 'next-up';
    shownFor = request;
  };
  cinema.player.onState(check);
  cinema.player.onSequence(() => check(cinema.player.state()));
}
// Manual queue --------------------------------------------------------------------------------------
// Saved playable references (playback.capture) the user adds from an item's details, kept in this module's
// own storage as {ref, label, media_type}: never action tokens, requests, hashes, URLs or history. Plays
// only when the user chooses Play or Play queue, through the normal module play operation. Manual Play never
// continues; only an armed Play queue run shows Up Next and starts the next entry, once per natural end.
export const QUEUE_LIMIT = 50;
const REF = /^[A-Za-z0-9_-]{8,64}$/, MEDIA_TYPE = /^[a-z][a-z0-9_.-]{0,31}$/;
export function cleanQueue(raw) {
  const out = [], seen = new Set();
  for (const e of Array.isArray(raw) ? raw : []) {
    if (!e || typeof e.ref !== 'string' || !REF.test(e.ref) || seen.has(e.ref) || out.length >= QUEUE_LIMIT) continue;
    seen.add(e.ref);
    out.push({ref: e.ref, label: cleanTitle(e.label) || 'Saved item', media_type: typeof e.media_type === 'string' && MEDIA_TYPE.test(e.media_type) ? e.media_type : 'unknown'});
  }
  return out;
}
const PROBLEMS = {provider_unavailable: 'Its addon is not available right now.', replay_unverified: 'Cinema can no longer confirm it would play the same way.',
  listing_properties_required: 'It needs its original listing to play.', reference_invalid: 'This saved item is no longer valid.'};
export const problemText = (code) => PROBLEMS[code] || 'Not available right now.';
// Plain words for a play operation as polled. "bound" means the request exists, not that anything is on
// screen: it stays "Starting" until that request's own first frame (an event).
export function outcomeOf(operation, label) {
  if (!operation) return {text: `“${label}” could not be started.`, done: true};
  if (operation.state === 'bound') return {text: `Starting “${label}”…`, done: true, bound: true};
  if (operation.state !== 'done') return {text: `Starting “${label}”…`, done: false};
  if (operation.outcome === 'finished') return {text: `“${label}” played.`, done: true};
  if (operation.outcome === 'cancelled') return {text: `“${label}” was cancelled.`, done: true};
  if (operation.outcome === 'no_playback') return {text: `The addon finished without playing “${label}”.`, done: true};
  return {text: `“${label}” could not be played: ${cleanTitle(operation.message) || 'the addon or player could not play it.'}`, done: true};
}

// Queue run (opt-in) -------------------------------------------------------------------------------------
// Only an explicit "Play queue" arms a run. Up Next and the one-step advance belong to that run alone.
export const UP_NEXT_LEAD = 10;  // seconds before a known end
// The entry after `ref` in the queue as it is now (by saved ref, never by row), or null.
export function nextAfter(queue, ref) {
  const i = queue.findIndex(e => e.ref === ref);
  return i < 0 ? null : queue[i + 1] || null;
}
// Whether an Up Next prompt may be shown now, or why not. Pure.
export function upNextReason({run, state, caps, yielding, next}) {
  if (!run || !run.armed) return 'not armed';
  if (!run.request || run.terminal) return 'no current request';
  if (!run.framed) return 'no first frame yet';
  if (!state || state.id !== run.request) return 'not the playing request';
  if (!caps || !caps.timed || caps.live === true || !Number.isFinite(caps.duration) || caps.duration <= 0) return 'no known finite duration';
  if (!Number.isFinite(state.position) || state.position < caps.duration - UP_NEXT_LEAD) return 'not near the end';
  if (yielding) return 'an addon dialog is open';
  if (!next) return 'no next entry';
  return 'ready';
}
// Why automatic continuation cannot include an item (its addon's own Up Next), in plain words, or null.
export function continuationProblem(reference, label) {
  if (!reference) return `“${label}” is no longer saved.`;
  if (reference.available !== true) return `“${label}”: ${problemText(reference.problem)}`;
  const up = reference.features?.up_next;
  if (up === 'none') return null;
  if (up === 'addon') return `“${label}”'s addon has its own Up Next, so Cinema won't continue the queue with it. Use Play instead.`;
  return `Cinema doesn't know whether “${label}”'s addon has its own Up Next, so it won't continue the queue with it. Use Play instead.`;
}
const STOPPED = {stopped: 'was stopped', failed: 'could not be played', replaced: 'was replaced by another video', closed: 'was closed'};

function mountQueue(cinema, continuation = {}) {
  // The Queue view is a SEMANTIC surface (2026-11-17, review of bh): what is queued, each entry's state and actions, the run
  // controls; the active skin draws it. (An older Cinema without surfaces: no Queue view.)
  if (typeof cinema.surfaces?.view !== 'function') return;
  let statusText = '';
  let queue = [], writes = Promise.resolve(), availability = new Map(), checked = 0;
  let run = null;          // the one play this module is following (manual, or a step of an armed run)
  let pending = null;      // a Play queue start whose first entry is still being checked
  const recent = [];       // the latest events, for a request whose bind is seen after its events
  // Precedence (2026-11-17): while a deliberately armed run (or a Play queue start) is on, the queue alone continues.
  continuation.queueActive = () => !!(run && run.armed) || !!pending;
  const say = (text) => { statusText = text;render(); };
  // Leaving the Queue view voids a Play queue start still being checked, even if the view is opened again
  // before the check answers (that later opening is not the one Play queue was pressed in).
  const view = cinema.surfaces.view({label: 'Queue'}, {title: 'Queue', sections: []}, {onAction: p => queueAction(p),
    onOpen: () => refreshAvailability(), onClose: () => { if (pending) dropPending('Play queue did not start: the queue was closed.'); }});
  const gone = (error) => String(error?.message || error).includes('unloaded');

  function render() {
    const sections = [];
    if (queue.length) sections.push({items: [{kind: 'action', id: 'queue-play-all', label: 'Play queue', disabled: !!run || !!pending},
      ...(run?.armed || pending ? [{kind: 'action', id: 'queue-cancel', label: 'Cancel queue'}] : [])]});
    queue.forEach((entry, index) => {
      const known = availability.get(entry.ref);
      const note = run?.ref === entry.ref ? (run.armed ? 'Now playing (queue)' : 'Now playing') : known && !known.available ? problemText(known.problem) : '';
      sections.push({heading: `${index + 1}. ${entry.label}`, items: [...(note ? [{kind: 'text', text: note, tone: 'muted'}] : []),
        {kind: 'action', id: `play.${entry.ref}`, label: 'Play', note: entry.label, disabled: !!known && !known.available || !!run || !!pending},
        {kind: 'action', id: `up.${entry.ref}`, label: 'Up', note: entry.label, disabled: index === 0},
        {kind: 'action', id: `down.${entry.ref}`, label: 'Down', note: entry.label, disabled: index === queue.length - 1},
        {kind: 'action', id: `remove.${entry.ref}`, label: 'Remove', note: entry.label}]});
    });
    if (!queue.length) {   // UX-05: an empty queue says what goes here and offers one way on, the page's own Search (or Home)
      const way = cinema.page?.browseLabel?.() || '';
      sections.push({items: [{kind: 'text', text: 'Your queue is empty.', tone: 'normal'},
        {kind: 'text', text: 'Open a title\'s details and choose “Add to queue” to line it up here.', tone: 'muted'},
        ...(way ? [{kind: 'action', id: 'queue-browse', label: way}] : [])]});
    } else sections.push({items: [{kind: 'action', id: 'queue-clear', label: 'Clear queue'}]});
    view.update({title: 'Queue', status: statusText ? {state: 'ok', text: statusText} : null, sections});
  }
  function queueAction({id}) {
    if (id === 'queue-play-all') return playQueue();
    if (id === 'queue-cancel') { if (pending) dropPending('Play queue was cancelled.');else if (run) cancelRun(run);return; }
    if (id === 'queue-browse') return cinema.page?.browse?.();
    if (id === 'queue-clear') return clearAll();
    const m = /^(play|up|down|remove)\.(.+)$/.exec(id), entry = m ? queue.find(e => e.ref === m[2]) : null;
    if (!entry) return;
    if (m[1] === 'play') start(entry, false);else if (m[1] === 'up') move(entry.ref, -1);else if (m[1] === 'down') move(entry.ref, 1);else remove(entry.ref);
  }
  // Every change re-reads the stored queue, applies itself and writes, one at a time: nothing another
  // change of this module wrote meanwhile is lost (storage replaces one key at a time). A reference is
  // forgotten inside the same chain, and only if it is not queued at that moment, so a quick re-add of
  // the same item never loses its reference to a late forget.
  const change = (fn) => {
    const step = writes.then(async () => {
      const now = cleanQueue((await cinema.storage.get()).queue);
      const out = (await fn(now)) || {};
      if (out.write) await cinema.storage.set('queue', out.write);
      queue = out.write || now;
      for (const ref of out.forget || []) if (!queue.some(e => e.ref === ref)) await cinema.playback.forget(ref).catch(() => {});
      render();followEdit();
      return out;
    });
    writes = step.catch(() => {});
    return step;
  };
  async function refreshAvailability(force = false) {
    if (!force && Date.now() - checked < 2000) return;
    checked = Date.now();
    try {
      const listed = await cinema.playback.references();
      availability = new Map((listed || []).map(r => [r.ref, {available: r.available === true, problem: r.problem}]));
    } catch (error) { if (gone(error)) return; }
    render();
  }
  const fresh = async (ref) => (await cinema.playback.references() || []).find(r => r.ref === ref) || null;

  // One play: its operation is followed to the request it became, then that request's events. -------------
  const closePrompt = (r) => { const p = r.prompt; r.prompt = null; if (p) p.close(); };
  const disarm = (r, text) => { r.armed = false; closePrompt(r); if (text) say(text); };
  function finish(r) {
    closePrompt(r);
    if (r.op && r.request === null) cinema.playback.cancel(r.op).catch(() => {});  // still waiting: withdrawn
    if (run === r) run = null;
    render();
  }
  // The user cancels the run: the latch is set before anything is awaited. The video keeps playing.
  function cancelRun(r) {
    if (!r.armed) return;
    r.armed = false;r.cancelled = true;
    closePrompt(r);
    say(`The queue will stop after “${r.label}”.`);render();
  }
  async function start(entry, armed) {
    if (run) { say('Another queued item is starting.'); return; }
    if (pending) dropPending('');  // another Play: a Play queue start still being checked is void
    if (!queue.some(e => e.ref === entry.ref)) { say(`“${entry.label}” is no longer in your queue.`); return; }
    const r = {ref: entry.ref, label: entry.label, armed, cancelled: false, op: null, request: null, framed: false, terminal: null, prompt: null, promptRef: null, checking: false};
    run = r;render();
    view.close();  // the normal Play path takes the screen
    say(`Starting “${entry.label}”…`);
    let operation = null;
    try { operation = await cinema.playback.play(entry.ref); } catch (error) { if (gone(error)) return; }
    if (run !== r) { if (operation?.id && operation.state !== 'done') cinema.playback.cancel(operation.id).catch(() => {}); return; }
    if (!operation?.id) { disarm(r, outcomeOf(null, entry.label).text); finish(r); return; }
    r.op = operation.id;
    for (let i = 0; i < 600 && run === r && r.request === null; i++) {
      if (typeof operation.request === 'string' && (operation.state === 'bound' || operation.state === 'done')) { bind(r, operation.request); break; }
      if (operation.state === 'done') { disarm(r, outcomeOf(operation, entry.label).text); finish(r); return; }
      await new Promise(resolve => { try { cinema.timers.setTimeout(resolve, 1000); } catch { resolve(); } });
      if (run !== r) return;
      try { operation = await cinema.playback.operation(r.op); }
      catch (error) { if (gone(error)) return; operation = null; }
      if (run !== r) return;
      if (!operation) { disarm(r, `“${entry.label}”: its outcome could not be read, so nothing more was done.`); finish(r); return; }
    }
    if (run === r && r.request === null) { disarm(r, `“${entry.label}” did not start in time.`); finish(r); }
  }
  function bind(r, request) {
    r.request = request;
    for (const event of recent.filter(e => e.request === request)) { if (run !== r) break; apply(r, event); }
  }
  function apply(r, event) {
    if (event.request !== r.request) {
      if (event.type === 'requested') { disarm(r, r.armed ? 'Another video started, so the queue stopped.' : ''); finish(r); }
      return;
    }
    if (event.type === 'first_frame') { if (!r.framed) { r.framed = true; if (!r.terminal) say(`Playing “${r.label}”.`); } return; }
    if (!event.terminal || r.terminal) return;  // one terminal per request: a repeated "ended" does nothing
    r.terminal = event.type;
    if (event.type === 'ended') { ended(r); return; }
    disarm(r, `“${r.label}” ${STOPPED[event.type] || 'ended'}.${r.armed ? ' The queue stopped.' : ''}`);finish(r);
  }
  cinema.playback.onEvent((event) => {
    recent.push(event);if (recent.length > 40) recent.shift();
    // Any request while Play queue is still checking is someone else's play: that start is stale.
    if (pending && event.type === 'requested') dropPending('Another video started, so Play queue did not start.');
    const r = run;if (!r) return;
    if (r.request === null) {
      // Before the bind: any request that is not this module's own play means someone else is playing.
      if (event.type === 'requested' && !(event.origin === 'module' && event.module === cinema.id)) { disarm(r, r.armed ? 'Another video started, so the queue stopped.' : ''); finish(r); }
      return;
    }
    apply(r, event);
  }, {onLoss: () => { if (pending) dropPending('Cinema lost track of playback, so Play queue did not start.'); const r = run; if (r) { disarm(r, 'Cinema lost track of playback, so the queue stopped.'); finish(r); } }});

  // A natural, owned end: at most one new normal play, of the very entry Up Next showed. -----------------
  async function ended(r) {
    const shown = r.promptRef;closePrompt(r);
    if (!r.armed) { if (!r.cancelled) say(`“${r.label}” ended.`); finish(r); return; }
    if (!r.framed) { disarm(r, `No video frame was seen for “${r.label}”, so the queue stopped.`); finish(r); return; }
    const next = nextAfter(queue, r.ref);
    if (!next) { disarm(r, 'That was the end of your queue.'); finish(r); return; }
    if (shown !== next.ref) { disarm(r, `Up next wasn't shown for “${next.label}”, so the queue stopped.`); finish(r); return; }
    let problem;
    try { problem = continuationProblem(await fresh(next.ref), next.label); }
    catch (error) { if (gone(error)) return; problem = 'The next item could not be checked, so the queue stopped.'; }
    if (run !== r || !r.armed) return;  // cancelled or interrupted while checking
    if (nextAfter(queue, r.ref)?.ref !== next.ref) { disarm(r, 'The queue changed, so it stopped.'); finish(r); return; }
    if (problem) { disarm(r, problem); finish(r); return; }
    finish(r);
    start(next, true);
  }
  // Up Next near a known end, for the next entry only after a fresh check of it.
  async function maybePrompt(state) {
    const r = run;if (!r || r.prompt || r.checking || r.promptRef) return;
    const next = nextAfter(queue, r.ref);
    if (upNextReason({run: r, state, caps: cinema.player.capabilities(), yielding: cinema.player.yielding, next}) !== 'ready') return;
    r.checking = true;
    let problem;
    try { problem = continuationProblem(await fresh(next.ref), next.label); }
    catch (error) { if (gone(error)) return; problem = 'The next item could not be checked, so the queue will stop here.'; }
    finally { r.checking = false; }
    if (run !== r || !r.armed || r.terminal || nextAfter(queue, r.ref)?.ref !== next.ref) return;
    if (problem) { disarm(r, problem); render(); return; }
    const now = cinema.player.state();
    if (upNextReason({run: r, state: now, caps: cinema.player.capabilities(), yielding: cinema.player.yielding, next}) !== 'ready') return;
    continuation.yieldSequence?.();   // exactly one prompt for this playback: the armed queue's
    const handle = cinema.player.prompt({message: `Up next: “${next.label}”`, actions: [{label: 'Cancel queue', onPress: () => cancelRun(r)}], onDismiss: () => cancelRun(r)});
    if (!handle) return;
    handle.element.dataset.playlist = 'up-next';
    r.prompt = handle;r.promptRef = next.ref;
  }
  // A queue edit while a run plays: Up Next follows "the entry after the current one" by saved ref.
  function followEdit() {
    if (pending && order() !== pending.order) dropPending('The queue changed, so Play queue did not start.');
    const r = run;if (!r || !r.promptRef) return;
    if (nextAfter(queue, r.ref)?.ref !== r.promptRef) { closePrompt(r); r.promptRef = null; maybePrompt(cinema.player.state()); }
  }
  cinema.player.onState((state) => maybePrompt(state));

  // Play queue's start, while its first entry is still being checked: one at a time, visibly cancellable,
  // and void as soon as anything the user did since makes it stale. Nothing is sent unless, right before the
  // play, it is still this very start, the queue is still the one it was pressed on, and its view is open.
  const order = () => queue.map(e => e.ref).join('|');
  function dropPending(text) {
    const p = pending;if (!p) return;
    pending = null;  // the latch: a late answer for p now finds another value here and does nothing
    if (text) say(text);
    render();
  }
  async function playQueue() {
    if (run || pending || !queue.length) return;
    const first = queue[0];
    const p = {ref: first.ref, order: order()};
    pending = p;render();
    say(`Checking “${first.label}”…`);
    let problem;
    try { problem = continuationProblem(await fresh(first.ref), first.label); }
    catch (error) { if (gone(error)) return; problem = 'The queue could not be checked.'; }
    if (pending !== p) return;  // cancelled or made stale meanwhile: its words were said then
    if (order() !== p.order || queue[0]?.ref !== p.ref) { dropPending('The queue changed, so Play queue did not start.'); return; }
    if (!view.open) { dropPending('Play queue did not start: the queue was closed.'); return; }
    if (problem) { dropPending(problem); return; }
    pending = null;
    start(first, true);
  }

  const move = (ref, delta) => change(now => {
    const i = now.findIndex(e => e.ref === ref), j = i + delta;
    if (i < 0 || j < 0 || j >= now.length) return {};
    const next = [...now];[next[i], next[j]] = [next[j], next[i]];
    return {write: next};
  }).catch(() => say('The queue could not be changed.'));
  const remove = (ref) => change(now => now.some(e => e.ref === ref) ? {write: now.filter(e => e.ref !== ref), forget: [ref]} : {})
    .then(out => { if (out.write) say('Removed from your queue.'); })
    .catch(() => say('The queue could not be changed.'));
  const clearAll = () => change(now => now.length ? {write: [], forget: now.map(e => e.ref)} : {})
    .then(out => { if (out.write) say('Your queue is empty.'); })
    .catch(() => say('The queue could not be changed.'));

  cinema.items.action({label: 'Add to queue', onPress: async ({label, media_type, action}) => {
    try {
      const out = await change(async now => {
        if (now.length >= QUEUE_LIMIT) return {say: `The queue is full (${QUEUE_LIMIT}). Remove an item first.`};
        const saved = await cinema.playback.capture(action);  // a refusal throws: nothing is written
        if (!saved || typeof saved.ref !== 'string' || !REF.test(saved.ref)) return {say: 'This item could not be saved.'};
        if (now.some(e => e.ref === saved.ref)) return {say: 'Already in your queue.'};
        return {write: [...now, ...cleanQueue([{ref: saved.ref, label: label || saved.label, media_type}])], say: 'Added to your queue.'};
      });
      refreshAvailability(true);
      return out.say;
    } catch (error) {
      if (gone(error)) throw error;  // turned off meanwhile: nothing was written, and the page draws nothing
      return `Not added: ${cleanTitle(error?.message) || 'this item cannot be saved.'}`;
    }
  }});
  // On mounting: references this module saved that no queue entry uses (an add cut short by the module
  // being turned off between its capture and its write) are forgotten, inside the chain.
  change(async now => {
    const listed = await cinema.playback.references().catch(() => null);
    return listed ? {forget: listed.map(r => r.ref).filter(ref => !now.some(e => e.ref === ref))} : {};
  }).then(() => refreshAvailability(true)).catch(() => {});
}

export function unmount() {
  // Nothing of its own to release: Cinema removes its UI, marks, timers and subscriptions.
}
