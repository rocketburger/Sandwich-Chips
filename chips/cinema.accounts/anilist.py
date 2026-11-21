"""AniList account provider for Accounts & Services (cinema.accounts.provider/1), production code (2026-11-15).

Protocol authority (AniList API docs, read 2026-11-15):
- https://docs.anilist.co/guide/auth/authenticated-requests: POST https://graphql.anilist.co with "Authorization: Bearer
  <access token>", "Content-Type: application/json", "Accept: application/json", body {"query": ...}. Tokens are JWTs.
- https://docs.anilist.co/guide/auth/: OAuth2 (authorization code or implicit grant) needs a client registered at
  AniList (client id, redirect URL). Tokens are valid for 1 year; refresh tokens are not supported; scopes are not
  supported (a token gives almost full access to the user's data).
- https://docs.anilist.co/guide/graphql/queries/user: Viewer { id name } is the user the token belongs to.
- https://docs.anilist.co/guide/graphql/pagination and /reference/object/page, /reference/query: Page(page, perPage
  max 50) with ONE data field plus pageInfo; only pageInfo.hasNextPage is reliable; depth max 5000 entries.
  Page.mediaList(userId, type, status, sort); MediaList {mediaId, status, progress, media}; MediaListStatus CURRENT |
  PLANNING | COMPLETED | DROPPED | PAUSED | REPEATING; MediaListSort includes MEDIA_ID.
- https://docs.anilist.co/guide/rate-limiting: 90 requests a minute (30 while degraded); 429 with Retry-After.

What this provider does:
- Connect: the user pastes an access token. Since 2026-11-16, "Sign in with AniList" (begin_authorize) opens AniList's
  own consent page in the system browser for CINEMA'S OWN registered app, when its id is present in anilist-app.json
  (the developer's registration; none is shipped until it exists), and AniList's PIN page then shows the token to paste.
  Cinema never borrows another app's id, secret or relay (Otaku's or anyone's), registers nothing itself and automates
  no sign-in; without anilist-app.json only the paste is offered, as before. The token is checked
  with Viewer before anything is kept; the account is the Viewer's id from that answer, never a typed name. Viewer.name
  is checked for shape and discarded. A token whose JWT "exp" has passed is refused without any request (the claim is
  only used to refuse, never to accept or identify).
- Read: ONE read-only operation, watchlist.read: one page (50) of the connected user's list for one status, from ONE
  fixed query text (no consumer GraphQL), normalized to {media_id, media_type, status, progress, title, total, cover, format, year} (the last three, 0.10.4:
  for title cards; None when absent or unexpected). No
  mutation, history, scrobble, progress write or sync exists here.
- The stored connection is one bundle {"v": 1, "token": ..., "viewer": <int>} under ctx.secrets "anilist". The token is
  never in a URL, log, status, contract value or error; errors are fixed codes. The viewer id stays in this provider: it
  is the userId of the fixed list query and is never returned to anyone.
- Disconnect removes Cinema's copy only (AniList has no revoke for this; the token stays valid at AniList until it
  expires or the user revokes the app there).
"""
import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request

API_URL = 'https://graphql.anilist.co'
USER_AGENT = 'Cinema-Accounts/0.12.1'
SECRET_NAME = 'anilist'
TIMEOUT = 10.0
MAX_RESPONSE = {'viewer': 4096, 'list': 131072}
TOKEN = re.compile(r'[A-Za-z0-9_-]{8,1400}\.[A-Za-z0-9_-]{8,1400}\.[A-Za-z0-9_-]{8,1400}')
MAX_TOKEN = 2048
CLIENT = re.compile(r'[1-9][0-9]{0,9}')          # an AniList application id (Cinema's own registration; anilist-app.json)
NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')
PER_PAGE = 50
COVER = re.compile(r'https://s4\.anilist\.co/file/anilistcdn/[A-Za-z0-9/_.%-]{1,300}')   # = its declared art prefix (WATCHLIST)
FORMATS = ('tv', 'tv_short', 'movie', 'special', 'ova', 'ona', 'music', 'manga', 'novel', 'one_shot')
# Airing facts (0.12.0, 2026-11-21): AniList's own release status and next airing episode, read in the SAME list request,
# so what comes next on Home needs no per-title read (each was a detail and a franchise walk against AniList's 30-90
# requests a minute).
RELEASES = ('finished', 'releasing', 'not_yet_released', 'cancelled', 'hiatus')
MAX_AT = 2 ** 33
MAX_PAGES = 20                      # 1000 entries at most (AniList allows a depth of 5000)
MAX_ID = 2 ** 31 - 1
STATUSES = ('CURRENT', 'PLANNING', 'COMPLETED', 'DROPPED', 'PAUSED', 'REPEATING')
TYPES = ('ANIME', 'MANGA')
TITLE = re.compile(r'[^\x00-\x1f\x7f-\x9f]{1,200}')
COOLDOWN_MAX = 120.0
CONNECT_CODES = ('authentication_failed', 'token_expired', 'rate_limited', 'network', 'service_error', 'bad_response',
                 'save_failed', 'write_unconfirmed', 'stopped', 'internal')

# The only two query texts this provider ever sends.
QUERIES = {
    'viewer': 'query { Viewer { id name } }',
    'list': ('query ($userId: Int!, $type: MediaType!, $status: MediaListStatus!, $page: Int!, $perPage: Int!) '
             '{ Page(page: $page, perPage: $perPage) { pageInfo { hasNextPage } '
             'mediaList(userId: $userId, type: $type, status: $status, sort: [MEDIA_ID]) '
             '{ mediaId status progress media { id type episodes chapters title { userPreferred } '
             'format seasonYear coverImage { large } status nextAiringEpisode { episode airingAt } } } } }'),
}


class TransportError(Exception):
    """A fixed code only: network, redirect, too_large, unknown_query. Never a URL, header or response text. `notes`:
    the privacy-safe diagnostic of the answer, when there was one (see Transport.request)."""

    def __init__(self, code, notes=None):
        Exception.__init__(self, code)
        self.code = code
        self.notes = notes


# Privacy-safe diagnostics (2026-11-16, after a live "answer Cinema does not understand"): what kind of answer came
# back, never what it said. Only: the HTTP status, the media type's main part (checked against a pattern), the byte
# count (bounded by the read limit), and one fixed reason. Never the body, a header value beyond the media type, the
# token, the Viewer's id or name, a URL or an exception's text.
MEDIA = re.compile(r'[a-z]{1,20}/[a-z0-9.+-]{1,60}')
REASONS = ('too_large', 'not_json', 'not_object', 'redirect', 'graphql_errors', 'no_viewer', 'viewer_id', 'viewer_name', 'http_status',
           'edge_refused')


def edge_refused(data):
    """True for the network edge's own JSON refusal (an object with "cloudflare_error": true), which answers before
    AniList's API: never a GraphQL answer, never about the token. Only that one boolean is read."""
    return isinstance(data, dict) and data.get('cloudflare_error') is True


def _notes(status, headers, size):
    media = (headers.get('Content-Type') or '').split(';')[0].strip().lower() if headers is not None else ''
    return dict(http=status if type(status) is int and 100 <= status <= 599 else None, media=media if MEDIA.fullmatch(media) else ('other' if media else None),
                bytes=size if type(size) is int and size >= 0 else None)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a 3xx is answered as itself, never followed (the token is never sent anywhere else)


def _variables(name, variables):
    if name == 'viewer':
        return variables is None
    return (isinstance(variables, dict) and set(variables) == {'userId', 'type', 'status', 'page', 'perPage'}
            and type(variables['userId']) is int and 0 < variables['userId'] <= MAX_ID and variables['type'] in TYPES
            and variables['status'] in STATUSES and type(variables['page']) is int and 1 <= variables['page'] <= MAX_PAGES
            and variables['perPage'] == PER_PAGE)


class Transport(object):
    """HTTPS POST to graphql.anilist.co only, the two named queries only, the token only in the Authorization header,
    bounded time and size, no redirects, no proxy-supplied credentials."""

    diagnoses = True   # request() fills `notes` (a consumer checks this flag; a substituted transport may not)

    def __init__(self, opener=None, timeout=TIMEOUT):
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.timeout = timeout

    def request(self, name, bearer, variables=None, notes=None):
        """(HTTP status, dict or None, retry_after seconds or None). Raises TransportError. `notes` (a dict, optional) is
        filled with the answer's privacy-safe diagnostic: {http, media, bytes, reason?}."""
        notes = notes if isinstance(notes, dict) else {}
        if name not in QUERIES or not _variables(name, variables):
            raise TransportError('unknown_query')
        body = dict(query=QUERIES[name])
        if variables is not None:
            body['variables'] = variables
        # User-Agent (0.10.3): the request names itself, as the Anime extension's catalogue transport always has. Without
        # it, urllib sends its default "Python-urllib/x.y", which AniList's network edge refuses before the API: HTTP
        # 403, a 708-byte JSON edge error 1010 "browser_signature_banned" (observed 2026-11-16 with an anonymous public
        # catalogue query; the same request with a named client answered 200). Nothing else about the request changes.
        request = urllib.request.Request(API_URL, data=json.dumps(body).encode('utf-8'), method='POST', headers={
            'Authorization': 'Bearer ' + bearer, 'Content-Type': 'application/json', 'Accept': 'application/json',
            'User-Agent': USER_AGENT})
        limit = MAX_RESPONSE[name]
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as answered:
                response = answered
            with response:
                status = response.getcode() if hasattr(response, 'getcode') else response.status
                headers = getattr(response, 'headers', None)
                retry = headers.get('Retry-After') if headers is not None else None
                raw = response.read(limit + 1)
        except (urllib.error.URLError, OSError, ValueError):
            raise TransportError('network')   # never the exception's own text
        notes.update(_notes(status, headers, len(raw)))
        if 300 <= status < 400:
            notes['reason'] = 'redirect'
            raise TransportError('redirect', dict(notes))
        if len(raw) > limit:
            notes['reason'] = 'too_large'    # bytes = limit + 1: "more than the limit", never the full size
            raise TransportError('too_large', dict(notes))
        try:
            value = json.loads(raw.decode('utf-8')) if raw else None
        except (UnicodeDecodeError, ValueError):
            value = None
            notes['reason'] = 'not_json'
        if value is not None and not isinstance(value, dict):
            notes['reason'] = 'not_object'
        elif value is None and 'reason' not in notes:
            notes['reason'] = 'not_json'     # an empty body
        try:
            retry = min(float(retry), COOLDOWN_MAX) if retry is not None and 0 <= float(retry) else None
        except (TypeError, ValueError):
            retry = None
        return status, value if isinstance(value, dict) else None, retry


def token_expired(token, now):
    """True only when the token is a JWT whose payload has a numeric "exp" at or before now. Anything undecodable is
    not called expired (Viewer decides). The claim is never used to accept a token or to identify an account."""
    try:
        part = token.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)).decode('utf-8'))
    except Exception:
        return False
    exp = payload.get('exp') if isinstance(payload, dict) else None
    return type(exp) in (int, float) and exp <= now


def _failure(status, data):
    """The fixed code for an answer that is not a success: authentication_failed | rate_limited | service_error |
    bad_response. AniList's documented 429 carries errors [{status: 429}]; an invalid or expired token is read
    defensively (HTTP 401, or an error whose status is 400/401 with the message "Invalid token": observed AniList
    behaviour, not documented)."""
    errors = data.get('errors') if isinstance(data, dict) else None
    statuses = [e.get('status') for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
    messages = [e.get('message') for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
    if status == 429 or 429 in statuses:
        return 'rate_limited'
    if status == 401 or 401 in statuses or ('Invalid token' in messages and (status == 400 or 400 in statuses)):
        return 'authentication_failed'
    if isinstance(status, int) and status >= 500:
        return 'service_error'
    if edge_refused(data):
        return 'service_error'   # refused before AniList's API: a service problem, not "an answer Cinema does not understand"
    return 'service_error' if statuses else 'bad_response'


def viewer_reason(status, data):
    """The fixed REASONS code of a Viewer answer that is not accepted (None when it is). Reads only shapes."""
    if not isinstance(data, dict):
        return None                                  # the transport already named it (not_json / not_object)
    if edge_refused(data):
        return 'edge_refused'
    if data.get('errors'):
        return 'graphql_errors'
    if status != 200:
        return 'http_status'
    found = data.get('data')
    who = found.get('Viewer') if isinstance(found, dict) else None
    if not isinstance(who, dict):
        return 'no_viewer'
    if not (type(who.get('id')) is int and 0 < who['id'] <= MAX_ID):
        return 'viewer_id'
    if not (isinstance(who.get('name'), str) and who['name'].strip()):
        return 'viewer_name'
    return None


def viewer(status, data):
    """One Viewer answer -> ('connected', viewer id) | ('failed', code). Pure; reads only data.Viewer.{id,name}.

    2026-11-16 correction: the account is the Viewer's integer id (checked strictly, as before); the name is only
    required to be text and is discarded. The earlier rule that a name match [A-Za-z0-9_-]{1,64} and that Viewer hold
    exactly {id, name} is NOT stated by AniList's contract, so a real account could be refused by Cinema's own rule
    (shown as "an answer Cinema does not understand")."""
    if status == 200 and isinstance(data, dict) and not data.get('errors'):
        if viewer_reason(status, data) is None:
            return 'connected', data['data']['Viewer']['id']
        return 'failed', 'bad_response'
    return 'failed', _failure(status, data)


class ReadRefused(Exception):
    """A list read that cannot be answered: a fixed code only (not_connected | needs_attention | invalid_arguments |
    cancelled | changed | stopped | authentication_failed | token_expired | rate_limited | network | service_error |
    bad_response). Through the contract it becomes "provider_failed": never an empty list."""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


def page_entries(status, data, media_type, list_status):
    """One list answer for (media_type, list_status) -> (entries, has_next). Only the queried fields are read; every
    entry must be of the type and status asked for. Raises ReadRefused."""
    if status != 200 or not isinstance(data, dict) or data.get('errors'):
        raise ReadRefused(_failure(status, data))
    found = data.get('data')
    page = found.get('Page') if isinstance(found, dict) else None
    info = page.get('pageInfo') if isinstance(page, dict) else None
    items = page.get('mediaList') if isinstance(page, dict) else None
    if not isinstance(info, dict) or type(info.get('hasNextPage')) is not bool or not isinstance(items, list) or len(items) > PER_PAGE:
        raise ReadRefused('bad_response')
    entries, seen = [], set()
    for item in items:
        media = item.get('media') if isinstance(item, dict) else None
        title = media.get('title') if isinstance(media, dict) else None
        if not (isinstance(item, dict) and isinstance(media, dict) and isinstance(title, dict)):
            raise ReadRefused('bad_response')
        mid, progress, name = item.get('mediaId'), item.get('progress'), title.get('userPreferred')
        total = media.get('episodes' if media_type == 'ANIME' else 'chapters')
        if not (type(mid) is int and 0 < mid <= MAX_ID and media.get('id') == mid and mid not in seen
                and item.get('status') == list_status and media.get('type') == media_type
                and (progress is None or (type(progress) is int and 0 <= progress <= 100000))
                and (total is None or (type(total) is int and 0 < total <= 100000))
                and (name is None or (isinstance(name, str) and TITLE.fullmatch(name)))):
            raise ReadRefused('bad_response')
        seen.add(mid)
        # What the list's title cards show (0.10.4): decorative, so a missing or unexpected value is left out (None),
        # never a refusal of the page. The cover only from AniList's own image CDN.
        cover = media.get('coverImage') if isinstance(media.get('coverImage'), dict) else {}
        cover = cover.get('large') if isinstance(cover.get('large'), str) and COVER.fullmatch(cover['large']) else None
        form = media.get('format').lower() if isinstance(media.get('format'), str) and media['format'].lower() in FORMATS else None
        year = media.get('seasonYear') if type(media.get('seasonYear')) is int and 1900 < media['seasonYear'] < 2200 else None
        # Airing facts (0.12.0): left out (None) when missing or unexpected, never a refusal of the page; then Next up
        # reads that title as before.
        # AniList's own release words are read HERE and stated in the contract's neutral terms: how many episodes have
        # aired, and whether more are still to come (boundary: nothing of AniList's vocabulary is fixed in the contract).
        release = media.get('status').lower() if isinstance(media.get('status'), str) and media['status'].lower() in RELEASES else None
        upcoming = media.get('nextAiringEpisode') if isinstance(media.get('nextAiringEpisode'), dict) else {}
        ep, at = upcoming.get('episode'), upcoming.get('airingAt')
        next_airing = dict(episode=ep, at=at) if type(ep) is int and 0 < ep <= 100000 and type(at) is int and 0 < at < MAX_AT else None
        aired = next_airing['episode'] - 1 if next_airing else (total if release in ('finished', 'cancelled') and total else None)
        airing = None if release is None else release in ('releasing', 'not_yet_released', 'hiatus')
        entries.append(dict(media_id=mid, media_type=media_type.lower(), status=list_status.lower(),
                            progress=progress if progress is not None else 0, title=name, total=total,
                            cover=cover, format=form, year=year, aired=aired, airing=airing, next_airing=next_airing))
    return entries, info['hasNextPage']


def _detail(notes):
    """The attempt's privacy-safe diagnostic, or None: {http, media, bytes, reason}, each checked again here."""
    if not isinstance(notes, dict) or not notes:
        return None
    http, media, size, reason = notes.get('http'), notes.get('media'), notes.get('bytes'), notes.get('reason')
    return dict(http=http if type(http) is int and 100 <= http <= 599 else None,
                media=media if isinstance(media, str) and (media == 'other' or MEDIA.fullmatch(media)) else None,
                bytes=size if type(size) is int and 0 <= size <= max(MAX_RESPONSE.values()) + 1 else None,
                reason=reason if reason in REASONS else None)


class ConnectRefused(Exception):
    """invalid_key | already_pending | stopped"""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


def _bundle(value):
    if not isinstance(value, dict) or set(value) != {'v', 'token', 'viewer'} or value['v'] != 1:
        return None
    if not (isinstance(value['token'], str) and len(value['token']) <= MAX_TOKEN and TOKEN.fullmatch(value['token'])):
        return None
    return value if type(value['viewer']) is int and 0 < value['viewer'] <= MAX_ID else None


def _encode(bundle):
    return json.dumps(bundle, separators=(',', ':'), sort_keys=True).encode('utf-8')


class _Attempt(object):
    def __init__(self, ident):
        self.id, self.cancelled, self.state, self.code = ident, threading.Event(), 'verifying', None


class AniListProvider(object):
    """One instance per module instance. Contract surface: describe(), perform(operation, args, cancelled).
    Settings seams (cinema.accounts.connect/1 through backend.settings_action): begin_connect, connect_state,
    cancel_connect, disconnect, begin_check, checking, connection_state."""

    LABEL = 'AniList'

    def __init__(self, ctx, transport=None, clock=None, monotonic=None):
        self.ctx, self.transport = ctx, transport or Transport()
        self.clock, self.monotonic = clock or time.time, monotonic or time.monotonic
        self.lock = threading.Lock()
        self.bundle, self.raw, self.loaded, self.problem, self.version = None, None, False, None, 0
        self.connection = 'unknown'          # last VERIFIED; unknown after start until checked
        self.attempt, self.attempts = None, 0
        self.checks = 0                      # checks running now
        self.cooldown = 0.0                  # monotonic time before which no request is sent (after a 429)
        self.stopped = False
        self.awaiting = None                 # monotonic deadline of an open browser sign-in step, or None

    # Stored connection ----------------------------------------------------------------------------------------------
    def _load(self):
        with self.lock:
            if self.loaded:
                return self.bundle, self.problem
        try:
            raw = self.ctx.secrets.get(SECRET_NAME)
            bundle = _bundle(json.loads(raw.decode('utf-8'))) if raw is not None else None
            problem = 'needs_attention' if raw is not None and bundle is None else None
        except ValueError:
            raw, bundle, problem = None, None, 'needs_attention'
        except Exception as exc:                 # SecretError or ExtensionStopped: nothing claimed
            raw, bundle = None, None
            problem = 'needs_attention' if getattr(exc, 'code', None) in ('corrupt', 'undecryptable') else 'unknown'
        with self.lock:
            if not self.loaded:
                self.bundle, self.raw, self.problem, self.loaded = bundle, raw if bundle else None, problem, True
                if bundle is None and problem is None:
                    self.connection = 'not_connected'
            return self.bundle, self.problem

    def connection_state(self):
        bundle, problem = self._load()
        with self.lock:
            return dict(connection=problem or ('not_connected' if bundle is None else self.connection), entitlement='unknown')

    # Contract -------------------------------------------------------------------------------------------------------
    # Its list, DECLARED (2026-11-18, audit R3): media ids are AniList ids, the kinds it reads, and where its covers are.
    # The contract validates this provider's answers against exactly this; nothing of AniList is fixed in the contract.
    WATCHLIST = dict(namespace='anilist', media_types=['anime', 'manga'], art=['https://s4.anilist.co/file/anilistcdn/'])

    def describe(self):
        return dict(label=self.LABEL, connection=self.connection_state()['connection'], operations=['account.status', 'watchlist.read'],
                    watchlist=dict(self.WATCHLIST, media_types=list(self.WATCHLIST['media_types']), art=list(self.WATCHLIST['art'])))

    def perform(self, operation, args, cancelled):
        if operation == 'account.status' and args == {}:
            return dict(connection=self.check(cancelled))
        if operation == 'watchlist.read' and isinstance(args, dict) and set(args) == {'media_type', 'status', 'page'}:
            return self.read(args['media_type'], args['status'], args['page'], cancelled)
        raise ValueError('not offered')

    def _send(self, name, token, variables, cancelled, admit, notes=None):
        """One request, no retry, honouring a 429 cool-down. (status, data). Raises ReadRefused (the admission's own code |
        rate_limited | cancelled | network | bad_response).

        Dispatch boundary (as Premiumize's _current): `admit()` (None when the request may go, else a fixed code) is
        evaluated under self.lock together with the cool-down and cancel checks, IMMEDIATELY before transport.request. A
        stop, disconnect, reconnect or cancel that takes self.lock before that point means nothing is sent. The lock is
        NOT held over the network: one that comes after admission cannot recall the request already started; its
        answer is then refused by the caller's post-result checks (version, stop, cancel), never committed or returned."""
        with self.lock:
            refused = admit()
            if refused:
                raise ReadRefused(refused)
            if cancelled.is_set():
                raise ReadRefused('cancelled')
            if self.monotonic() < self.cooldown:
                raise ReadRefused('rate_limited')            # nothing is sent during AniList's own timeout
        try:
            if notes is not None and getattr(self.transport, 'diagnoses', False):
                status, data, retry = self.transport.request(name, bearer=token, variables=variables, notes=notes)
            else:
                status, data, retry = self.transport.request(name, bearer=token, variables=variables)
        except TransportError as exc:
            if notes is not None and exc.notes:
                notes.update(exc.notes)
            raise ReadRefused('network' if exc.code in ('network', 'redirect') else 'bad_response')
        finally:
            token = None
        if status == 429:
            with self.lock:
                self.cooldown = max(self.cooldown, self.monotonic() + (retry if retry is not None else 60.0))
        return status, data

    def _stored(self, version):
        """The admission of a request made with the stored token taken at `version` (see _send)."""
        return lambda: 'stopped' if self.stopped else ('changed' if self.version != version else None)

    def read(self, media_type, list_status, page, cancelled=None):
        """watchlist.read: one page of the connected user's list. The token, the viewer id and the version are taken
        together under the lock. It is not sent if, at the dispatch boundary (_send), the provider has stopped, the
        connection was replaced or removed, or the request was cancelled; once sent it cannot be recalled, and its answer
        is refused afterwards on the same conditions. Raises ReadRefused."""
        cancelled = cancelled or threading.Event()
        kind, wanted = (media_type.upper() if isinstance(media_type, str) else None), (list_status.upper() if isinstance(list_status, str) else None)
        if kind not in TYPES or wanted not in STATUSES or type(page) is not int or not 1 <= page <= MAX_PAGES \
                or media_type != kind.lower() or list_status != wanted.lower():
            raise ReadRefused('invalid_arguments')
        self._load()
        with self.lock:
            if self.stopped:
                raise ReadRefused('stopped')
            if self.problem:
                raise ReadRefused('needs_attention')
            if self.bundle is None:
                raise ReadRefused('not_connected')
            token, user, version = self.bundle['token'], self.bundle['viewer'], self.version
        if token_expired(token, self.clock()):
            self._record('needs_attention', version, cancelled)
            raise ReadRefused('token_expired')
        try:
            status, data = self._send('list', token, dict(userId=user, type=kind, status=wanted, page=page, perPage=PER_PAGE), cancelled,
                                      self._stored(version))
        finally:
            token = None
        with self.lock:
            if self.stopped:
                raise ReadRefused('stopped')
            if self.version != version:
                raise ReadRefused('changed')
        if cancelled.is_set():
            raise ReadRefused('cancelled')
        try:
            entries, more = page_entries(status, data, kind, wanted)
        except ReadRefused as refused:
            if refused.code == 'authentication_failed':
                self._record('needs_attention', version, cancelled)
            raise
        # 2026-11-16 (restart fix): AniList answered this list with the stored token for the stored viewer, so the token
        # works. A connection not verified since start ("unknown") becomes "connected"; a verified state is not replaced.
        with self.lock:
            if self.version == version and not self.stopped and self.connection == 'unknown':
                self.connection = 'connected'
        return dict(entries=entries, has_next=more and page < MAX_PAGES, truncated=more and page == MAX_PAGES)

    def check(self, cancelled=None):
        """The stored token's connection, verified with Viewer now: connected | not_connected | needs_attention | unknown.
        A stored token is "connected" only if Viewer answers for the SAME account it was connected as."""
        cancelled = cancelled or threading.Event()
        self._load()
        with self.lock:
            if self.stopped:
                return 'unknown'
            if self.problem:
                return self.problem
            if self.bundle is None:
                return 'not_connected'
            token, user, version = self.bundle['token'], self.bundle['viewer'], self.version
            self.checks += 1
        try:
            if token_expired(token, self.clock()):
                return self._record('needs_attention', version, cancelled)
            try:
                status, data = self._send('viewer', token, None, cancelled, self._stored(version))
            except ReadRefused:
                return 'unknown'      # not sent (stopped, changed, cancelled, cooling down) or not answered
            word, detail = viewer(status, data)
            if word == 'connected':
                return self._record('connected' if detail == user else 'needs_attention', version, cancelled)
            return self._record('needs_attention' if detail == 'authentication_failed' else 'unknown', version, cancelled)
        finally:
            token = None
            with self.lock:
                self.checks -= 1

    def _record(self, connection, version, cancelled):
        with self.lock:
            if self.version != version or self.stopped or cancelled.is_set():
                return 'unknown'
            if connection != 'unknown':
                self.connection = connection
        return connection

    def begin_check(self):
        """Settings' "Check now": 'started' | 'already_checking' | 'not_connected' | 'stopped'."""
        bundle, problem = self._load()
        with self.lock:
            if self.stopped:
                return 'stopped'
            if bundle is None and not problem:
                return 'not_connected'
            if self.checks:
                return 'already_checking'
            self.checks += 1          # held until the spawned check has started its own count

        def run():
            try:
                self.check()
            finally:
                with self.lock:
                    self.checks -= 1
        try:
            self.ctx.spawn(run)
        except Exception:
            with self.lock:
                self.checks -= 1
            return 'stopped'
        return 'started'

    def checking(self):
        with self.lock:
            return self.checks > 0

    # Connect (Settings; explicit user input) --------------------------------------------------------------------------
    def begin_connect(self, token):
        """Verify a token the user has just pasted and, if Viewer accepts it, make it this provider's connection. Runs on
        one tracked module task. Raises ConnectRefused. The token is never logged or kept unless committed."""
        if not isinstance(token, str) or len(token) > MAX_TOKEN or not TOKEN.fullmatch(token):
            raise ConnectRefused('invalid_key')
        with self.lock:
            if self.stopped:
                raise ConnectRefused('stopped')
            if self.attempt is not None and self.attempt.state in ('verifying', 'saving'):
                raise ConnectRefused('already_pending')
            self.attempts += 1
            attempt = self.attempt = _Attempt(self.attempts)
            self.awaiting = None                 # a pasted token ends the browser step (verified as any token)
        try:
            self.ctx.spawn(self._connect, attempt, token)
        except Exception:
            self._end(attempt, 'failed', 'stopped')
            raise ConnectRefused('stopped')
        return attempt.id

    # Sign in with AniList (2026-11-16) ---------------------------------------------------------------------------------
    # AniList's documented grant for an app that cannot keep a secret is the implicit grant: the system browser opens
    # https://anilist.co/api/v2/oauth/authorize?client_id=<CINEMA'S OWN APP>&response_type=token, the user approves there,
    # and AniList returns the token to the redirect registered for that app. Cinema's app is registered with AniList's
    # documented PIN redirect (https://anilist.co/api/v2/oauth/pin), so AniList SHOWS the token and the user pastes it
    # here once; it is then checked by the same Viewer verification and kept by the same secret store as before. An
    # automatic return (a loopback or custom-scheme redirect) is NOT used: it is not verified to work with AniList.
    # The client id is Cinema's own developer-owned registration, read from this module's anilist-app.json
    # ({"client_id": "<digits>"}); without it there is no sign-in button and the token paste stays. No secret exists or
    # is shipped (implicit grant), and nothing of any other application is used.
    AUTHORIZE = 'https://anilist.co/api/v2/oauth/authorize?client_id=%s&response_type=token'
    AWAIT = 600.0                        # how long Cinema waits for the token after opening the browser

    def client_id(self):
        try:
            import os
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'anilist-app.json')
            with open(path, 'rb') as handle:
                value = json.loads(handle.read(4096).decode('utf-8')).get('client_id')
        except Exception:
            return None
        return value if isinstance(value, str) and CLIENT.fullmatch(value) else None

    def begin_authorize(self, opener=None):
        """Open AniList's consent page in the system browser; then await the pasted token (bounded). An outcome:
        browser_opened | not_registered | already_pending | stopped | unsupported."""
        client = self.client_id()
        if client is None:
            return 'not_registered'
        with self.lock:
            if self.stopped:
                return 'stopped'
            if self.attempt is not None and self.attempt.state in ('verifying', 'saving'):
                return 'already_pending'
        try:
            import webbrowser
            opened = (opener or webbrowser.open)(self.AUTHORIZE % client)
        except Exception:
            opened = False
        if opened is False:
            return 'unsupported'
        with self.lock:
            self.awaiting = self.monotonic() + self.AWAIT
        return 'browser_opened'

    def _awaiting(self):
        """'awaiting_token' while the browser step is open, 'timed_out' once its time passed (said once), else None."""
        until = getattr(self, 'awaiting', None)
        if until is None:
            return None
        if self.monotonic() >= until:
            self.awaiting = None
            return 'timed_out'
        return 'awaiting_token'

    def connect_state(self):
        with self.lock:
            attempt = self.attempt
            waiting = self._awaiting() if attempt is None or attempt.state not in ('verifying', 'saving') else None
            if waiting == 'awaiting_token':
                return dict(state='awaiting_token')
            if waiting == 'timed_out':
                return dict(state='cancelled', code='timed_out')
            if attempt is None:
                return dict(state='idle')
            out = dict(state=attempt.state)
            if attempt.code:
                out['code'] = attempt.code
            return out

    def connect_detail(self):
        """The last attempt's privacy-safe diagnostic ({http, media, bytes, reason}) when it failed on AniList's answer,
        else None. Never the token, body, name or id (see _detail)."""
        with self.lock:
            attempt = self.attempt
            if attempt is None or attempt.state not in ('failed', 'rejected'):
                return None
            return getattr(attempt, 'detail', None)

    def cancel_connect(self):
        with self.lock:
            attempt = self.attempt
            if getattr(self, 'awaiting', None) is not None and (attempt is None or attempt.state not in ('verifying', 'saving')):
                self.awaiting = None             # the browser step: Cinema stops waiting (nothing was kept)
                return True
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

    def _connect(self, attempt, token):
        try:
            if token_expired(token, self.clock()):
                return self._end(attempt, 'rejected', 'token_expired')
            notes = {}
            try:
                status, data = self._send('viewer', token, None, attempt.cancelled,
                                          lambda: 'cancelled' if self._halted(attempt) else None, notes=notes)   # a superseded candidate is never sent
            except ReadRefused as refused:
                if self._halted(attempt) or refused.code == 'cancelled':
                    return self._end(attempt, 'cancelled')
                attempt.detail = _detail(notes)
                return self._end(attempt, 'failed', refused.code if refused.code in CONNECT_CODES else 'service_error')
            if self._halted(attempt):
                return self._end(attempt, 'cancelled')
            word, detail = viewer(status, data)
            if word != 'connected':
                reason = viewer_reason(status, data)
                if reason:
                    notes.setdefault('reason', reason)
                attempt.detail = _detail(notes)
                # The candidate is not accepted: the previous connection is untouched.
                if detail == 'authentication_failed':
                    return self._end(attempt, 'rejected', 'authentication_failed')
                return self._end(attempt, 'failed', detail if detail in CONNECT_CODES else 'service_error')
            self._commit(attempt, {'v': 1, 'token': token, 'viewer': detail})
        except Exception:
            self._end(attempt, 'failed', 'internal')
        finally:
            token = None

    def _commit(self, attempt, bundle):
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
                    attempt.state, attempt.code = 'failed', 'stopped' if code in (None, 'stale') else 'save_failed'
                    return
                self._unconfirmed(attempt, bundle, new, prior)
                return
            self.bundle, self.raw, self.problem, self.loaded = bundle, new, None, True
            self.version += 1
            self.connection = 'connected'
            attempt.state, attempt.code = 'connected', None

    def _unconfirmed(self, attempt, bundle, new, prior):
        """write_failed: re-read the store and say what it holds (holds self.lock)."""
        try:
            stored = self.ctx.secrets.get(SECRET_NAME)
        except Exception:
            stored = False
        if stored is not False and stored == prior:
            attempt.state, attempt.code = 'failed', 'save_failed'
            return
        self.version += 1
        if stored == new:
            self.bundle, self.raw, self.problem, self.loaded = bundle, new, None, True
            self.connection = 'connected'
            attempt.state, attempt.code = 'connected', None
            return
        self.loaded = False
        self.connection = 'unknown'
        attempt.state, attempt.code = 'uncertain', 'write_unconfirmed'

    # Disconnect (explicit user action) --------------------------------------------------------------------------------
    def disconnect(self):
        """Remove Cinema's copy of the token (local only). 'disconnected' | 'not_connected' | 'failed' | 'stopped'."""
        with self.lock:
            if self.stopped:
                return 'stopped'
            if self.attempt is not None:
                self.attempt.cancelled.set()
            self.version += 1
            try:
                removed = self.ctx.secrets.delete(SECRET_NAME)
            except Exception:
                self.loaded = False
                self.connection = 'unknown'
                return 'failed'
            self.bundle, self.raw, self.problem, self.loaded = None, None, None, True
            self.connection = 'not_connected'
            return 'disconnected' if removed else 'not_connected'

    def stop(self):
        with self.lock:
            self.stopped = True
            if self.attempt is not None:
                self.attempt.cancelled.set()
