"""Real-Debrid account provider for Accounts & Services (cinema.accounts.provider/1), production code.

Protocol authority: https://api.real-debrid.com/ (read 2026-11-14), "opensource apps" device workflow:
    GET  https://api.real-debrid.com/oauth/v2/device/code?client_id=..&new_credentials=yes
         -> device_code, user_code, interval, expires_in, verification_url
    GET  https://api.real-debrid.com/oauth/v2/device/credentials?client_id=..&code=<device_code>   ("every 5 seconds";
         an error until the user has entered the code) -> client_id, client_secret (bound to the user)
    POST https://api.real-debrid.com/oauth/v2/token  client_id, client_secret, code=<device_code>,
         grant_type=http://oauth.net/grant_type/device/1.0 -> access_token, expires_in, token_type, refresh_token
    POST .../oauth/v2/token with code=<refresh_token> (same grant_type): a new token pair
    GET  https://api.real-debrid.com/rest/1.0/user  (Authorization: Bearer)
Errors: HTTP 4xx/5xx with {"error", "error_code"}; 401 bad token, 403 permission denied (account locked), 429 too many
requests ("slow down" is error_code 5). Limit: 250 requests per minute.

Offers ONLY account.status. Hash availability is not offered: no documented endpoint answers it, and "not offered" is
never turned into "unavailable". No resolve, add, download or history operation exists here.

CLIENT IDENTITY (activation blocker): the docs publish an open-source client id (DOCUMENTED_OPEN_SOURCE_CLIENT_ID) for
open-source apps. Cinema is not established as one (no licence is declared), and another add-on's identity is never
used. So ACTIVE_CLIENT_ID is None: starting a sign-in is refused ("client_identity_missing") until Cinema's identity is
decided (adopt the documented open-source client under a published open-source licence, or register Cinema's own
application). Status still works: with no stored connection it says "not_connected".

Secrets: one coherent bundle {client_id, client_secret, access_token, refresh_token, expires_at} under the module's
own ctx.secrets name "realdebrid", written once per sign-in or refresh. Never in status, reads, logs or plain storage;
errors are fixed codes, never response text. The device user_code and verification URL are kept for a backend-only
presentation seam (sign_in_state), reached by no page or HTTP route yet.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_ORIGIN = 'https://api.real-debrid.com'
ENDPOINTS = {'device_code': ('GET', '/oauth/v2/device/code'), 'credentials': ('GET', '/oauth/v2/device/credentials'),
             'token': ('POST', '/oauth/v2/token'), 'user': ('GET', '/rest/1.0/user')}
GRANT_TYPE = 'http://oauth.net/grant_type/device/1.0'
DOCUMENTED_OPEN_SOURCE_CLIENT_ID = 'X245A4XAIBGVM'   # as published for open-source apps; NOT active (see above)
ACTIVE_CLIENT_ID = None                             # Cinema's client identity: an activation decision
SECRET_NAME = 'realdebrid'
TIMEOUT = 10.0
MAX_RESPONSE = 65536
REFRESH_MARGIN = 120          # seconds before expiry a token is refreshed
MAX_INTERVAL = 60             # polling backoff cap, seconds
MAX_POLLS = 720
TOKEN = re.compile(r'[A-Za-z0-9._~+/=-]{8,512}')
USER_CODE = re.compile(r'[A-Za-z0-9-]{4,32}')
VERIFICATION = re.compile(r'https://(?:www\.)?real-debrid\.com/[A-Za-z0-9/._?=&-]{0,200}')


class TransportError(Exception):
    """A fixed code only: network, redirect, too_large, unknown_endpoint. Never a URL, header or response text."""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a 3xx is answered as itself, never followed


class Transport(object):
    """HTTPS to the fixed provider origin only, named endpoints only, bounded time and size, no redirects."""

    def __init__(self, opener=None, timeout=TIMEOUT):
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.timeout = timeout

    def request(self, endpoint, query=None, form=None, bearer=None):
        """(status, dict or None). Raises TransportError."""
        if endpoint not in ENDPOINTS:
            raise TransportError('unknown_endpoint')
        method, path = ENDPOINTS[endpoint]
        url = API_ORIGIN + path + ('?' + urllib.parse.urlencode(query) if query else '')
        headers = {'Accept': 'application/json'}
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        data = urllib.parse.urlencode(form).encode('ascii') if form is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as answered:
                response = answered
            with response:
                status = response.getcode() if hasattr(response, 'getcode') else response.status
                raw = response.read(MAX_RESPONSE + 1)
        except (urllib.error.URLError, OSError, ValueError):
            raise TransportError('network')  # never the exception's own text (it may hold the URL)
        if 300 <= status < 400:
            raise TransportError('redirect')
        if len(raw) > MAX_RESPONSE:
            raise TransportError('too_large')
        try:
            value = json.loads(raw.decode('utf-8')) if raw else None
        except (UnicodeDecodeError, ValueError):
            value = None
        return status, value if isinstance(value, dict) else None


class SignInRefused(Exception):
    """client_identity_missing | already_pending | stopped"""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


def _bundle(value):
    """A stored bundle held to its exact shape, or None."""
    keys = {'v', 'client_id', 'client_secret', 'access_token', 'refresh_token', 'expires_at'}
    if not isinstance(value, dict) or set(value) != keys or value['v'] != 1 or type(value['expires_at']) is not int:
        return None
    return value if all(isinstance(value[k], str) and TOKEN.fullmatch(value[k]) for k in keys - {'v', 'expires_at'}) else None


def _token_answer(status, data):
    if status != 200 or not isinstance(data, dict):
        return None
    access, refresh, expires = data.get('access_token'), data.get('refresh_token'), data.get('expires_in')
    if not (isinstance(access, str) and TOKEN.fullmatch(access) and isinstance(refresh, str) and TOKEN.fullmatch(refresh)
            and type(expires) is int and 60 <= expires <= 30 * 86400):
        return None
    return access, refresh, expires


class _Flow(object):
    def __init__(self, ident):
        self.id, self.cancelled = ident, threading.Event()
        self.state, self.code, self.user_code, self.verification_url, self.expires_at = 'starting', None, None, None, None


class RealDebridProvider(object):
    """One instance per module instance. Contract surface: describe(), perform(operation, args, cancelled).
    Backend-only sign-in seam (no page or HTTP route reaches it yet): begin_sign_in(), sign_in_state(), cancel_sign_in()."""

    LABEL = 'Real-Debrid'

    def __init__(self, ctx, transport=None, client_id=None, clock=time.time, wait=None):
        self.ctx, self.transport, self.client_id, self.clock = ctx, transport or Transport(), client_id, clock
        self.wait = wait or (lambda event, seconds: event.wait(seconds))   # cooperative: returns at once when cancelled
        self.lock = threading.Lock()
        self.bundle, self.loaded, self.problem, self.version = None, False, None, 0
        self.connection = 'unknown'      # the last VERIFIED state; never "connected" without a successful /user
        self.flow, self.flows = None, 0
        self.refreshing = None           # the single in-flight refresh: an Event, its result in self.refresh_result
        self.stopped = False

    # Stored connection ------------------------------------------------------------------------------------------------
    def _load(self):
        """(bundle or None, problem or None); loads once per instance (restart recovery: the stored bundle)."""
        with self.lock:
            if self.loaded:
                return self.bundle, self.problem
        try:
            raw = self.ctx.secrets.get(SECRET_NAME)
            problem = None
            bundle = _bundle(json.loads(raw.decode('utf-8'))) if raw is not None else None
            if raw is not None and bundle is None:
                problem = 'needs_attention'      # stored, but not a bundle this provider wrote
        except ValueError:
            bundle, problem = None, 'needs_attention'
        except Exception as exc:             # SecretError or ExtensionStopped: nothing claimed
            code = getattr(exc, 'code', None)
            bundle, problem = None, 'needs_attention' if code in ('corrupt', 'undecryptable') else 'unknown'
        with self.lock:
            if not self.loaded:
                self.bundle, self.problem, self.loaded = bundle, problem, True
                if bundle is None and problem is None:
                    self.connection = 'not_connected'
            return self.bundle, self.problem

    def _save(self, bundle, expect_version, flow=None, cancelled=None):
        """Write one coherent bundle, only if nothing else changed it meanwhile: 'saved', 'stale' (another sign-in or
        refresh won, the instance stopped, the sign-in `flow` is no longer the current one or was cancelled, or the
        request's `cancelled` Event is set: this result is dropped) or 'failed' (nothing stored; never "connected").

        The commit boundary: every one of those checks and the save happen under self.lock, and cancel_sign_in() and
        stop() set their cancellation under the same lock. So a cancel either comes first (nothing is saved) or after
        the save (the save stands: a cancel does not undo a completed commit). A request Event set by its caller without
        this lock is linearized the same way: seen at the check, or after the commit."""
        with self.lock:
            if self.stopped or self.version != expect_version:
                return 'stale'
            if flow is not None and (flow is not self.flow or flow.cancelled.is_set()):
                return 'stale'
            if cancelled is not None and cancelled.is_set():
                return 'stale'
            try:
                self.ctx.secrets.set(SECRET_NAME, json.dumps(bundle, separators=(',', ':')).encode('utf-8'))
            except Exception:
                return 'failed'
            self.bundle, self.problem, self.loaded = bundle, None, True
            self.version += 1
            return 'saved'

    # Contract -------------------------------------------------------------------------------------------------------
    def describe(self):
        bundle, problem = self._load()
        with self.lock:
            connection = problem or ('not_connected' if bundle is None else self.connection)
        return dict(label=self.LABEL, connection=connection, operations=['account.status'])

    def perform(self, operation, args, cancelled):
        if operation != 'account.status' or args != {}:
            raise ValueError('not offered')
        return dict(connection=self.check(cancelled))

    def check(self, cancelled=None):
        """The account's connection, verified with GET /user now: connected | not_connected | needs_attention | unknown."""
        cancelled = cancelled or threading.Event()
        bundle, problem = self._load()
        if problem:
            return problem
        if bundle is None:
            return 'not_connected'
        version = self.version
        if bundle['expires_at'] - REFRESH_MARGIN <= self.clock():
            bundle = self._refresh(bundle, cancelled)
            if not isinstance(bundle, dict):
                return self._record(bundle, version)
            version = self.version
        result = self._user(bundle, cancelled)
        if result == 'refresh':   # 401: one refresh, then one more try
            bundle = self._refresh(bundle, cancelled)
            if not isinstance(bundle, dict):
                return self._record(bundle, version)
            version = self.version
            result = self._user(bundle, cancelled)
            if result == 'refresh':
                result = 'needs_attention'
        return self._record(result, version)

    def _record(self, connection, version):
        """A verified state, only for the bundle it was verified with: an answer overtaken by a newer bundle, or arriving
        after the provider stopped, is neither kept nor returned (it is "unknown")."""
        with self.lock:
            if self.version != version or self.stopped:
                return 'unknown'
            self.connection = connection
        return connection

    def _user(self, bundle, cancelled):
        if cancelled.is_set():
            return 'unknown'
        try:
            status, data = self.transport.request('user', bearer=bundle['access_token'])
        except TransportError:
            return 'unknown'
        if cancelled.is_set():
            return 'unknown'                      # the answer came after the request was cancelled: not used
        if status == 200 and isinstance(data, dict) and data.get('id') is not None:
            return 'connected'
        if status == 401:
            return 'refresh'
        if status == 403:
            return 'needs_attention'
        return 'unknown'                      # 429, 5xx, anything unexpected: not known now

    def _refresh(self, bundle, cancelled):
        """Single flight: one refresh at a time per instance; others wait for its outcome. Returns the new bundle, or a
        connection word (needs_attention | unknown) when there is none."""
        with self.lock:
            waiting = self.refreshing
            if waiting is None:
                self.refreshing, base = threading.Event(), self.version
        if waiting is not None:
            waiting.wait(TIMEOUT * 2)
            with self.lock:
                return self.refresh_result if self.refresh_result is not None else 'unknown'
        outcome = 'unknown'
        try:
            if not cancelled.is_set() and not self.stopped:
                try:
                    status, data = self.transport.request('token', form=dict(client_id=bundle['client_id'], client_secret=bundle['client_secret'],
                                                                             code=bundle['refresh_token'], grant_type=GRANT_TYPE))
                except TransportError:
                    status, data = None, None
                answer = _token_answer(status, data)
                if answer:
                    access, refresh, expires = answer
                    renewed = dict(bundle, access_token=access, refresh_token=refresh, expires_at=int(self.clock()) + expires)
                    # Not saved when the request was cancelled meanwhile. If the service already rotated the pair, the
                    # stored (older) pair may no longer refresh: a later check then says needs_attention. Cancel cannot
                    # undo a rotation that already happened at the service.
                    saved = self._save(renewed, base, cancelled=cancelled)
                    # failed: the provider may already have rotated the old pair, so sign in again; stale: dropped
                    outcome = renewed if saved == 'saved' else 'needs_attention' if saved == 'failed' else 'unknown'
                elif status in (400, 401, 403):
                    outcome = 'needs_attention'   # the refresh itself was refused: sign in again
        finally:
            with self.lock:
                self.refresh_result = outcome
                event, self.refreshing = self.refreshing, None
            event.set()
        return outcome

    refresh_result = None

    # Sign-in (device flow) ----------------------------------------------------------------------------------------
    def begin_sign_in(self):
        """Start the device flow on one tracked module task. Raises SignInRefused."""
        if not self.client_id:
            raise SignInRefused('client_identity_missing')
        with self.lock:
            if self.stopped:
                raise SignInRefused('stopped')
            if self.flow is not None and self.flow.state in ('starting', 'pending', 'saving'):
                raise SignInRefused('already_pending')
            self.flows += 1
            flow = self.flow = _Flow(self.flows)
        try:
            self.ctx.spawn(self._run, flow)
        except Exception:
            self._end(flow, 'failed', 'stopped')
            raise SignInRefused('stopped')
        return flow.id

    def cancel_sign_in(self):
        """Cancel the current sign-in. Set under self.lock, the same lock its credential commit holds (see _save): a
        cancel either prevents the save or comes after it. After a completed save, cancel does not remove the stored
        connection (that is a later Disconnect)."""
        with self.lock:
            flow = self.flow
            if flow is None:
                return False
            flow.cancelled.set()
        return True

    def sign_in_state(self):
        """Backend-only presentation seam: {state, code?, user_code?, verification_url?, expires_in_s?}. Never tokens."""
        with self.lock:
            flow = self.flow
            if flow is None:
                return dict(state='idle')
            out = dict(state=flow.state)
            if flow.code:
                out['code'] = flow.code
            if flow.state == 'pending':
                out.update(user_code=flow.user_code, verification_url=flow.verification_url,
                           expires_in_s=max(0, int(flow.expires_at - self.clock())))
            return out

    def _end(self, flow, state, code=None):
        with self.lock:
            if flow.state not in ('success', 'failed', 'expired', 'cancelled'):
                flow.state, flow.code = state, code
                flow.user_code = flow.verification_url = None

    def _stopping(self, flow):
        return flow.cancelled.is_set() or self.stopped or self.ctx.stopping.is_set()

    def _run(self, flow):
        try:
            self._device_flow(flow)
        except Exception:
            self._end(flow, 'failed', 'internal')

    def _device_flow(self, flow):
        try:
            status, data = self.transport.request('device_code', query=dict(client_id=self.client_id, new_credentials='yes'))
        except TransportError:
            return self._end(flow, 'failed', 'network')
        if status == 429:
            return self._end(flow, 'failed', 'rate_limited')
        device, user_code = (data or {}).get('device_code'), (data or {}).get('user_code')
        interval, expires_in, url = (data or {}).get('interval'), (data or {}).get('expires_in'), (data or {}).get('verification_url')
        if not (status == 200 and isinstance(device, str) and TOKEN.fullmatch(device) and isinstance(user_code, str) and USER_CODE.fullmatch(user_code)
                and type(interval) is int and 1 <= interval <= MAX_INTERVAL and type(expires_in) is int and 30 <= expires_in <= 3600
                and isinstance(url, str) and VERIFICATION.fullmatch(url)):
            return self._end(flow, 'failed', 'bad_response')
        with self.lock:
            if self._stopping(flow):
                flow.state = 'cancelled'
                return
            flow.state, flow.user_code, flow.verification_url = 'pending', user_code, url
            flow.expires_at = self.clock() + expires_in
        credentials = None
        for _ in range(MAX_POLLS):
            self.wait(flow.cancelled, interval)
            if self._stopping(flow):
                return self._end(flow, 'cancelled')
            if self.clock() >= flow.expires_at:
                return self._end(flow, 'expired')
            try:
                status, data = self.transport.request('credentials', query=dict(client_id=self.client_id, code=device))
            except TransportError:
                continue                       # not known now: poll again within the code's lifetime
            if status == 200 and isinstance(data, dict) and isinstance(data.get('client_id'), str) and TOKEN.fullmatch(data['client_id']) \
                    and isinstance(data.get('client_secret'), str) and TOKEN.fullmatch(data['client_secret']):
                credentials = (data['client_id'], data['client_secret'])
                break
            if status == 429 or (isinstance(data, dict) and data.get('error_code') == 5):
                interval = min(MAX_INTERVAL, interval * 2)    # too many requests / slow down: back off
        if credentials is None:
            return self._end(flow, 'expired')
        if self._stopping(flow):
            return self._end(flow, 'cancelled')
        try:
            status, data = self.transport.request('token', form=dict(client_id=credentials[0], client_secret=credentials[1],
                                                                     code=device, grant_type=GRANT_TYPE))
        except TransportError:
            return self._end(flow, 'failed', 'network')
        answer = _token_answer(status, data)
        if answer is None:
            return self._end(flow, 'failed', 'rate_limited' if status == 429 else 'bad_response')
        access, refresh, expires = answer
        bundle = dict(v=1, client_id=credentials[0], client_secret=credentials[1], access_token=access, refresh_token=refresh,
                      expires_at=int(self.clock()) + expires)
        with self.lock:
            if self._stopping(flow) or flow is not self.flow:
                flow.state = 'cancelled'
                return
            flow.state = 'saving'
            version = self.version
        if not self._commit_sign_in(flow, bundle, version):
            return
        self._end(flow, 'success')
        self.check(flow.cancelled)            # verify now; "connected" only from a successful /user

    def _commit_sign_in(self, flow, bundle, version):
        # One atomic step (see _save): this flow still current and not cancelled, the instance not stopped, the version
        # unchanged, and the save. A module instance that ended is refused by ctx.secrets' own commit guard.
        saved = self._save(bundle, version, flow=flow)
        if saved != 'saved':
            self._end(flow, 'failed' if saved == 'failed' else 'cancelled', 'save_failed' if saved == 'failed' else None)
            return False
        with self.lock:
            self.connection = 'unknown'       # stored; verified by check() next
        return True

    def stop(self):
        with self.lock:   # the same commit boundary as cancel_sign_in
            self.stopped = True
            if self.flow is not None:
                self.flow.cancelled.set()
