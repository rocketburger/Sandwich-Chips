"""Premiumize account provider for Accounts & Services (cinema.accounts.provider/1), production code (2026-11-14).

Protocol authority: https://www.premiumize.me/api (read 2026-11-14). Nothing here is taken from the Real-Debrid provider.
    Auth: an API key, sent as "Authorization: Bearer <key>" (the documented preferred form; the legacy apikey query
          parameter is never used, so the key is never in a URL).
    GET  https://www.premiumize.me/api/account/info  (no parameters)
         success: {"status": "success", "customer_id": "<string>", "premium_until": <unix time> | null (free account),
                   "limit_used": <0..1>, "booster_points": <number>, ...}
         error:   {"status": "error", "message": "...", "code": "<stable code>"}
    Business errors come back with HTTP 200 and status "error"; HTTP 500 (unknown_error) is for failures. So the status
    code alone never decides anything: "connected" needs HTTP 200 AND a valid success envelope.
    Codes used here: authentication_failed (key missing, invalid or expired), permission_denied (authenticated but not
    allowed, e.g. a restricted or unconfirmed account), rate_limit_reached; any other code is "not known now".
    OAuth (device flow / PKCE) is documented too but needs Cinema's own registered client: not used, not required here.

Offers account.status and (from 0.6.0) debrid.availability: a READ-ONLY cache check
    POST https://www.premiumize.me/api/cache/check  items[]=<hash> (repeated, form body)
         success: {"status": "success", "response": [bool, ...], "filename": [...], "filesize": [...]} (parallel arrays
                  indexed by request order); only `response` is read.
And (from 0.8.0) debrid.resolve, for playback the user triggered: POST /api/transfer/directdl src=<bare magnet built
here from a 40-hex hash>; documented as generating direct download links "without storing it in your cloud". Only
content[{path, size, link}] is read (stream_link, location, filename are deprecated and ignored).
No cloud add (transfer/create), delete, history, autodelete or any account change exists here.

Connection (one owner: this provider instance):
- The stored connection is one bundle {"v": 1, "api_key": ...} under this module's ctx.secrets name "premiumize". The
  key is never in a URL, query, log, status, diagnostics, contract value or error; errors are fixed codes.
- connect seam (backend-only; no page, HTTP route or UI reaches it yet): begin_connect(key) takes a key the user has just
  typed (never one read from an add-on), verifies it with account/info, and commits it only if this attempt is still
  the current one, not cancelled, and the provider is not stopped (one atomic step under self.lock, which
  cancel_connect, disconnect and stop also take). A rejected candidate, a failure before the commit, or a cancel leaves
  the previous connection exactly as it was. An unconfirmed write is re-read and reported for what the store holds.
- After a restart the state is "unknown" until a fresh check verifies it; "connected" only from a valid success
  envelope for the stored key.
- Premium entitlement is kept apart from authentication ("premium" | "not_premium" | "unknown", backend-only):
  the contract carries the connection only, and a free account is still a connected one.
- disconnect() removes this provider's own stored key only (nothing is revoked at Premiumize, nothing else is touched);
  any verification or connect still running for an earlier connection can no longer change anything.
"""
import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

API_ORIGIN = 'https://www.premiumize.me'
ENDPOINTS = {'account_info': ('GET', '/api/account/info'), 'cache_check': ('POST', '/api/cache/check'),
             'directdl': ('POST', '/api/transfer/directdl')}
MAX_FILES = 2000               # the contract's bound (cinema_accounts_contract.MAX_FILES); a pack's first entries beyond are left out
RESOLVE_RESPONSE = 4194304     # a directdl answer for a big pack (2026-11-17: 256 KiB refused packs with subtitles and fonts)
MAX_HASHES = 50                 # the contract's bound (cinema_accounts_contract.MAX_HASHES)
HASH = re.compile(r'[0-9a-f]{40}')
SECRET_NAME = 'premiumize'
TIMEOUT = 10.0
MAX_RESPONSE = 16384
CACHE_RESPONSE = 262144
# The documented key format is not specified beyond "API key"; a conservative token shape: no spaces, quotes, separators.
API_KEY = re.compile(r'[A-Za-z0-9_-]{8,128}')
CUSTOMER = re.compile(r'[A-Za-z0-9_-]{1,64}')
ENTITLEMENT = ('premium', 'not_premium', 'unknown')
CONNECT_STATES = ('idle', 'verifying', 'saving', 'connected', 'rejected', 'failed', 'cancelled', 'uncertain')
CONNECT_CODES = ('authentication_failed', 'permission_denied', 'rate_limited', 'network', 'service_error', 'bad_response',
                 'save_failed', 'write_unconfirmed', 'stopped', 'internal')


class TransportError(Exception):
    """A fixed code only: network, redirect, too_large, unknown_endpoint. Never a URL, header or response text."""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a 3xx is answered as itself, never followed (the key is never sent anywhere else)


class Transport(object):
    """HTTPS to www.premiumize.me only, named endpoints only, the key only in the Authorization header, bounded time
    and size, no redirects."""

    def __init__(self, opener=None, timeout=TIMEOUT):
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.timeout = timeout

    def request(self, endpoint, bearer, form=None):
        """(HTTP status, dict or None). `form`: a list of (name, value) pairs, sent form-encoded as the body of a POST
        endpoint (repeated names allowed, e.g. items[]); never in the URL. Raises TransportError."""
        if endpoint not in ENDPOINTS:
            raise TransportError('unknown_endpoint')
        method, path = ENDPOINTS[endpoint]
        if (form is not None) != (method == 'POST'):
            raise TransportError('unknown_endpoint')
        headers = {'Accept': 'application/json', 'Authorization': 'Bearer ' + bearer}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode('ascii')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        request = urllib.request.Request(API_ORIGIN + path, data=data, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as answered:
                response = answered
            # cache/check also returns every hash's file name and size: 50 batch names (JSON-escaped non-ASCII) can pass
            # 16 KiB, so its answer has its own, larger bound (2026-11-17).
            limit = RESOLVE_RESPONSE if endpoint == 'directdl' else CACHE_RESPONSE if endpoint == 'cache_check' else MAX_RESPONSE
            with response:
                status = response.getcode() if hasattr(response, 'getcode') else response.status
                raw = response.read(limit + 1)
        except (urllib.error.URLError, OSError, ValueError):
            raise TransportError('network')  # never the exception's own text
        if 300 <= status < 400:
            raise TransportError('redirect')
        if len(raw) > limit:
            raise TransportError('too_large')
        try:
            value = json.loads(raw.decode('utf-8')) if raw else None
        except (UnicodeDecodeError, ValueError):
            value = None
        return status, value if isinstance(value, dict) else None


class ConnectRefused(Exception):
    """invalid_key | already_pending | stopped"""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


def _bundle(value):
    if not isinstance(value, dict) or set(value) != {'v', 'api_key'} or value['v'] != 1:
        return None
    return value if isinstance(value['api_key'], str) and API_KEY.fullmatch(value['api_key']) else None


def _encode(bundle):
    return json.dumps(bundle, separators=(',', ':'), sort_keys=True).encode('utf-8')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def verdict(status, data, now):
    """What one account/info answer says: ('connected', entitlement) | ('authentication_failed', None) |
    ('permission_denied', None) | ('unknown', code). Pure; reads no field beyond those checked here."""
    if status == 200 and isinstance(data, dict):
        state = data.get('status')
        if state == 'success':
            customer, until = data.get('customer_id'), data.get('premium_until', 'missing')
            if not (isinstance(customer, str) and CUSTOMER.fullmatch(customer)):
                return 'unknown', 'bad_response'
            if until is None:
                return 'connected', 'not_premium'          # documented: null for free accounts
            if not _number(until) or until < 0:
                return 'unknown', 'bad_response'
            return 'connected', 'premium' if until > now else 'not_premium'
        if state == 'error':
            code = data.get('code')
            if code == 'authentication_failed':
                return 'authentication_failed', None
            if code == 'permission_denied':
                return 'permission_denied', None
            return 'unknown', 'rate_limited' if code == 'rate_limit_reached' else 'service_error'
        return 'unknown', 'bad_response'
    # Not documented for business errors (they are HTTP 200), so read defensively and never as success.
    if status == 401:
        return 'authentication_failed', None
    if status == 403:
        return 'permission_denied', None
    if status == 429:
        return 'unknown', 'rate_limited'
    return 'unknown', 'service_error' if isinstance(status, int) and status >= 500 else 'bad_response'


class AvailabilityRefused(Exception):
    """A cache check that cannot be answered: a fixed code only (not_connected | needs_attention | invalid_hashes |
    cancelled | changed | stopped | authentication_failed | permission_denied | rate_limited | network | service_error |
    bad_response). Through the contract it becomes the refusal "provider_failed": never an answer of all-false."""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


def availability(status, data, hashes):
    """One /api/cache/check answer for exactly `hashes` (in request order) -> {hash: bool}. Raises AvailabilityRefused
    unless it is HTTP 200, status "success" and `response` is a list of exactly len(hashes) booleans. Only the booleans
    are read: filename, filesize and anything else in the answer are ignored and never passed on."""
    if status == 200 and isinstance(data, dict):
        state = data.get('status')
        if state == 'success':
            answer = data.get('response')
            if not isinstance(answer, list) or len(answer) != len(hashes) or not all(type(v) is bool for v in answer):
                raise AvailabilityRefused('bad_response')
            return {h: v for h, v in zip(hashes, answer)}
        if state == 'error':
            code = data.get('code')
            raise AvailabilityRefused(code if code in ('authentication_failed', 'permission_denied') else
                                      'rate_limited' if code == 'rate_limit_reached' else 'service_error')
        raise AvailabilityRefused('bad_response')
    if status == 401:
        raise AvailabilityRefused('authentication_failed')
    if status == 403:
        raise AvailabilityRefused('permission_denied')
    if status == 429:
        raise AvailabilityRefused('rate_limited')
    raise AvailabilityRefused('service_error' if isinstance(status, int) and status >= 500 else 'bad_response')


_PATH = re.compile(r'[^\x00-\x1f\x7f]{1,512}')
_LINK = re.compile(r'https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?/[\x21-\x7e]{0,2000}')


def _encoded(link):
    """An https link with any raw non-ASCII character or space percent-encoded (UTF-8); every other character as sent."""
    if not isinstance(link, str) or not link.startswith('https://') or len(link) > 4000:
        return None
    if all(0x21 <= ord(c) <= 0x7e for c in link):
        return link
    return urllib.parse.quote(link, safe=''.join(chr(c) for c in range(0x21, 0x7f)))


def resolved(status, data):
    """One /api/transfer/directdl answer -> [{path, size, link}] (the documented, non-deprecated fields only). Raises
    AvailabilityRefused unless HTTP 200, status "success" and `content` has at least one entry with a plain path, an
    integer size and an https link without credentials (other entries are left out; at most MAX_FILES are read). stream_link, transcode_status, location, filename, filesize and
    anything else are ignored."""
    if status == 200 and isinstance(data, dict):
        state = data.get('status')
        if state == 'success':
            content = data.get('content')
            if not isinstance(content, list) or not content:
                raise AvailabilityRefused('bad_response')
            files = []
            # 2026-11-17 (user report: batches "with no video file"): one entry Cinema cannot use no longer refuses the
            # whole pack. Such an entry is left out; a link carrying raw non-ASCII or spaces (Premiumize puts the file name
            # in it) is percent-encoded, which names the same resource; a big pack keeps its first MAX_FILES entries.
            for item in content[:MAX_FILES]:
                if not isinstance(item, dict):
                    continue
                path, size, link = item.get('path'), item.get('size'), _encoded(item.get('link'))
                if (isinstance(path, str) and _PATH.fullmatch(path) and type(size) is int and 0 <= size < 2 ** 53
                        and isinstance(link, str) and _LINK.fullmatch(link) and '@' not in link.split('/', 3)[2]):
                    files.append(dict(path=path, size=size, link=link))
            if not files:
                raise AvailabilityRefused('bad_response')
            return files
        if state == 'error':
            code = data.get('code')
            raise AvailabilityRefused(code if code in ('authentication_failed', 'permission_denied', 'not_found') else
                                      'rate_limited' if code == 'rate_limit_reached' else 'service_error')
        raise AvailabilityRefused('bad_response')
    return availability(status, data, [])   # the same HTTP-status refusals (never a success)


class _Flight(object):
    """One verification of one stored key, for one version: its result once done (see check)."""

    def __init__(self, version, key):
        self.version, self.key, self.done, self.result = version, key, threading.Event(), None   # None until done


def _wait(done, seconds, cancelled, step=0.05):
    """Cooperative bounded wait on the calling thread: True once `done`, False on timeout or cancel. No new thread."""
    import time
    deadline = time.monotonic() + seconds
    while not done.is_set():
        left = deadline - time.monotonic()
        if left <= 0 or cancelled.is_set():
            return False
        done.wait(min(step, left))
    return True


class _Attempt(object):
    def __init__(self, ident):
        self.id, self.cancelled, self.state, self.code = ident, threading.Event(), 'verifying', None


class PremiumizeProvider(object):
    """One instance per module instance. Contract surface: describe(), perform(operation, args, cancelled).
    Backend-only seams (no page or HTTP route reaches them yet): begin_connect, connect_state, cancel_connect, disconnect,
    connection_state."""

    LABEL = 'Premiumize'

    def __init__(self, ctx, transport=None, clock=None, wait=None):
        import time
        self.ctx, self.transport, self.clock = ctx, transport or Transport(), clock or time.time
        self.wait = wait or _wait    # (done Event, seconds, cancelled Event) -> finished?
        self.lock = threading.Lock()
        self.bundle, self.raw, self.loaded, self.problem, self.version = None, None, False, None, 0
        self.connection, self.entitlement = 'unknown', 'unknown'   # last VERIFIED; unknown after start until checked
        self.attempt, self.attempts = None, 0
        self.flight = None           # the one verification in flight (_Flight), for the current version
        self.refreshing = 0          # checks begun by begin_check that have not returned yet
        self.stopped = False

    # Stored connection ----------------------------------------------------------------------------------------------
    def _load(self):
        """(bundle or None, problem or None); the store is read once per instance, or again after an unconfirmed write."""
        with self.lock:
            if self.loaded:
                return self.bundle, self.problem
        try:
            raw = self.ctx.secrets.get(SECRET_NAME)
            bundle = _bundle(json.loads(raw.decode('utf-8'))) if raw is not None else None
            problem = 'needs_attention' if raw is not None and bundle is None else None  # stored, not this provider's shape
        except ValueError:
            raw, bundle, problem = None, None, 'needs_attention'
        except Exception as exc:                 # SecretError or ExtensionStopped: nothing claimed
            raw, bundle = None, None
            problem = 'needs_attention' if getattr(exc, 'code', None) in ('corrupt', 'undecryptable') else 'unknown'
        with self.lock:
            if not self.loaded:
                self.bundle, self.raw, self.problem, self.loaded = bundle, raw if bundle else None, problem, True
                if bundle is None and problem is None:
                    self.connection, self.entitlement = 'not_connected', 'unknown'
            return self.bundle, self.problem

    # Contract -------------------------------------------------------------------------------------------------------
    def describe(self):
        bundle, problem = self._load()
        with self.lock:
            connection = problem or ('not_connected' if bundle is None else self.connection)
        return dict(label=self.LABEL, connection=connection, operations=['account.status', 'debrid.availability', 'debrid.resolve'])

    def perform(self, operation, args, cancelled):
        if operation == 'account.status' and args == {}:
            return dict(connection=self.check(cancelled))
        if operation == 'debrid.availability' and isinstance(args, dict) and set(args) == {'hashes'}:
            return dict(available=self.availability(args['hashes'], cancelled))
        if operation == 'debrid.resolve' and isinstance(args, dict) and set(args) == {'hash'}:
            return dict(files=self.resolve(args['hash'], cancelled))
        raise ValueError('not offered')

    def _guarded(self, endpoint, form, cancelled):
        """One request with the stored key: the key and version taken together under the lock, re-checked right before
        sending (_current) and after the answer; cancel checked before and after. Returns (status, data, version). Raises
        AvailabilityRefused. One request, no retry."""
        self._load()
        with self.lock:
            if self.stopped:
                raise AvailabilityRefused('stopped')
            if self.problem:
                raise AvailabilityRefused('needs_attention')
            if self.bundle is None:
                raise AvailabilityRefused('not_connected')
            key, version = self.bundle['api_key'], self.version
        if cancelled.is_set():
            raise AvailabilityRefused('cancelled')
        if not self._current(version):
            raise AvailabilityRefused('changed')
        try:
            status, data = self.transport.request(endpoint, bearer=key, form=form)
        except TransportError as exc:
            raise AvailabilityRefused('network' if exc.code in ('network', 'redirect') else 'bad_response')
        finally:
            key = None
        with self.lock:
            if self.stopped:
                raise AvailabilityRefused('stopped')
            if self.version != version:
                raise AvailabilityRefused('changed')
        if cancelled.is_set():
            raise AvailabilityRefused('cancelled')
        return status, data, version

    def _answered(self, parse, status, data, version):
        """parse(status, data), and what that answer shows about the stored key (2026-11-16, restart fix): an accepted
        request proves the key works, so a connection not verified since start ("unknown") becomes "connected"; a
        refused key is "needs_attention". Recorded only for the version the request was made with (never a newer key)."""
        try:
            value = parse(status, data)
        except AvailabilityRefused as refused:
            if refused.code in ('authentication_failed', 'permission_denied'):
                self._observe('needs_attention', version)
            raise
        self._observe('connected', version)
        return value

    def _observe(self, connection, version):
        with self.lock:
            if self.stopped or self.version != version:
                return
            if connection == 'needs_attention':
                self.connection, self.entitlement = 'needs_attention', 'unknown'
            elif self.connection == 'unknown':
                self.connection = 'connected'   # entitlement stays as last verified (unknown until a check)

    def resolve(self, torrent_hash, cancelled=None):
        """Playback, user-triggered: the files of one CACHED torrent with Premiumize's download links, as
        [{path, size, link}]. POST /api/transfer/directdl with src = a bare magnet built here from the 40-hex hash
        (nothing an add-on supplies is sent as a URL). Documented: it "generates direct download links from a transfer
        source without storing it in your cloud" (no cloud item, nothing deleted; whether the service records a
        transfer is not documented). A bare magnet as src is not stated in the docs ("Transfer source URL"; magnets
        are described as resolving to every file); not live-proven. Raises AvailabilityRefused."""
        cancelled = cancelled or threading.Event()
        if not isinstance(torrent_hash, str) or not HASH.fullmatch(torrent_hash):
            raise AvailabilityRefused('invalid_hashes')
        status, data, version = self._guarded('directdl', [('src', 'magnet:?xt=urn:btih:' + torrent_hash)], cancelled)
        return self._answered(resolved, status, data, version)

    def availability(self, hashes, cancelled=None):
        """Read-only: which of these content hashes Premiumize's cache has, as {hash: bool} for exactly these hashes.

        POST /api/cache/check with one items[] form field per hash, in order; the answer's `response` booleans are read
        by position. Hashes as items: the documentation describes items[] as "links"; that a bare 40-hex hash is
        accepted is the pristine POV's own usage (indexers/premiumize_api.py check_cache), not a documented or live-proven
        fact. The stored key and its version are taken together under the lock; the request is refused before it is
        sent, and its answer refused after, if the request was cancelled, the provider stopped, or the connection was
        replaced or removed (version). One request, no retry, nothing stored. Raises AvailabilityRefused."""
        cancelled = cancelled or threading.Event()
        if (not isinstance(hashes, list) or not 0 < len(hashes) <= MAX_HASHES or len(set(hashes)) != len(hashes)
                or not all(isinstance(h, str) and HASH.fullmatch(h) for h in hashes)):
            raise AvailabilityRefused('invalid_hashes')
        hashes = list(hashes)
        status, data, version = self._guarded('cache_check', [('items[]', h) for h in hashes], cancelled)
        return self._answered(lambda s, d: availability(s, d, hashes), status, data, version)

    def connection_state(self):
        """Backend-only: {connection, entitlement}. Never the key, the customer id or any response text."""
        bundle, problem = self._load()
        with self.lock:
            connection = problem or ('not_connected' if bundle is None else self.connection)
            return dict(connection=connection, entitlement=self.entitlement if connection == 'connected' else 'unknown')

    def check(self, cancelled=None):
        """The stored key's connection, verified with account/info now: connected | not_connected | needs_attention |
        unknown.

        Flights (review of eb50d8a): the key to verify, its version and ownership of the one verification are taken in
        one step under self.lock, from what is stored NOW (re-read under the lock after loading), so a disconnect or a
        new connection made before that step is what is checked, never the key loaded earlier. A flight belongs to one
        version: before its request it refuses to send if that version is gone, and its answer is recorded only for it.
        A concurrent caller of the same version waits for that flight (cooperative, bounded, cancellable) and gets that
        flight's own result; on timeout, cancel, stop or a changed connection it gets "unknown", never a cached state."""
        cancelled = cancelled or threading.Event()
        self._load()
        with self.lock:
            if self.stopped:
                return 'unknown'
            if self.problem:
                return self.problem
            if self.bundle is None:
                return 'not_connected'
            flight = self.flight
            owner = flight is None or flight.version != self.version   # an older version's flight is not ours to join
            if owner:
                flight = self.flight = _Flight(self.version, self.bundle['api_key'])
        if not owner:
            finished = self.wait(flight.done, TIMEOUT * 2, cancelled)
            with self.lock:
                if not finished or cancelled.is_set() or self.stopped or self.version != flight.version:
                    return 'unknown'
                return flight.result
        result = 'unknown'
        try:
            if self._lost(flight):
                return result                        # gone before any request: the old key is not sent
            word, detail = self._verify(flight.key, cancelled)
            connection = {'connected': 'connected', 'authentication_failed': 'needs_attention',
                          'permission_denied': 'needs_attention'}.get(word, 'unknown')
            result = self._record(connection, detail if word == 'connected' else 'unknown', flight.version, cancelled)
            return result
        finally:
            with self.lock:
                flight.result = result
                if self.flight is flight:
                    self.flight = None
            flight.done.set()

    def begin_check(self):
        """Start one check of the stored key on a tracked module task (Settings' "Check now"): 'started' |
        'already_checking' | 'not_connected' | 'stopped'. The result is what check() records; read it with
        connection_state()."""
        bundle, problem = self._load()
        with self.lock:
            if self.stopped:
                return 'stopped'
            if bundle is None and not problem:
                return 'not_connected'
            if self.flight is not None or self.refreshing:
                return 'already_checking'
            self.refreshing += 1

        def run():
            try:
                self.check()
            finally:
                with self.lock:
                    self.refreshing -= 1
        try:
            self.ctx.spawn(run)
        except Exception:
            with self.lock:
                self.refreshing -= 1
            return 'stopped'
        return 'started'

    def checking(self):
        """True while a check runs (begun from Settings, or any other caller's)."""
        with self.lock:
            return self.flight is not None or self.refreshing > 0

    def _current(self, version):
        """True while the connection taken at `version` is still the stored one and the provider runs (checked right
        before a request; the lock is not held over the network)."""
        with self.lock:
            return not self.stopped and self.version == version

    def _lost(self, flight):
        with self.lock:
            return self.stopped or self.version != flight.version

    def _record(self, connection, entitlement, version, cancelled):
        """Keep a verified state only for the connection it was verified with: an answer overtaken (a new connection or
        a disconnect), cancelled, or arriving after stop is neither kept nor returned ("unknown")."""
        with self.lock:
            if self.version != version or self.stopped or cancelled.is_set():
                return 'unknown'
            self.connection = connection
            if connection == 'connected':
                self.entitlement = entitlement
            elif connection == 'needs_attention':
                self.entitlement = 'unknown'
        return connection

    def _verify(self, key, cancelled):
        if cancelled.is_set():
            return 'unknown', 'stopped'
        try:
            status, data = self.transport.request('account_info', bearer=key)
        except TransportError as exc:
            return 'unknown', 'network' if exc.code in ('network', 'redirect') else 'bad_response'
        if cancelled.is_set():
            return 'unknown', 'stopped'   # the answer came after a cancel: not used
        return verdict(status, data, self.clock())

    # Connect (backend-only seam; explicit user input) ------------------------------------------------------------------
    def begin_connect(self, api_key):
        """Verify a key the user has just supplied and, if it is accepted, make it this provider's connection. Runs on
        one tracked module task. Returns the attempt id. Raises ConnectRefused. The key is never logged or kept unless
        committed."""
        if not isinstance(api_key, str) or not API_KEY.fullmatch(api_key):
            raise ConnectRefused('invalid_key')
        with self.lock:
            if self.stopped:
                raise ConnectRefused('stopped')
            if self.attempt is not None and self.attempt.state in ('verifying', 'saving'):
                raise ConnectRefused('already_pending')
            self.attempts += 1
            attempt = self.attempt = _Attempt(self.attempts)
        try:
            self.ctx.spawn(self._connect, attempt, api_key)
        except Exception:
            self._end(attempt, 'failed', 'stopped')
            raise ConnectRefused('stopped')
        return attempt.id

    def connect_state(self):
        """Backend-only: {state, code?, entitlement?}. Never the key."""
        with self.lock:
            attempt = self.attempt
            if attempt is None:
                return dict(state='idle')
            out = dict(state=attempt.state)
            if attempt.code:
                out['code'] = attempt.code
            if attempt.state == 'connected':
                out['entitlement'] = self.entitlement
            return out

    def cancel_connect(self):
        """Cancel the current attempt. Set under self.lock, which the commit also holds: a cancel either prevents the
        commit or comes after it (then the connection stands; removing it is disconnect)."""
        with self.lock:
            attempt = self.attempt
            if attempt is None or attempt.state not in ('verifying', 'saving'):
                return False
            attempt.cancelled.set()
        return True

    def _end(self, attempt, state, code=None):
        with self.lock:
            if attempt.state in ('verifying', 'saving'):
                attempt.state, attempt.code = state, code

    def _halted(self, attempt):
        return attempt.cancelled.is_set() or self.stopped or self.ctx.stopping.is_set() or attempt is not self.attempt

    def _connect(self, attempt, key):
        try:
            word, detail = self._verify(key, attempt.cancelled)
            if self._halted(attempt):
                return self._end(attempt, 'cancelled')
            if word != 'connected':
                # The candidate is not accepted: the previous connection is untouched.
                if word in ('authentication_failed', 'permission_denied'):
                    return self._end(attempt, 'rejected', word)
                return self._end(attempt, 'failed', detail if detail in CONNECT_CODES else 'service_error')
            self._commit(attempt, {'v': 1, 'api_key': key}, detail)
        except Exception:
            self._end(attempt, 'failed', 'internal')

    def _commit(self, attempt, bundle, entitlement):
        """One atomic step under self.lock: still the current attempt, not cancelled, not stopped; then the write."""
        new = _encode(bundle)
        with self.lock:
            if self._halted(attempt):
                attempt.state = 'cancelled'
                return
            attempt.state = 'saving'
            prior = self.raw
            try:
                self.ctx.secrets.set(SECRET_NAME, new)
            except Exception as exc:
                code = getattr(exc, 'code', None)
                if code != 'write_failed':
                    # Refused before anything was replaced (stopped instance, quota, a damaged stored value, no
                    # sealer): the previous connection stands.
                    attempt.state, attempt.code = 'failed', 'stopped' if code in (None, 'stale') else 'save_failed'
                    return
                self._unconfirmed(attempt, bundle, new, prior, entitlement)
                return
            self.bundle, self.raw, self.problem, self.loaded = bundle, new, None, True
            self.version += 1
            self.connection, self.entitlement = 'connected', entitlement   # verified just now, with exactly this key
            attempt.state, attempt.code = 'connected', None

    def _unconfirmed(self, attempt, bundle, new, prior, entitlement):
        """write_failed: the store either kept the old value (failure before the replace) or holds the new one whose
        read-back failed. Re-read it and say what it holds; if that is not known either, say so. (Holds self.lock.)"""
        try:
            stored = self.ctx.secrets.get(SECRET_NAME)
        except Exception:
            stored = False   # not readable now
        if stored is not False and stored == prior:
            attempt.state, attempt.code = 'failed', 'save_failed'        # the previous connection stands
            return
        self.version += 1                                                 # whatever was verified before no longer applies
        if stored == new:
            self.bundle, self.raw, self.problem, self.loaded = bundle, new, None, True
            self.connection, self.entitlement = 'connected', entitlement
            attempt.state, attempt.code = 'connected', None
            return
        self.loaded = False                                               # read the store again when next asked
        self.connection, self.entitlement = 'unknown', 'unknown'
        attempt.state, attempt.code = 'uncertain', 'write_unconfirmed'

    # Disconnect (backend-only seam; explicit user action) -------------------------------------------------------------
    def disconnect(self):
        """Remove this provider's own stored key (local only: nothing is revoked at Premiumize). A connect or check still
        running can no longer change anything. 'disconnected' | 'not_connected' | 'failed' | 'stopped'."""
        with self.lock:
            if self.stopped:
                return 'stopped'
            if self.attempt is not None:
                self.attempt.cancelled.set()
            self.version += 1
            try:
                removed = self.ctx.secrets.delete(SECRET_NAME)
            except Exception:
                self.loaded = False   # not known now: read the store again when next asked
                self.connection, self.entitlement = 'unknown', 'unknown'
                return 'failed'
            self.bundle, self.raw, self.problem, self.loaded = None, None, None, True
            self.connection, self.entitlement = 'not_connected', 'unknown'
            return 'disconnected' if removed else 'not_connected'

    def stop(self):
        with self.lock:   # the same commit boundary as cancel_connect and disconnect
            self.stopped = True
            if self.attempt is not None:
                self.attempt.cancelled.set()
