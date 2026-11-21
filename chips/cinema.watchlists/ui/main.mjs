// Watchlists' own screens (2026-11-17, D1 of the modularity audit). This module owns its words and its policy: which
// lists exist and what they are called, which service a Lists screen starts on, how the account's and this device's next
// episodes are merged, what an unread or partly read list means, and its title actions (Follow, Stop following,
// Tracking). It publishes them as SEMANTIC surfaces (cinema.surface/1) and title actions; the active skin draws them; the
// page ROUTES what it asks (lists.read, titles.call/open) and owns focus, keys and Back. Moved here unchanged in meaning
// from the page core (web/app.js Lists and title-card.mjs follow/tracking, alpha bf), which no longer knows any of it.

// ---- Words (owned here) -----------------------------------------------------------------------------------------------
export const LIST_SECTIONS = [['current', 'Continue watching'], ['next', 'Next up'], ['upcoming', 'Upcoming'], ['planning', 'Plan to watch'],
  ['completed', 'Completed'], ['paused', 'On hold'], ['dropped', 'Dropped'], ['repeating', 'Rewatching'], ['following', 'Following']];
const SECTION_LABEL = Object.fromEntries(LIST_SECTIONS);
const LOCAL_SECTIONS = ['next', 'upcoming'];   // from the series followed on this device, no account needed
// Following (2026-11-17, moved from the page core's "Manage following" screen): every local follow, to open or stop.
export const STOP_NOTE = 'Only the follow on this device goes. Your lists on any list service, your tracking choice, where you stopped watching and the episodes you watched stay as they are.';
// A connection a read may be tried with: "connected", or "unknown" (a stored connection not verified since Cinema
// started; never the same as not connected). The read's own answer then decides.
export const usableConnection = connection => connection === 'connected' || connection === 'unknown';
const LIST_PROBLEM = {
  not_connected: s => `${s} is not connected. Connect it in Settings › Extensions › Accounts & Services.`,
  needs_attention: s => `${s}'s saved sign-in no longer works. Check it in Settings › Extensions › Accounts & Services.`,
  accounts_off: () => 'Accounts & Services is off, so your list could not be read.',
  not_available: s => `${s} has no connection available in Cinema.`,
  timed_out: s => `${s} took too long to answer. Try again.`,
  busy: () => 'Accounts & Services is busy. Try again in a moment.',
  cancelled: () => 'The read was cancelled.',
  provider_failed: s => `${s} could not give your list just now (a network problem, a rate limit or a sign-in that no longer works).`,
};
// Why an account list was NOT read: said as what happened, never as an empty list.
export function listProblemText(serviceLabel, code) {
  const s = serviceLabel || 'The service';
  return (LIST_PROBLEM[code] || (x => `Your ${x} list could not be read just now.`))(s);
}
export function listCaption(entry, serviceLabel) {
  const remote = `${serviceLabel}: ${entry.progress}${entry.total ? ' of ' + entry.total : ''} watched`;
  // Cinema's own record for the same installment, kept apart from the service's count (never merged into it).
  return entry.local != null ? `${remote} · On this device: episode ${entry.local} finished` : remote;
}
export function nextCaption(i, section, origin) {
  const what = section === 'next' ? `Episode ${i.episode}${i.part && i.part !== i.label ? ' · ' + i.part : ''}${i.returning ? ' (next part)' : ''}`
    : (i.at ? `Episode ${i.episode} airs ${new Date(i.at * 1000).toISOString().slice(0, 10)}` : `Episode ${i.episode} · not aired yet`);
  const from = origin === 'account' ? `From your ${i.service_label || 'list'}: ${i.remote?.progress ?? 0}${i.remote?.total ? ' of ' + i.remote.total : ''} watched` : 'Followed on this device';
  return `${what} · ${from}`;
}

// ---- The Lists screen ---------------------------------------------------------------------------------------------------
const callId = () => { const b = new Uint8Array(12);globalThis.crypto.getRandomValues(b);return Array.from(b, x => x.toString(16).padStart(2, '0')).join(''); };
export class ListsScreen {
  constructor(lists, titles, onChange = () => {}) {
    Object.assign(this, {lists, titles, onChange});
    this.state = {services: [], service: null, section: null, page: 1, items: [], hasNext: false, message: '', loading: false, opened: false};
    this.generation = 0;this.calls = [];this.closed = false;
  }
  // Every read this screen has in flight is withdrawn (both legs of Next up, or a list page).
  withdraw() { for (const call of this.calls) this.lists.cancel(call);this.calls = []; }
  async open(hint = null) {
    // The initial service lookup belongs to THIS opening: answered (or failed) after close or another open, it changes
    // nothing and reads nothing (review of bg).
    const gen = ++this.generation, mine = () => !this.closed && gen === this.generation;
    let services = [];
    try { services = ((await this.lists.read('watchlists', {})).services || []).filter(s => s.reads); }
    catch (error) { if (!mine()) return;this.state = {...this.state, opened: true, message: error.message || 'Lists could not be read.'};return this.onChange(); }
    if (!mine()) return;
    const service = services.find(s => s.connection === 'connected') || services.find(s => usableConnection(s.connection)) || services[0] || null;
    this.state = {...this.state, opened: true, services, service: service?.id || null, page: 1, confirm: null,
      section: hint === 'following' ? 'following' : usableConnection(service?.connection) ? 'current' : 'next'};
    return this.load(false);
  }
  choose(section) { if (!SECTION_LABEL[section]) return;this.state.section = section;return this.load(false); }
  chooseService(id) { if (!this.state.services.some(s => s.id === id)) return;this.state.service = id;return this.load(false); }
  more() { return this.load(true); }
  close() { this.closed = true;this.generation++;this.withdraw(); }
  async load(more = false) {
    if (this.closed) return;
    const st = this.state;
    if (more && (st.loading || !st.hasNext)) return;          // one More at a time, and only when there is a next page
    // A request generation: a tab, service or page change supersedes every earlier read; what each read asked for is
    // captured BEFORE it waits, and a stale answer (success or error) changes nothing.
    const gen = ++this.generation;
    this.withdraw();
    const want = {service: st.service, section: st.section, page: more ? st.page + 1 : 1};
    const current = () => !this.closed && gen === this.generation && st === this.state && st.service === want.service && st.section === want.section;
    st.loading = true;if (!more) { st.items = [];st.hasNext = false; }
    st.message = 'Loading…';this.onChange();
    const service = st.services.find(s => s.id === want.service);
    let items = [], message = '', hasNext = false;
    try {
      if (want.section === 'following') {
        const r = await this.lists.read('followed', {page: want.page});
        if (!current()) return;
        items = (r.items || []).map(f => ({title: f.label || 'Untitled', subtitle: f.available ? `Opens in ${f.provider_label || 'its video extension'}` : 'Its video extension is off or not installed',
          follow: f}));
        hasNext = (r.page || 1) < (r.pages || 1);
        if (!items.length && !more) message = 'You follow no series on this device. Follow one from its card.';
        else if (!more) message = `${r.total} ${r.total === 1 ? 'series' : 'series'} on this device. Choose one to open it or stop following it.`;
      } else if (LOCAL_SECTIONS.includes(want.section)) {
        const call = callId(), call2 = callId();
        this.calls = [call, call2];
        const fromAccount = usableConnection(service?.connection)
          ? this.lists.read('accountnext', {service: service.id, call: call2}).catch(error => ({items: [], error: error.message})) : Promise.resolve({items: []});
        const [account, local] = await Promise.all([fromAccount, this.lists.read('upnext', {call}).catch(error => ({items: [], error: error.message}))]);
        if (!current()) return;
        const state = want.section === 'next' ? 'next_aired' : 'upcoming', seen = [];
        // The same installment and episode from both the account and this device is one tile that says both. Matched by
        // a shared stated id (2026-11-21): an account row decided from the list alone has ids but no installment locator.
        const pairs = i => new Set(Object.entries(i.ids || {}).map(([k, v]) => `${k}:${v}`).concat(i.installment ? [`@${i.installment}`] : []));
        for (const [origin, list] of [['account', account.items || []], ['local', local.items || []]]) for (const i of list) {
          if (i.state !== state) continue;
          const mine = pairs(i), row = seen.find(s => s.episode === i.episode && [...mine].some(p => s.pairs.has(p)))?.made;
          if (row) { row.subtitle += ' · also followed on this device';continue; }
          const made = {title: i.label || 'Untitled', subtitle: nextCaption(i, want.section, origin), row: i, open: want.section === 'next' ? 'next' : 'title'};
          seen.push({pairs: mine, episode: i.episode, made});items.push(made);
        }
        const uncertain = (account.items || []).filter(i => i.state === 'uncertain' || i.state === 'unavailable').length;
        // An account list that was not READ is never "nothing new": only what was read is. A list read only in part keeps
        // its rows, names the lists not read, and says nothing is new only about what was read.
        const asked = usableConnection(service?.connection), readState = account.error ? 'refused' : (account.account?.state || 'ok');
        const unread = asked && readState !== 'ok';
        const empty = want.section === 'next'
          ? (asked && !unread ? 'Nothing new to watch from your list or the series you follow.' : 'Nothing new to watch from the series you follow on this device.')
          : (asked && !unread ? 'Nothing announced yet.' : 'Nothing announced yet for the series you follow on this device.');
        const missing = (account.account?.failed || []).map(f => SECTION_LABEL[f.status] || f.status);
        const partial = readState === 'partial' ? `Only part of your ${service.label} list could be read: ${missing.join(' and ')} could not be read. ${listProblemText(service.label, account.account.code)}` : '';
        message = [!items.length ? empty : '', unread ? (account.error || partial || listProblemText(service.label, account.account.code)) : '',
          uncertain ? `${uncertain} of your list's titles could not be placed exactly (not guessed).` : '', local.error || ''].filter(Boolean).join(' ');
      } else if (!service || !usableConnection(service.connection)) {
        message = service ? `Connect ${service.label} in Settings › Extensions › Accounts & Services to see this list. Connecting only reads it: nothing is tracked or sent.`
          : 'No list service can be read yet.';
      } else {
        const r = await this.lists.read('list', {service: service.id, kind: (service.kinds || [])[0] || null, status: want.section, page: want.page});
        if (!current()) return;
        if (r.state !== 'ok') message = listProblemText(service.label, r.code);
        items = (r.entries || []).map(e => ({title: e.title || 'Untitled', subtitle: listCaption(e, service.label), meta: [e.format, e.year].filter(Boolean).join(' · '),
          art: e.poster || null, ids: e.media?.ids || null}));
        hasNext = r.state === 'ok' && !!r.has_next;
        if (r.state === 'ok' && !items.length && !more) message = 'Nothing on this list.';
      }
    } catch (error) { if (!current()) return;message = error.message; }
    finally { if (gen === this.generation) st.loading = false; }
    st.page = want.page;
    st.items = more ? [...st.items, ...items] : items;st.hasNext = hasNext;st.message = message || (st.items.length ? '' : 'Nothing here.');
    this.onChange();
  }
  descriptor() {
    const st = this.state;
    const sections = [];
    const asked = st.confirm != null ? st.items[st.confirm] : null;
    if (st.services.length > 1) sections.push({heading: 'Service', items: st.services.map(s => ({kind: 'action', id: 'svc.' + s.id, label: s.label, tone: s.id === st.service ? 'primary' : 'normal'}))});
    sections.push({items: LIST_SECTIONS.map(([key, label]) => ({kind: 'action', id: 'sec.' + key, label, tone: key === st.section ? 'primary' : 'normal'}))});
    if (asked) sections.push({heading: `“${asked.title}”`, items: [{kind: 'text', text: STOP_NOTE, tone: 'muted'},
      ...(asked.follow.available ? [{kind: 'action', id: 'f.open', label: 'Open its title'}] : []),
      {kind: 'action', id: 'f.stop', label: 'Stop following', tone: 'danger'}, {kind: 'action', id: 'f.keep', label: 'Keep following'}]});
    sections.push({items: [{kind: 'list', id: 'titles', layout: LOCAL_SECTIONS.includes(st.section) || st.section === 'following' ? 'rows' : 'tiles', label: SECTION_LABEL[st.section] || null,
      items: st.items.map((e, i) => ({id: 'e' + i, title: e.title, subtitle: e.subtitle, meta: e.meta || null, art: e.art || null})),
      more: st.hasNext ? 'more' : null, moreLabel: 'More'}]});
    return {title: 'Lists', lead: st.message || null, status: st.loading ? {state: 'pending', text: 'Loading…'} : null, sections};
  }
  async stop(entry) {
    const st = this.state;
    try { await this.lists.read('unfollow', {key: entry.follow.key});this.onFollows?.();if (this.closed || st !== this.state) return;
      st.confirm = null;await this.load(false);st.message = `Not following “${entry.title}” any more. ${STOP_NOTE}`;this.onChange(); }
    catch (error) { if (this.closed || st !== this.state) return;st.message = error.message || 'That could not be done.';this.onChange(); }
  }
  act({id, item}) {
    const asked = this.state.confirm != null ? this.state.items[this.state.confirm] : null;
    if (id === 'f.keep') { this.state.confirm = null;return this.onChange(); }
    if (id === 'f.stop' && asked) return this.stop(asked);
    if (id === 'f.open' && asked) { this.state.confirm = null;return this.titles.open({title: asked.follow}); }
    if (id.startsWith('sec.')) { this.state.confirm = null;return this.choose(id.slice(4)); }
    if (id.startsWith('svc.')) return this.chooseService(id.slice(4));
    if (id === 'more') return this.more();
    if (id === 'titles' && item) {
      const index = Number(item.slice(1)), entry = this.state.items[index];if (!entry) return;
      if (entry.follow) { this.state.confirm = index;return this.onChange(); }
      if (entry.row) return this.titles.open({[entry.open]: entry.row});
      if (entry.ids) return this.titles.open({ids: entry.ids});
    }
  }
}

// ---- Home's Continue and Following rows (moved from the page core, 2026-11-17 review of bh) ------------------------------
// What a continuity item says and what choosing it does. Only an aired next episode opens an episode (then its sources:
// never a play); an upcoming, uncertain or caught-up work opens its title to browse; a work that could not be read does
// nothing.
const UNREAD = {provider_unavailable: provider => `${provider || 'Its video extension'} is off or not available.`,
  not_read: () => 'Not read in time; shown again next time.', timed_out: () => 'Not read in time; shown again next time.',
  cancelled: () => 'Not read in time; shown again next time.',
  busy: () => 'Cinema was busy reading other series; shown again next time.',
  provider_changed: () => 'Its video extension changed while it was read; shown again next time.'};
// Tiles of providers that are no longer registered, marked at once from the `following` answer (no provider is asked).
export function markGone(items, providers) {
  const present = new Set(providers || []);
  return (items || []).map(item => present.has(item.provider) ? item
    : {...item, state: 'unavailable', code: 'provider_unavailable', installment: undefined, ids: undefined, episode: undefined, stale: false});
}
export function upNextText(item) {
  const part = item.part && item.part !== item.label ? ` · ${item.part}` : '';
  const from = item.stale ? ' (from earlier)' : '';
  const title = item.label || 'Untitled';
  if (item.state === 'next_aired') return {title, badge: `Episode ${item.episode}`, line: `Next to watch${part}${from}`, action: 'episode'};
  if (item.state === 'upcoming') return {title, badge: 'Upcoming', line: `Upcoming: episode ${item.episode}${part}${item.at ? ' · ' + new Date(item.at * 1000).toISOString().slice(0, 10) : ''}${from}`, action: 'title'};
  if (item.state === 'caught_up') return {title, badge: 'Caught up', line: `You are caught up${from}`, action: 'title'};
  if (item.state === 'uncertain') return {title, badge: 'Your choice', line: `${item.reason}${from}`, action: 'title'};
  return {title, badge: 'Unavailable', line: (UNREAD[item.code] || (() => 'Could not be read right now.'))(item.provider_label), action: null};
}
export const FRESH_MS = 60000;   // an answer this recent is shown again without a new read
// The rows: read only while Home is shown (one bounded, cancellable read at a time); a late answer after Home was left,
// or superseded, changes nothing. Continue: next aired episodes; Following: the rest, then Manage following.
export class HomeRows {
  constructor(lists, titles, onChange = () => {}, now = () => Date.now()) {
    Object.assign(this, {lists, titles, onChange, now});
    this.state = {follows: 0, items: null, error: null, shown: 0, lifecycle: null, loadedAt: 0};
    this.generation = 0;this.call = null;this.visible = false;this.stale = false;
  }
  withdraw() { if (this.call) this.lists.cancel(this.call);this.call = null; }
  show() { this.visible = true;return this.refresh(); }
  hide() { this.visible = false;this.generation++;this.withdraw(); }
  changed() { this.stale = true;this.refresh(); }   // a follow changed (this module's own action): the count at once, rows when shown
  async refresh() {
    const gen = ++this.generation, mine = () => gen === this.generation, st = this.state;
    let f;
    try { f = await this.lists.read('following', {}); } catch (_) { f = {follows: 0, providers: [], lifecycle: null}; }
    if (!mine()) return;
    if (!f.follows) { Object.assign(st, {follows: 0, items: null, error: null});return this.onChange(); }
    st.follows = f.follows;
    if (st.items && f.lifecycle !== st.lifecycle) { st.items = markGone(st.items, f.providers);st.loadedAt = 0;this.onChange(); }
    if (!this.stale && st.items && this.now() - st.loadedAt < FRESH_MS) return this.onChange();
    if (!this.visible) return this.onChange();
    this.withdraw();const call = callId();this.call = call;this.stale = false;this.onChange();
    try {
      const a = await this.lists.read('upnext', {call});
      if (!mine()) return;
      Object.assign(st, {items: a.items, follows: a.follows, shown: a.shown, lifecycle: a.lifecycle, error: null, loadedAt: this.now()});
    } catch (error) {
      if (!mine()) return;
      if (error.code === 'watchlists_off') Object.assign(st, {follows: 0, items: null});
      else if (error.code === 'watchlists_update') st.error = error.message;
      else if (error.code !== 'cancelled') st.error = st.items ? null : 'Your followed series could not be read right now.';
    } finally { if (this.call === call) this.call = null; }
    this.onChange();
  }
  descriptor() {
    const st = this.state;
    if (!st.follows) return {sections: []};
    const manage = {more: 'manage', moreLabel: 'Manage following', moreNote: `${st.follows} series on this device`};
    // Still reading (or unreadable): Following already offers Manage following, so Home has these rows at once.
    if (!st.items) return {status: {state: st.error ? 'error' : 'pending', text: st.error || 'Reading your followed series…'},
      sections: [{heading: 'Continue', items: [{kind: 'text', text: st.error || 'Reading your followed series…', tone: st.error ? 'warning' : 'muted'}]},
        {heading: 'Following', items: [{kind: 'list', id: 'following', layout: 'tiles', items: [], ...manage}]}]};
    const tile = (item, i) => { const x = upNextText(item);
      return {id: 'i' + i, title: x.title, badge: x.badge, subtitle: x.line, meta: item.provider_label || null, tone: item.state === 'next_aired' ? 'normal' : 'muted', disabled: !x.action}; };
    const indexed = st.items.map((item, i) => [item, i]);
    const next = indexed.filter(([x]) => x.state === 'next_aired'), other = indexed.filter(([x]) => x.state !== 'next_aired');
    const sections = [];
    if (next.length) sections.push({heading: 'Continue', items: [{kind: 'list', id: 'continue', layout: 'tiles', items: next.map(([x, i]) => tile(x, i))}]});
    const following = [{kind: 'list', id: 'following', layout: 'tiles', items: other.map(([x, i]) => tile(x, i)), ...manage}];
    if (st.follows > st.shown) following.push({kind: 'text', text: `Showing the ${st.shown} series you followed most recently of ${st.follows}.`, tone: 'muted'});
    sections.push({heading: 'Following', items: following});
    return {sections};
  }
  act({id, item}, showFollowing) {
    if (id === 'manage') return showFollowing();
    const row = item ? this.state.items?.[Number(item.slice(1))] : null;if (!row) return;
    const action = upNextText(row).action;
    if (action === 'episode') return this.titles.open({next: row});
    if (action === 'title') return this.titles.open({title: row});
  }
}

// ---- Title actions: Follow and Tracking ----------------------------------------------------------------------------------
export function destinationLabel(t) {
  const d = t?.current?.destination;
  return d === 'none' ? 'None' : d ? (t.suggestions?.find(s => s.service === d)?.label || d) : 'Not chosen';
}
export function titleActions(t) {
  if (!t || t.off) return [];
  const choices = [['unset', 'Not chosen', ''], ['none', 'None', 'none'], ...(t.suggestions || []).filter(s => s.compatible).map(s => [s.service, s.label, s.service])];
  return [t.followed ? {id: 'unfollow', label: 'Stop following'} : {id: 'follow', label: 'Follow'},
    {id: 'tracking', label: `Tracking: ${destinationLabel(t)}`, note: [t.current?.explanation, t.note].filter(Boolean).join(' '),
      choices: choices.map(([id, label, value]) => ({id, label, checked: (t.current?.destination || '') === value}))}];
}

let current = null;
export function mount(cinema) {
  if (!cinema.titles || !cinema.lists || !cinema.surfaces) return;   // an older Cinema: nothing of this module's shows
  const known = new Map();   // title ref -> what this module read for it (bounded)
  let alive = true;
  const handle = cinema.titles.actions(title => titleActions(known.get(title.ref)), {
    async onOpen(title) {
      if (!title.media || known.has(title.ref)) return;
      try {
        const [suggested, destination] = await Promise.all([cinema.titles.call(title.ref, 'suggest'), cinema.titles.call(title.ref, 'destination')]);
        known.set(title.ref, {suggestions: suggested.suggestions || [], note: suggested.note || '', followed: !!suggested.followed, key: suggested.key || null, current: destination});
      } catch (error) { known.set(title.ref, {off: true}); }
      while (known.size > 32) known.delete(known.keys().next().value);
      if (alive) handle.changed();
    },
    async onAction({title, id, choice}) {
      const t = known.get(title);if (!t || t.off) return;
      try {
        if (id === 'follow') { const r = await cinema.titles.call(title, 'follow');Object.assign(t, {followed: true, key: r.key || t.key});cinema.titles.status(title, 'Following this series.');home?.changed(); }
        else if (id === 'unfollow' && t.key) { await cinema.titles.call(title, 'unfollow', {key: t.key});t.followed = false;cinema.titles.status(title, 'Not following any more. Only the follow on this device went.');home?.changed(); }
        else if (id === 'tracking' && choice) { t.current = await cinema.titles.call(title, 'bind', {destination: choice === 'unset' ? null : choice});cinema.titles.status(title, t.current?.explanation || ''); }
        else return;
      } catch (error) { cinema.titles.status(title, error.message || 'That did not work.');return; }
      if (alive) handle.changed();
    },
  });
  // Home's rows (Continue, Following): this module's own, drawn by the skin on Home.
  let homeHandle = null;
  const home = cinema.surfaces.home ? new HomeRows(cinema.lists, cinema.titles, () => { if (alive && homeHandle) homeHandle.update(home.descriptor()); }) : null;
  if (home) {
    homeHandle = cinema.surfaces.home(home.descriptor(), {onAction: p => home.act(p, () => view?.show?.('following')), onOpen: () => home.show(), onClose: () => home.hide()});
    home.refresh();   // the count at once, so a Home that has only these rows can be the start view
  }
  // The Lists screen: a view in the page's navigation whose content the skin draws.
  let screen = null, view = null;
  const draw = () => { if (alive && view && screen) view.update(screen.descriptor()); };
  const fresh = () => { const s = new ListsScreen(cinema.lists, cinema.titles, draw);s.onFollows = () => home?.changed();return s; };
  screen = fresh();
  view = cinema.surfaces.view({label: 'Lists', placement: 'page'}, screen.descriptor(), {   // a full page, as Lists always was
    onAction: payload => screen.act(payload),
    onOpen: hint => { screen.close();screen = fresh();draw();screen.open(hint); },
    onClose: () => screen.close(),
  });
  current = {stop() { alive = false;screen?.close();home?.hide(); }};
}
export function unmount() { current?.stop();current = null; }
