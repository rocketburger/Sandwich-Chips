"""Accounts & Services (cinema.accounts 0.1.0): FOUNDATION ONLY.

Provides the platform-neutral cinema.accounts/1 contract (cinema_accounts_contract) in the existing service registry
and delegates allowlisted requests to account providers registered as cinema.accounts.provider/1. It ships no provider,
reads no account, add-on, setting or profile file, stores nothing, and reaches no network: with no provider registered
it reports exactly that, and every request is refused ("no_provider"). Real providers, sign-in, secret storage,
persistent connections, the hand-over to add-ons and history are REQUIRED and OPEN (research/ACCOUNTS-FOUNDATION-2026-11-14.md).

Lifecycle (cinema_backends): on while the user has it on; turning it off removes its service (handles held by
consumers stop working), cancels every waiting request, and any provider answer that arrives afterwards is dropped.

Bounds (review of ecc55fd): cancellation is cooperative only. A provider is told (its `cancelled` Event) and a late
answer is dropped, but Python cannot stop a running call. So every describe() and perform() actually running holds a
slot of contract.RUNNING (shared across this module's instances) until it returns; when all are taken the answer is
"busy" and no thread is started. Arguments and results are detached copies at the boundary (contract.arguments,
contract.result, contract.copy): nobody can change what was validated, kept, or read by another consumer.
"""
import secrets
import threading

import cinema_accounts_contract as contract

DESCRIBE_LIMIT = 0.5     # seconds per provider for describe()
REQUEST_LIMIT = 5.0      # seconds a provider has for one request (operations not named below)
# 2026-11-17 (Codex AZ review: a 77-source cache check failed as a whole while one resolve worked): a request's limit
# must cover its provider's own request timeout (Premiumize: 10 s), or a slow but valid answer is always "timed_out".
REQUEST_LIMITS = {'account.status': 12.0, 'debrid.availability': 15.0, 'debrid.resolve': 30.0}
# The provider's own fixed refusal codes passed on as a safe `detail` (never a message, URL or response text).
DETAILS = ('authentication_failed', 'permission_denied', 'rate_limited', 'network', 'service_error', 'bad_response', 'too_large',
           'not_connected', 'needs_attention', 'not_found', 'changed', 'redirect')
MAX_PENDING = 8          # waiting requests at once
MAX_KEPT = 32            # finished results kept for result()
MAX_PROVIDERS = 8
# The add-ons this module lets use one of its connections through Cinema's add-on channel (2026-11-17, D3: this POLICY was
# Cinema core's cinema_addon_channel): each at the ONE version its integration was checked against, behind its own
# preference of this module's (default Off), through one provider and only the operations named. Cinema's core routes the
# calls; it decides nothing of this.
ADDON_GRANTS = {
    'plugin.video.pov': dict(version='6.10.23', preference='pov_cache_check', provider='premiumize',
                             operations=('debrid.availability', 'debrid.resolve')),
    'plugin.video.otaku.testing': dict(version='5.3.4', preference='otaku_premiumize', provider='premiumize',
                                       operations=('debrid.availability', 'debrid.resolve')),
}


def _caller():
    """The consumer calling through Cinema's service handle now ((owner, generation)), or None (an older Cinema, or a
    direct call by this module itself)."""
    try:
        import cinema_services
        return cinema_services.caller()
    except Exception:
        return None


def _mine(request):
    """Whether the consumer calling now may read or cancel `request`: the one that submitted it; a direct call with no
    consumer (this module itself, or Cinema's core through an unscoped handle) may reach any."""
    now = _caller()
    return now is None or request.get('caller') == now


def _call(owner, fn, limit):
    """fn() on its own thread, charged to the shared running-call budget (contract.RUNNING) until fn itself returns.
    ('ok', value), ('failed', None) when it raised or overran, ('busy', None) when the budget is full: then no thread
    is started. An overrunning call keeps its slot (Python cannot stop it), so repeated calls cannot pile up threads."""
    if not contract.RUNNING.acquire(owner):
        return 'busy', None
    box, done = {}, threading.Event()

    def run():
        try:
            box['value'] = fn()
        except Exception:
            box['failed'] = True
        finally:
            contract.RUNNING.release(owner)
            done.set()
    try:
        threading.Thread(target=run, name='CinemaAccountsCall', daemon=True).start()
    except RuntimeError:
        contract.RUNNING.release(owner)
        return 'failed', None
    return ('ok', box.get('value')) if done.wait(limit) and 'failed' not in box else ('failed', None)


class Accounts(object):
    """The cinema.accounts/1 implementation one module instance provides."""
    # What a consumer may call through its handle (D4): nothing else of this object is reachable.
    CONTRACT_OPERATIONS = ('status', 'submit', 'result', 'cancel', 'addon_grant')

    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.requests = {}       # id -> {state, operation, provider, args, cancelled, ...}
        self.order = []          # finished ids, oldest first (bounded by MAX_KEPT)
        self.stopped = False

    # The providers registered now (as the registry lists them; nothing cached) -----------------------------------
    def _providers(self):
        listed = self.ctx.services.list(contract.PROVIDER_CONTRACT, contract.PROVIDER_MAJOR)
        return [(entry['name'], entry['service']) for entry in listed[:MAX_PROVIDERS]]

    def status(self):
        providers = []
        for name, service in self._providers():
            outcome, value = _call(self.ctx.id, service.describe, DESCRIBE_LIMIT)
            facts = contract.described(value) if outcome == 'ok' else None
            providers.append(dict(provider=name, state='ok', **facts) if facts
                             else dict(provider=name, state='busy' if outcome == 'busy' else 'not_answering'))
        # foundation_only is False from 0.5.0: a provider (Premiumize) can be connected from Settings. Add-ons still do
        # not use connections (no delegation), and history is never written.
        return dict(contract='%s/%d' % (contract.CONTRACT, contract.MAJOR), foundation_only=False, history_writes=False,
                    state='ready' if providers else 'no_providers', providers=providers,
                    operations=sorted(contract.OPERATIONS))

    # Add-on grants (D3) ------------------------------------------------------------------------------------------
    def addon_grant(self, addon, version=None, operation=None):
        """May `addon` (at `version`) use a connection through Cinema's add-on channel, for `operation` (or, None, for any
        of its granted operations)? {ok: True, provider, operations} or {ok: False, code}: not_offered (not an add-on this
        module grants, or its preference is not On), version_needed (eligible: ask again with its installed version),
        version (another version than the one checked), operation (not one granted), no_connection (the provider has no
        stored connection offering those operations). Asked when a grant opens AND on every call, so turning the
        preference off, disconnecting or turning this module off takes effect at once."""
        rule = ADDON_GRANTS.get(addon) if isinstance(addon, str) else None
        stored = self.ctx.storage.get() if rule is not None else None
        if rule is None or (stored or {}).get(rule['preference']) != 'On':
            return dict(ok=False, code='not_offered')
        if version is None:
            return dict(ok=False, code='version_needed')
        if version != rule['version']:
            return dict(ok=False, code='version')
        if operation is not None and operation not in rule['operations']:
            return dict(ok=False, code='operation')
        ready = any(p.get('provider') == rule['provider'] and p.get('state') == 'ok' and all(op in (p.get('operations') or []) for op in rule['operations'])
                    and p.get('connection') in ('connected', 'unknown') for p in self.status()['providers'])
        if not ready:
            # Only with a stored connection (verified, or not yet re-verified since the start: each call checks it
            # again); never with none or one that needs attention, so an add-on is never pointed at a missing account.
            return dict(ok=False, code='no_connection')
        return dict(ok=True, provider=rule['provider'], operations=list(rule['operations']))

    # Delegation -------------------------------------------------------------------------------------------------
    def submit(self, operation, provider, args):
        if not isinstance(operation, str) or operation not in contract.OPERATIONS:
            return contract.refusal('unknown_operation')
        args = contract.arguments(operation, args)  # a detached copy: the caller's object is never kept
        if args is None:
            return contract.refusal('invalid_arguments')
        providers = dict(self._providers())
        if not providers:
            return contract.refusal('no_provider')
        service = providers.get(provider) if isinstance(provider, str) else None
        if service is None:
            return contract.refusal('provider_unavailable')
        outcome, value = _call(self.ctx.id, service.describe, DESCRIBE_LIMIT)
        if outcome == 'busy':
            return contract.refusal('busy')
        facts = contract.described(value) if outcome == 'ok' else None
        if facts is None or operation not in facts['operations']:
            return contract.refusal('not_supported')
        if not contract.scoped_arguments(operation, args, facts):   # e.g. a list kind this provider never declared (R3)
            return contract.refusal('invalid_arguments')
        with self.lock:
            if self.stopped:
                return contract.refusal('module_stopped')
            if sum(1 for r in self.requests.values() if r['state'] == 'pending') >= MAX_PENDING:
                return contract.refusal('busy')
            # The worker's slot is charged now and freed only when perform() itself returns (see _run), whatever
            # happened to the request meanwhile: a full budget is refused here, before any worker is started.
            if not contract.RUNNING.acquire(self.ctx.id):
                return contract.refusal('busy')
            ident = 'r-' + secrets.token_hex(8)
            # Bound to whoever submitted it (D4, 2026-11-17): the consumer's lease as Cinema's service handle states it
            # while this call runs; only that consumer can read or cancel it.
            request = dict(state='pending', operation=operation, args=args, cancelled=threading.Event(), caller=_caller(),
                           scope=facts.get('watchlist'))   # its answer is held to THIS provider's declaration (R3)
            timer = threading.Timer(REQUEST_LIMITS.get(operation, REQUEST_LIMIT), self._finish, args=(ident, request), kwargs=dict(refused='timed_out'))
            timer.daemon = True
            request['timer'] = timer  # attached before any work can finish, so finishing always cancels it
            self.requests[ident] = request
        try:
            self.ctx.spawn(self._run, ident, request, service)
        except Exception:  # the module is stopping: nothing runs
            contract.RUNNING.release(self.ctx.id)
            self._finish(ident, request, refused='module_stopped')
            return contract.refusal('module_stopped')
        timer.start()  # a timer cancelled before it starts never fires
        return dict(ok=True, request=ident)

    def _run(self, ident, request, service):
        owner = self.ctx.id
        try:
            try:
                value = service.perform(request['operation'], contract.copy(request['args']), request['cancelled'])
            except Exception as failed:
                code = getattr(failed, 'code', None)
                self._finish(ident, request, refused='provider_failed', detail=code if code in DETAILS else None)
                return
            value = contract.result(request['operation'], value, request['args'], request.get('scope'))  # detached, then validated
            if value is None:
                self._finish(ident, request, refused='invalid_result')
                return
            self._finish(ident, request, value=value)
        finally:
            contract.RUNNING.release(owner)  # only now: perform() has returned

    def _finish(self, ident, request, value=None, refused=None, detail=None):
        """Commit one outcome, once: a later one (a provider answering after a cancel, timeout or stop) is dropped."""
        with self.lock:
            if request['state'] != 'pending':
                return False
            request['cancelled'].set()
            if refused:
                request.update(state='cancelled' if refused in ('cancelled', 'module_stopped') else 'refused', code=refused)
                if detail:
                    request['detail'] = detail
            else:
                request.update(state='done', value=value)
            timer = request.pop('timer', None)
            self.order.append(ident)
            while len(self.order) > MAX_KEPT:
                self.requests.pop(self.order.pop(0), None)
        if timer is not None:
            timer.cancel()
        return True

    def result(self, ident):
        with self.lock:
            request = self.requests.get(ident) if isinstance(ident, str) and contract.REQUEST.fullmatch(ident) else None
            if request is not None and not _mine(request):
                request = None                       # another consumer's request: as if it did not exist
            if request is None:
                return dict(state='refused', **{k: v for k, v in contract.refusal('not_found').items() if k != 'ok'})
            if request['state'] == 'pending':
                return dict(state='pending')
            if request['state'] == 'done':
                return dict(state='done', value=contract.copy(request['value']))  # a fresh copy for every reader
            out = dict(state=request['state'], code=request['code'], message=contract.MESSAGES[request['code']])
            if request.get('detail'):
                out['detail'] = request['detail']   # the provider's own fixed reason (DETAILS), when it gave one
            return out

    def cancel(self, ident):
        with self.lock:
            request = self.requests.get(ident) if isinstance(ident, str) else None
            if request is not None and not _mine(request):
                request = None                       # never another consumer's
        return bool(request) and self._finish(ident, request, refused='cancelled')

    def stop(self):
        """The module is turned off: every waiting request ends as cancelled (module_stopped)."""
        with self.lock:
            self.stopped = True
            waiting = [(i, r) for i, r in self.requests.items() if r['state'] == 'pending']
        for ident, request in waiting:
            self._finish(ident, request, refused='module_stopped')


_instance = None
_providers = []
_connectable = {}   # registration name -> provider with a connect seam (Settings' declared actions: Premiumize, AniList)


def _view(name, provider, outcome):
    """What Settings may show for one connectable provider (cinema.accounts.connect/1): enums and booleans only. The
    key is never part of it."""
    state, attempt = provider.connection_state(), provider.connect_state()
    shown = dict(state=attempt['state'], code=attempt.get('code'))
    detail = provider.connect_detail() if hasattr(provider, 'connect_detail') else None
    if detail:   # 2026-11-16: what kind of answer a failed check got (status, media type, size, fixed reason): never its content
        shown['detail'] = dict(detail)
    return dict(provider=name, label=provider.LABEL, connection=state['connection'], entitlement=state['entitlement'],
                checking=provider.checking(), attempt=shown, outcome=outcome)


_watch = {}          # request id -> (provider, media_type, status, page): watchlist reads Settings itself started
MAX_WATCH = 16
_watch_lock = threading.Lock()   # Settings may run two reads at once


def _watchlist(action, name, provider, args):
    """Settings' own view of watchlist.read (cinema.accounts.connect/1 actions watchlist, watchlist_result,
    watchlist_cancel). The read goes through this module's cinema.accounts/1 contract (submit/result/cancel: its
    arguments and result validation, request time limit, cancellation and module-off), never to the provider directly.
    Settings can read and cancel only requests it started here; the answer is the contract's validated page."""
    if 'watchlist.read' not in provider.describe().get('operations', ()):
        return dict(provider=name, request=None, outcome='not_supported')
    if action == 'watchlist':
        connection = provider.connection_state()['connection']
        if connection in ('not_connected', 'needs_attention'):
            return dict(provider=name, request=None, outcome=connection)
        query = dict(media_type=args.get('media_type'), status=args.get('status'), page=args.get('page'))
        answer = _instance.submit('watchlist.read', name, query)
        if not answer.get('ok'):
            code = answer.get('code')
            return dict(provider=name, request=None, outcome='busy' if code == 'busy' else 'stopped' if code == 'module_stopped' else 'refused')
        with _watch_lock:
            _watch[answer['request']] = (name, query['media_type'], query['status'], query['page'])
            while len(_watch) > MAX_WATCH:
                _watch.pop(next(iter(_watch)))
        return dict(provider=name, request=answer['request'], outcome='started')
    request = args.get('request')
    with _watch_lock:
        mine = _watch.get(request)
    if mine is None or mine[0] != name:
        if action == 'watchlist_cancel':
            return dict(provider=name, request=request, outcome='nothing_pending')
        return dict(provider=name, request=request, media_type=None, status=None, page=None, state='unknown', code=None, value=None)
    if action == 'watchlist_cancel':
        return dict(provider=name, request=request, outcome='cancelled' if _instance.cancel(request) else 'nothing_pending')
    got = _instance.result(request)
    state = got.get('state')
    return dict(provider=name, request=request, media_type=mine[1], status=mine[2], page=mine[3],
                state=state if state in ('pending', 'done', 'refused', 'cancelled') else 'unknown',
                code=got.get('code') if state in ('refused', 'cancelled') else None, value=got.get('value') if state == 'done' else None)


def settings_action(args):
    """The declared Settings action read ("connect"): reached only through Settings' action route (cinema_settings_actions
    checks the action and its exact arguments first, and the answer's shape after). A key arrives only with "connect",
    goes to the provider's connect seam, and is not kept, logged or returned here."""
    action = args.get('action')
    if action == 'list':
        # sign_in (2026-11-16): the provider can open its service's own consent page (Cinema's own registered app).
        return dict(providers=[dict(provider=name, label=p.LABEL, sign_in=bool(getattr(p, 'client_id', lambda: None)()))
                               for name, p in sorted(_connectable.items())])
    name = args.get('provider')
    provider = _connectable.get(name)
    if provider is None:
        raise ValueError('not connectable')
    if action in ('watchlist', 'watchlist_result', 'watchlist_cancel'):
        return _watchlist(action, name, provider, args)
    outcome = None
    if action == 'connect':
        try:
            provider.begin_connect(args.get('key'))
            outcome = 'started'
        except Exception as exc:
            code = getattr(exc, 'code', None)
            outcome = code if code in ('invalid_key', 'already_pending', 'stopped') else 'unsupported'
        finally:
            args = None
    elif action == 'authorize':   # Sign in with the service: its consent page in the system browser (no key here)
        outcome = provider.begin_authorize() if hasattr(provider, 'begin_authorize') else 'unsupported'
    elif action == 'cancel':
        outcome = 'cancelled' if provider.cancel_connect() else 'nothing_pending'
    elif action == 'disconnect':
        outcome = provider.disconnect()
    elif action == 'refresh':
        outcome = provider.begin_check()
    elif action != 'state':
        raise ValueError('not an action')
    return _view(name, provider, outcome)


def _sibling(name):
    """A file of this module's own folder, loaded afresh for this instance (as the host loads backend.py)."""
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + '.py')
    spec = importlib.util.spec_from_file_location('cinema_accounts_%s_%x' % (name, id(path)), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start(ctx):
    global _instance
    del _providers[:]   # this instance's providers only (a previous instance's were stopped with it)
    _connectable.clear()
    with _watch_lock:
        _watch.clear()  # an earlier instance's requests are not this one's
    _instance = Accounts(ctx)
    ctx.services.provide(contract.CONTRACT, contract.MAJOR, contract.NAME, _instance)
    ctx.reads.provide('status', lambda args: _instance.status())
    # Real-Debrid (realdebrid.py): account.status only. Sign-in is refused until Cinema's client identity is decided.
    realdebrid = _sibling('realdebrid')
    provider = realdebrid.RealDebridProvider(ctx, client_id=realdebrid.ACTIVE_CLIENT_ID)
    ctx.services.provide(contract.PROVIDER_CONTRACT, contract.PROVIDER_MAJOR, 'real-debrid', provider)
    _providers.append(provider)
    # Premiumize (premiumize.py): account.status only, with an API key the user supplies (no client registration needed).
    # Settings › Extensions › Accounts & Services connects it (the declared "connect" actions read below); add-ons do not
    # use the connection yet.
    premiumize = _sibling('premiumize')
    provider = premiumize.PremiumizeProvider(ctx)
    ctx.services.provide(contract.PROVIDER_CONTRACT, contract.PROVIDER_MAJOR, 'premiumize', provider)
    _providers.append(provider)
    _connectable['premiumize'] = provider
    # AniList (anilist.py, 2026-11-15): a watchlist connection with an access token the user supplies (validated with
    # Viewer; Cinema has no AniList client, so no in-app sign-in), and ONE read-only operation, watchlist.read. Add-ons do
    # not use it: no add-on adapter exists for it (Otaku's watchlist needs its own token-gated flavor; see the research
    # record ACCOUNTS-ANILIST-WATCHLIST-2026-11-15.md).
    anilist = _sibling('anilist')
    provider = anilist.AniListProvider(ctx)
    ctx.services.provide(contract.PROVIDER_CONTRACT, contract.PROVIDER_MAJOR, 'anilist', provider)
    _providers.append(provider)
    _connectable['anilist'] = provider
    ctx.reads.provide('connect', settings_action)   # declared in the manifest as its Settings actions read
    ctx.log('Accounts & Services started (providers: Real-Debrid, Premiumize, AniList)')


def stop():
    if _instance is not None:
        _instance.stop()
    for provider in _providers:
        provider.stop()   # an unfinished sign-in ends here; nothing it fetched is stored afterwards
