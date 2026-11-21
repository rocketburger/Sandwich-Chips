// Accounts & Services, its Settings screen (2026-11-17, D1 of the modularity audit). This module owns its words, its
// screens and its policy: which connections exist, what a credential is called and how long it may be, when sign-in,
// "Your list", Check now or Disconnect are offered, and what every outcome means. It publishes them as SEMANTIC surfaces
// (cinema.surface/1); the active skin draws them; the page places them in Settings › Extensions › Accounts & Services and
// owns focus, keys and the secret field. Moved here unchanged in meaning from the page core (web/app.js and
// web/settings-model.mjs, alpha bf), which no longer knows anything about Accounts.
//
// The typed credential goes from the page's secret field straight to this module's declared Settings action (connect):
// read once, cleared at once, never kept here, never shown again; the bridge refuses an answer that echoes it.

// ---- Words (owned here) -----------------------------------------------------------------------------------------------
const CONNECTION_TEXT = {connected: 'Connected', not_connected: 'Not connected', needs_attention: 'Needs attention', unknown: 'Connection not known'};
const OPERATION_TEXT = {'account.status': 'connection status', 'debrid.availability': 'availability check'};
const ENTITLEMENT_TEXT = {premium: 'Premium', not_premium: 'Not premium (a free or expired account)', unknown: 'Not known'};
const ATTEMPT_TEXT = {
  verifying: 'Checking the key with the service…', saving: 'Saving the connection…', connected: 'Connected',
  awaiting_token: 'Waiting for the token from the service (sign-in open in your browser)',
  rejected: {authentication_failed: 'The service did not accept this key. Check it and try again.',
             token_expired: 'This token has expired. Get a new one and try again.',
             permission_denied: 'The service accepted the key but does not allow this account to be used (for example an unconfirmed account).'},
  failed: {rate_limited: 'The service is busy (too many requests). Try again in a moment.', network: 'The service could not be reached. Check the connection and try again.',
           service_error: 'The service had a problem. Try again later.', bad_response: 'The service gave an answer Cinema does not understand. Nothing was changed.',
           save_failed: 'The key was accepted but could not be saved. Your previous connection, if any, is unchanged.',
           stopped: 'Accounts & Services stopped before the key was saved. Nothing was changed.', internal: 'Something went wrong. Nothing was changed.'},
  cancelled: 'Cancelled. Nothing was changed.',
  uncertain: 'The key may or may not have been saved. Choose Check now to see what Cinema has.',
};
const KEY_SHAPE = 'That does not look like an API key (letters, digits, - and _ only, 8 to 128 characters).';
const OUTCOME_TEXT = {
  invalid_key: KEY_SHAPE,
  already_pending: 'A connection is already being checked.', stopped: 'Accounts & Services is stopping. Nothing was changed.',
  unsupported: 'This connection cannot be made here.', nothing_pending: 'Nothing was being connected.',
  disconnected: 'Disconnected. Cinema no longer keeps the key.', not_connected: 'There was no connection to remove.',
  failed: 'The key could not be removed just now. Check now to see what Cinema has.', already_checking: 'Already checking.',
  browser_opened: 'Opened in your browser. Approve Cinema there, then paste the token it shows.',
  not_registered: 'Sign-in is not available in this Cinema yet (its own app is not registered with the service). Paste a token instead.',
};
// What each of this module's providers' credentials is called, its length, and what the connection is used for.
const CREDENTIALS = {
  anilist: {noun: 'access token', maxLength: 2048, entitlement: false, watchlist: true,
            lead: label => `Paste the ${label} access token: the one ${label} shows after you approve Cinema (Sign in with ${label}), or one you obtained yourself. Cinema checks it with ${label} (it asks only who the token belongs to), keeps it encrypted for your Windows account, and never shows it again. Tokens last about a year; Cinema asks you to sign in again when one stops working.`,
            invalid: 'That does not look like an AniList access token (three parts separated by dots).',
            expired: 'The saved token no longer works (it expired or was revoked). Connect again with a new token, or disconnect.',
            usedBy: 'Cinema only: it can read your list, never change it. Your add-ons keep their own AniList login.',
            disconnect: label => `Cinema removes the token it keeps. The token itself stays valid at ${label} until it expires or you revoke it there; your add-ons keep their own login.`},
};
const API_KEY = {noun: 'API key', maxLength: 256, entitlement: true,
  lead: label => `Paste the API key from your ${label} account page. Cinema checks it with ${label}, keeps it encrypted for your Windows account, and never shows it again.`,
  invalid: KEY_SHAPE, expired: 'The saved key no longer works. Connect again with a new key, or disconnect.',
  usedBy: 'Cinema\'s video extensions, to check what is cached and to play; an add-on only where you turn that on below. Otherwise add-ons keep their own accounts',
  disconnect: label => `Cinema removes the API key it keeps. Nothing changes at ${label}. Cinema's video extensions can no longer check or play through it, and add-ons you set to use it stop doing so; add-ons with their own ${label} login keep it.`};
export const credentialFor = provider => CREDENTIALS[provider] || API_KEY;
const WATCH_TYPES = [['anime', 'Anime'], ['manga', 'Manga']];
const WATCH_STATUSES = [['current', {anime: 'Watching', manga: 'Reading'}], ['planning', 'Planning'], ['completed', 'Completed'],
  ['paused', 'Paused'], ['dropped', 'Dropped'], ['repeating', {anime: 'Rewatching', manga: 'Rereading'}]];
const statusLabel = (status, type) => { const f = WATCH_STATUSES.find(([s]) => s === status), l = f ? f[1] : status;return typeof l === 'object' ? l[type] || l.anime : String(l); };
const typeLabel = type => (WATCH_TYPES.find(([t]) => t === type) || [type, type])[1];
const WATCH_OUTCOME_TEXT = {not_connected: 'The account is not connected.', needs_attention: 'The saved token no longer works. Connect again first.',
  not_supported: 'This connection has no list.', busy: 'Accounts & Services is busy. Try again in a moment.',
  stopped: 'Accounts & Services is stopping.', refused: 'The list could not be asked for just now.'};
function watchResultText(state, code, label) {
  if (state === 'cancelled') return code === 'module_stopped' ? 'Accounts & Services was turned off. Nothing was read.' : 'Cancelled.';
  if (state === 'unknown') return 'Cinema no longer has this read (Accounts & Services was restarted). Read again.';
  return {timed_out: `${label} took too long to answer. Try again.`, busy: 'Accounts & Services is busy. Try again in a moment.',
    provider_failed: `${label} could not give the list (for example a rate limit, a network problem, or a token that no longer works). Check now, or try again later.`,
    invalid_result: `${label} answered something Cinema does not show. Nothing was shown.`}[code] || 'The list could not be read just now.';
}
const DETAIL_REASON = {too_large: 'larger than an account answer can be', not_json: 'not readable as data', not_object: 'not the expected kind of data',
  redirect: 'a redirect (not followed)', graphql_errors: 'an error message from the service', no_viewer: 'no account in the answer',
  viewer_id: 'no usable account number', viewer_name: 'no account name', http_status: 'an unexpected status',
  edge_refused: 'refused at the network edge in front of the service, before reaching it'};
const usable = connection => connection === 'connected' || connection === 'unknown';
export function connectionText(view) {
  if (!view) return 'Not known';
  if (view.checking) return 'Checking…';
  if (view.attempt?.state === 'verifying' || view.attempt?.state === 'saving') return 'Connecting…';
  return CONNECTION_TEXT[view.connection] || CONNECTION_TEXT.unknown;
}
function attemptText(attempt) {
  if (!attempt || attempt.state === 'idle') return '';
  const said = ATTEMPT_TEXT[attempt.state];
  if (typeof said === 'string') return said;
  return (said && said[attempt.code]) || (attempt.state === 'rejected' ? 'The service did not accept this key.' : 'That did not work. Nothing was changed.');
}
function attemptDetail(attempt) {
  const d = attempt && typeof attempt.detail === 'object' ? attempt.detail : null;
  if (!d) return '';
  const size = Number.isInteger(d.bytes) ? (d.reason === 'too_large' ? `more than ${d.bytes - 1} bytes` : `${d.bytes} bytes`) : '';
  return [Number.isInteger(d.http) ? `HTTP ${d.http}` : '', typeof d.media === 'string' ? d.media : '', size, DETAIL_REASON[d.reason] || ''].filter(Boolean).join(' · ');
}
export function accountFacts(view) {
  if (!view) return [{label: 'Connection', value: 'Not known'}];
  const cred = credentialFor(view.provider), facts = [{label: 'Connection', value: connectionText(view)}];
  if (view.connection === 'connected' && cred.entitlement) facts.push({label: 'Account', value: ENTITLEMENT_TEXT[view.entitlement] || ENTITLEMENT_TEXT.unknown});
  if (view.connection === 'needs_attention') facts.push({label: 'What to do', value: cred.expired});
  const last = attemptText(view.attempt);
  if (last && view.attempt.state !== 'connected') facts.push({label: 'Last attempt', value: last});
  const details = attemptDetail(view.attempt);
  if (details && view.attempt.state !== 'connected') facts.push({label: 'Details', value: details});
  facts.push({label: 'Used by', value: cred.usedBy});
  return facts;
}
export function statusFacts(answer, failed = false) {
  if (failed || !answer) return [{label: 'Status', value: 'Unknown: it could not be read just now'}];
  const said = {off: 'Off: turn it on to see its status', not_running: 'Not running just now', replaced: 'It was restarted: refresh to read its status again',
    busy: 'Busy: refresh to try again', unknown: 'Unknown: it could not be read just now'};
  if (answer.state !== 'ok' || !answer.status) return [{label: 'Status', value: said[answer.state] || said.unknown}];
  const s = answer.status, facts = [];
  if (s.foundation_only === false) facts.push({label: 'Stage', value: 'Accounts can be connected below. Cinema\'s video extensions use them; an add-on only where you turn that on'});
  facts.push({label: 'Providers', value: s.state === 'no_providers' ? 'None registered' : `${s.providers.length}${s.truncated ? ' shown (more are registered)' : ' registered'}`});
  for (const p of s.providers || []) {
    const name = p.label || 'Unnamed provider';
    if (p.state !== 'ok') { facts.push({label: name, value: p.state === 'busy' ? 'Busy just now' : 'Not answering'});continue; }
    const ops = (p.operations || []).map(op => OPERATION_TEXT[op]).filter(Boolean);
    facts.push({label: name, value: `${CONNECTION_TEXT[p.connection] || CONNECTION_TEXT.unknown} · ${ops.length ? `offers ${ops.join(', ')}` : 'offers nothing yet'}`});
  }
  if (s.history_writes === false) facts.push({label: 'History', value: 'Never written by this module'});
  return facts;
}
export function actionProblem(answer) {
  const said = {off: 'Accounts & Services is off. Turn it on to connect an account.', not_running: 'Accounts & Services is not running just now.',
    replaced: 'Accounts & Services was restarted. Open it again.', busy: 'Accounts & Services is busy. Try again.',
    unknown: 'Accounts & Services did not answer just now. Nothing was changed.'};
  if (!answer || typeof answer !== 'object') return said.unknown;
  return answer.state === 'ok' ? '' : (said[answer.state] || said.unknown);
}

// ---- The screens, as semantic surfaces ------------------------------------------------------------------------------
const text = (t, tone = 'normal') => ({kind: 'text', text: t, tone});
const act = (id, label, note, tone) => ({kind: 'action', id, label, note: note || null, tone: tone || 'normal'});
export function surfaceOf(s) {
  const entry = s.providers?.find(p => p.provider === s.provider) || null, view = entry?.view || null, label = entry?.label || 'Account', cred = credentialFor(s.provider);
  if (s.screen === 'key') return {title: `${label} ${cred.noun}`, lead: s.error || cred.lead(label), sections: [{items: [
    {kind: 'secret', id: 'key', label: `${label} ${cred.noun}`, max: cred.maxLength, submit: 'connect'},
    act('connect', 'Connect', `Checks the ${cred.noun}, then saves it`, 'primary'), act('key-cancel', 'Cancel', 'Nothing is sent')]}]};
  if (s.screen === 'confirm') return {title: `Disconnect ${label}?`, lead: cred.disconnect(label), sections: [{items: [
    act('confirm', 'Disconnect', `Removes the ${cred.noun} from Cinema`, 'danger'), act('keep', 'Keep the connection')]}]};
  if (s.screen === 'list') {
    const w = s.watch;
    if (!w) return {title: `Your ${label} list`, sections: [{items: [act('back', 'Back')]}]};
    const where = `${typeLabel(w.media_type)} · ${statusLabel(w.status, w.media_type)} · page ${w.page}`;
    const lead = w.state === 'loading' ? `Reading ${where}…` : w.message ? `${w.message} (${where})` : w.state === 'empty' ? `Nothing here (${where}).`
      : `${where}: ${w.entries.length} ${w.entries.length === 1 ? 'title' : 'titles'}${w.has_next ? ' on this page' : ''}. Read-only: Cinema never changes your list.${w.truncated ? ' Cinema shows the first 20 pages (1000 entries) only.' : ''}`;
    const items = [
      {kind: 'choice', id: 'type', label: 'Type', value: w.media_type, options: WATCH_TYPES.map(([value, l]) => ({value, label: l}))},
      {kind: 'choice', id: 'status', label: 'List', value: w.status, options: WATCH_STATUSES.map(([value]) => ({value, label: statusLabel(value, w.media_type)}))}];
    if (w.state === 'loading') items.push(act('watch-cancel', 'Cancel reading'));
    if (w.state === 'done') items.push(text('Lists (Watchlists) shows your lists as titles you can open.', 'muted'));
    if (w.state !== 'loading' && w.page > 1) items.push(act('prev', 'Previous page'));
    if (w.state === 'done' && w.has_next) items.push(act('next', 'Next page'));
    if (w.state === 'error' || w.state === 'cancelled') items.push(act('again', 'Read again'));
    items.push(act('back', 'Back'));
    return {title: `Your ${label} list`, lead, status: w.state === 'loading' ? {state: 'pending', text: 'Reading…'} : null, sections: [{items}]};
  }
  if (s.screen === 'account') {
    const lead = s.error || s.note || entry?.problem || '';
    if (!view) return {title: label, lead, sections: [{items: [{kind: 'facts', rows: accountFacts(null)}, act('reload', 'Read again'), act('back', 'Back')]}]};
    const items = [{kind: 'facts', rows: accountFacts(view)}];
    const connecting = view.attempt.state === 'verifying' || view.attempt.state === 'saving';
    if (view.attempt.state === 'awaiting_token') {
      return {title: label, lead: `${label} is open in your browser. Approve Cinema there; ${label} then shows a token. Paste it here: Cinema checks who it belongs to before keeping it. Cinema waits up to 10 minutes.`,
        sections: [{items: [...items, act('key', `Paste the token from ${label}`, 'Checks it, then saves it', 'primary'), act('cancel', 'Cancel', 'Stops waiting; nothing is kept'), act('back', 'Back')]}]};
    }
    const timedOut = view.attempt.state === 'cancelled' && view.attempt.code === 'timed_out' && !lead
      ? 'The sign-in waited 10 minutes without a token, so Cinema stopped waiting. Nothing was kept. Sign in again when you are ready.' : '';
    if (connecting) items.push(act('cancel', 'Cancel connecting', 'Stops before the key is saved'));
    else {
      if (entry?.sign_in) items.push(act('authorize', view.connection === 'connected' ? `Sign in to ${label} again` : `Sign in with ${label}`, `Opens ${label} in your browser to approve Cinema`));
      items.push(act('key', view.connection === 'not_connected' ? `Connect with an ${cred.noun}` : `Use another ${cred.noun}`, entry?.sign_in ? 'If you already have one' : '',
        view.connection === 'not_connected' ? 'primary' : 'normal'));
    }
    if (!connecting && usable(view.connection) && cred.watchlist) items.push(act('list', 'Your list', 'Read-only: Cinema never changes it'));
    if (!connecting && view.connection !== 'not_connected') { items.push(act('refresh', view.checking ? 'Checking…' : 'Check now', `Asks ${label} again`));items.push(act('disconnect', 'Disconnect', 'Asks first')); }
    items.push(act('back', 'Back'));
    return {title: label, lead: lead || timedOut, sections: [{items}]};
  }
  // The home screen of this module's part: its declared status and its connections.
  const status = [{kind: 'facts', rows: statusFacts(s.status, s.statusFailed)}, act('status', s.statusLoading ? 'Refreshing…' : 'Refresh status')];
  const connections = [];
  if (!s.providers && s.loading) connections.push(text('Reading connections…', 'muted'));
  else if (s.problem && !(s.providers || []).length) connections.push(text(s.problem, 'warning'));
  else if (!(s.providers || []).length) connections.push(text('No account can be connected here yet.', 'muted'));
  else connections.push({kind: 'list', id: 'providers', items: s.providers.map(p => ({id: p.provider, title: p.label, subtitle: p.view ? connectionText(p.view) : (p.problem || 'Not known')}))});
  connections.push(text('Cinema keeps these connections for you. Its video extensions use them; an add-on uses one only where you turn that on.', 'muted'));
  return {sections: [{heading: 'Status', items: status}, {heading: 'Connections', items: connections}]};
}

// ---- The state machine (moved from the page core; same guards) -----------------------------------------------------
let current = null;
export function mount(cinema) {
  if (!cinema.surfaces || !cinema.settings) return;   // an older Cinema: its Settings screen stays as it was
  const s = {screen: 'home', provider: null, providers: null, loading: true, problem: null, status: null, statusFailed: false, statusLoading: false,
    note: null, error: null, acting: 0, watch: null};
  let epoch = 0, polls = 0, pollTimer = null, wticket = 0, wtimer = null, commands = 0, alive = true;
  const timers = cinema.timers;
  const gone = error => String(error?.message || error).includes('unloaded');
  const handle = cinema.surfaces.settings(surfaceOf(s), {onAction, onBack});
  const draw = () => { if (alive) handle.update(surfaceOf(s)); };
  const own = mine => alive && mine === epoch;
  current = {stop() { alive = false;epoch++;if (pollTimer) timers.clear(pollTimer);if (wtimer) timers.clear(wtimer);dropWatch(); }};

  let statusStarts = 0;
  async function readStatus() {
    s.statusLoading = true;draw();
    try { s.status = await cinema.settings.status();s.statusFailed = false; } catch (error) { if (gone(error)) return;s.statusFailed = true; }
    if (s.status?.state === 'not_running' && ++statusStarts <= 10 && alive) { timers.setTimeout(() => { if (alive) readStatus(); }, 1000); }
    s.statusLoading = false;draw();
  }
  let starts = 0;   // just turned on: the backend may still be starting (not_running): asked again, at most 10 times
  async function loadConnections() {
    const mine = ++epoch;s.loading = true;s.problem = null;draw();
    try {
      const listed = await cinema.settings.act('list', {});
      if (!own(mine)) return;
      if (listed?.state === 'not_running' && ++starts <= 10) { timers.setTimeout(() => { if (own(mine)) loadConnections(); }, 1000);return; }
      if (listed?.state !== 'ok') throw Object.assign(Error('not ok'), {said: actionProblem(listed)});
      const providers = [];
      for (const p of listed.value.providers) {
        const st = await cinema.settings.act('state', {provider: p.provider});
        if (!own(mine)) return;
        providers.push({...p, view: st?.state === 'ok' ? st.value : null, problem: st?.state === 'ok' ? '' : actionProblem(st)});
      }
      s.providers = providers;s.loading = false;
    } catch (error) { if (gone(error) || !own(mine)) return;s.loading = false;s.providers = s.providers || [];s.problem = error.said || actionProblem(null); }
    draw();schedulePoll(mine);
  }
  const pending = view => !!view && (view.checking || view.attempt.state === 'verifying' || view.attempt.state === 'saving');
  function schedulePoll(mine) {
    if (pollTimer) timers.clear(pollTimer);
    if (!own(mine) || !(s.providers || []).some(p => pending(p.view)) || ++polls > 60) { polls = 0;return; }
    pollTimer = timers.setTimeout(() => poll(mine), 700);
  }
  async function poll(mine) {
    for (const p of s.providers || []) {
      if (!own(mine)) return;
      if (!pending(p.view)) continue;
      try {
        const st = await cinema.settings.act('state', {provider: p.provider});
        if (!own(mine)) return;
        if (st?.state === 'ok') Object.assign(p, {view: st.value, problem: ''});
        else { p.problem = actionProblem(st);if (st?.state === 'replaced' || st?.state === 'off') return loadConnections(); }
      } catch (error) { if (gone(error)) return; }
    }
    draw();schedulePoll(mine);
  }
  function command(action, args = {}, after) {
    const provider = s.provider, mine = epoch, token = ++commands;
    if (!provider) return;
    s.acting = token;s.error = null;
    cinema.settings.act(action, {provider, ...args}).then(answer => {
      if (!own(mine)) return;
      if (answer?.state !== 'ok') { if (s.provider === provider) s.error = actionProblem(answer);if (answer?.state === 'replaced' || answer?.state === 'off') loadConnections();return; }
      const p = s.providers?.find(x => x.provider === provider);if (p) Object.assign(p, {view: answer.value, problem: ''});
      if (s.provider === provider) { s.note = (answer.value.outcome === 'invalid_key' ? credentialFor(provider).invalid : OUTCOME_TEXT[answer.value.outcome]) || '';s.error = null; }
      after?.(answer.value);schedulePoll(mine);
    }).catch(error => { if (!gone(error) && s.provider === provider) s.error = actionProblem(null); })
      .finally(() => { if (s.acting === token) s.acting = 0;draw(); });
  }
  // The read-only list (one read at a time, owned by a ticket; a late answer changes nothing).
  function dropWatch() {
    const w = s.watch;wticket++;if (wtimer) timers.clear(wtimer);
    if (w?.request && w.state === 'loading') cinema.settings.act('watchlist_cancel', {provider: w.provider, request: w.request}).catch(() => {});
  }
  const watchOwn = ticket => alive && wticket === ticket && s.screen === 'list' && s.watch?.provider === s.provider;
  function readWatch(change = {}) {
    const prev = s.watch?.provider === s.provider ? s.watch : null;dropWatch();
    const q = {provider: s.provider, media_type: prev?.media_type || WATCH_TYPES[0][0], status: prev?.status || 'current', page: prev?.page || 1, ...change};
    const ticket = wticket;
    s.watch = {...q, state: 'loading', request: null, entries: [], has_next: false, truncated: false, message: '', polls: 0};draw();
    cinema.settings.act('watchlist', q).then(answer => {
      if (!watchOwn(ticket)) {
        if (answer?.state === 'ok' && answer.value?.outcome === 'started' && answer.value.request) cinema.settings.act('watchlist_cancel', {provider: q.provider, request: answer.value.request}).catch(() => {});
        return;
      }
      if (answer?.state !== 'ok') { s.watch = {...s.watch, state: 'error', message: actionProblem(answer)};return draw(); }
      if (answer.value.outcome !== 'started') { s.watch = {...s.watch, state: 'error', message: WATCH_OUTCOME_TEXT[answer.value.outcome] || WATCH_OUTCOME_TEXT.refused};return draw(); }
      s.watch = {...s.watch, request: answer.value.request};scheduleWatch(ticket);
    }).catch(error => { if (!gone(error) && watchOwn(ticket)) { s.watch = {...s.watch, state: 'error', message: actionProblem(null)};draw(); } });
  }
  function scheduleWatch(ticket) {
    if (wtimer) timers.clear(wtimer);
    if (!watchOwn(ticket) || s.watch.state !== 'loading') return;
    if (++s.watch.polls > 40) { s.watch = {...s.watch, state: 'error', message: watchResultText('refused', 'timed_out', labelOf())};return draw(); }
    wtimer = timers.setTimeout(() => pollWatch(ticket), 400);
  }
  async function pollWatch(ticket) {
    if (!watchOwn(ticket) || !s.watch.request || s.watch.state !== 'loading') return;
    const asked = s.watch;let answer;
    try { answer = await cinema.settings.act('watchlist_result', {provider: asked.provider, request: asked.request}); } catch (error) { if (!gone(error)) scheduleWatch(ticket);return; }
    if (!watchOwn(ticket) || s.watch.request !== asked.request) return;
    if (answer?.state !== 'ok') { s.watch = {...s.watch, state: 'error', message: actionProblem(answer)};return draw(); }
    const v = answer.value;
    if (v.request !== asked.request || (v.state !== 'unknown' && (v.media_type !== asked.media_type || v.status !== asked.status || v.page !== asked.page))) return;
    if (v.state === 'pending') return scheduleWatch(ticket);
    if (v.state === 'done') s.watch = {...s.watch, state: v.value.entries.length ? 'done' : 'empty', entries: v.value.entries, has_next: v.value.has_next, truncated: v.value.truncated, message: ''};
    else s.watch = {...s.watch, state: v.state === 'cancelled' ? 'cancelled' : 'error', message: watchResultText(v.state, v.code, labelOf())};
    draw();
  }
  const labelOf = () => s.providers?.find(p => p.provider === s.provider)?.label || 'The service';

  function onAction({id, item, value, secret}) {
    if (s.screen === 'home') {
      if (id === 'status') return readStatus();
      if (id === 'providers' && item) { s.screen = 'account';s.provider = item;s.note = null;s.error = null;return draw(); }
      return;
    }
    if (s.screen === 'key') {
      if (id === 'connect') {
        const key = typeof secret === 'string' ? secret : '';
        if (!key.trim()) { s.error = `Paste the ${credentialFor(s.provider).noun} first.`;return draw(); }
        s.screen = 'account';s.note = 'Checking the key…';command('connect', {key});return draw();
      }
      if (id === 'key-cancel') { s.screen = 'account';s.error = null;return draw(); }
      return;
    }
    if (s.screen === 'confirm') {
      if (id === 'confirm') { s.screen = 'account';s.note = 'Disconnecting…';command('disconnect');return draw(); }
      if (id === 'keep') { s.screen = 'account';return draw(); }
      return;
    }
    if (s.screen === 'list') {
      if (id === 'type' && value) return readWatch({media_type: value, page: 1});
      if (id === 'status' && value) return readWatch({status: value, page: 1});
      if (id === 'watch-cancel') {
        const w = s.watch;if (!w || w.state !== 'loading') return;
        if (!w.request) { wticket++;s.watch = {...w, state: 'cancelled', message: 'Cancelled.'};return draw(); }
        const ticket = wticket;cinema.settings.act('watchlist_cancel', {provider: w.provider, request: w.request}).then(() => pollWatch(ticket)).catch(() => {});return;
      }
      if (id === 'prev') return readWatch({page: Math.max(1, s.watch.page - 1)});
      if (id === 'next') return readWatch({page: s.watch.page + 1});
      if (id === 'again') return readWatch();
      if (id === 'back') return onBack();
      return;
    }
    // account
    if (id === 'back') return onBack();
    if (id === 'reload') return loadConnections();
    if (id === 'key') { s.screen = 'key';s.error = null;return draw(); }
    if (id === 'authorize') return command('authorize');
    if (id === 'cancel') return command('cancel');
    if (id === 'refresh') { if (!s.acting) command('refresh');return; }
    if (id === 'disconnect') { s.screen = 'confirm';return draw(); }
    if (id === 'list') { s.screen = 'list';return readWatch(); }
  }
  // Back: up one level inside this module's part (true), or out of it at its home screen (false: the page goes back).
  function onBack() {
    if (s.screen === 'key' || s.screen === 'confirm') { s.screen = 'account';s.error = null;draw();return true; }
    if (s.screen === 'list') { dropWatch();s.watch = null;s.screen = 'account';s.note = null;s.error = null;draw();return true; }
    if (s.screen === 'account') { s.screen = 'home';s.provider = null;s.note = null;s.error = null;draw();return true; }
    return false;
  }
  readStatus();loadConnections();
}
export function unmount() { current?.stop();current = null; }
