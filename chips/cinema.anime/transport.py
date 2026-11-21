"""Fixed-origin HTTPS transport for the Anime extension (no Kodi, no add-on code).

Only the origins listed here are ever contacted; no redirect is followed; time and size are bounded; failures are fixed
codes (never a URL, header or response text). Tests substitute `Transport` entirely (offline)."""
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ORIGINS = {
    'anilist': ('POST', 'https://graphql.anilist.co/'),            # public GraphQL: catalogue reads only, no token
    'nyaa': ('GET', 'https://nyaa.si/'),                           # RSS search feed (?page=rss&q=...)
    'animetosho': ('GET', 'https://feed.animetosho.org/json'),     # JSON search feed (?q=...), the documented host
    # The same documented feed on the site's own host (2026-11-17): tried when the feed host does not resolve, so a
    # retirement is told apart from an outage.
    'animetosho_site': ('GET', 'https://animetosho.org/feed/json'),
    # nekoBT's public JSON API (2026-11-17): media search, one media's episode list, torrent search. Read-only, no key.
    'nekobt': ('GET', 'https://nekobt.to/api/v1/'),
    # Torrentio's public Stremio stream answers by Kitsu id, and Kitsu's public id mappings (MyAnimeList id -> Kitsu id).
    'torrentio': ('GET', 'https://torrentio.strem.fun/'),
    'kitsu': ('GET', 'https://kitsu.app/api/edge/'),
    # ani.zip's public mappings for one AniList entry (2026-11-17): its episodes' own titles, air dates, stills and
    # season-local/absolute numbers (episode_meta.py). Read-only, no key.
    'anizip': ('GET', 'https://api.ani.zip/mappings'),
}
# The only paths an origin with a path may be asked for (everything else is refused before any request).
PATHS = {
    'nekobt': re.compile(r'media/search|media/[a-z]?[0-9]{1,12}|torrents/search'),
    'torrentio': re.compile(r'stream/(?:series/kitsu:[0-9]{1,9}:[0-9]{1,5}|movie/kitsu:[0-9]{1,9})\.json'),
    'kitsu': re.compile(r'mappings'),
}
ACCEPT = {'kitsu': 'application/vnd.api+json'}     # Kitsu's JSON:API refuses any other Accept (HTTP 406)
MAX_BYTES = 2 * 1024 * 1024
USER_AGENT = 'Cinema-Anime/0.5'


class TransportError(Exception):
    """network | unresolved | redirect | too_large | http_<status> | unknown_origin | bad_request"""

    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Pacer(object):
    """AniList's request allowance, shared (2026-11-21, alpha by: "watchlists not loading").

    AniList allows 90 requests a minute per address (30 while degraded, as it was on 2026-11-21) and answers 429 with
    Retry-After past that. This extension's catalogue reads are unauthenticated but come from the same address as the
    user's own AniList list reads (Accounts & Services): Home's next-up rows read up to 12 entries, each a detail and a
    franchise walk, so one Home load could spend the whole allowance; AniList then refused the user's list too, and
    Accounts held AniList for its Retry-After ("AniList could not give your list just now"). So this extension paces
    itself: at most PER_MINUTE requests in any 60 s, fewer when AniList's own X-RateLimit-Remaining says the address
    has little left (RESERVE is kept for the user's list), and none during a 429's Retry-After. A request that would
    have to wait longer than its own time limit is not sent ('http_429', read as rate_limited by the catalogue)."""
    PER_MINUTE, RESERVE, WINDOW, LOW_GAP = 20, 8, 60.0, 4.0

    def __init__(self, clock=None, sleep=None):
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.lock = threading.Lock()
        self.sent = []          # monotonic times of the requests in the current window (reserved slots included)
        self.hold = 0.0         # no request before this (a 429's Retry-After, or a low remaining allowance)

    def admit(self, timeout):
        """Reserve a slot and wait for it. The seconds left of `timeout` after the wait; raises TransportError('http_429')
        without waiting when the slot is further away than `timeout`."""
        with self.lock:
            now = self.clock()
            self.sent = [t for t in self.sent if t > now - self.WINDOW]
            slot = max(now, self.hold)
            if len(self.sent) >= self.PER_MINUTE:
                slot = max(slot, self.sent[-self.PER_MINUTE] + self.WINDOW)
            if slot - now > timeout:
                raise TransportError('http_429')
            self.sent.append(slot)
            self.sent.sort()
        if slot > now:
            self.sleep(slot - now)
        return max(1.0, timeout - (slot - now))

    def answered(self, status, headers):
        """What AniList said about the allowance: a 429's Retry-After, or how many requests the address has left."""
        def number(name):
            try:
                return float(headers.get(name)) if headers is not None and headers.get(name) is not None else None
            except (TypeError, ValueError):
                return None
        with self.lock:
            now = self.clock()
            if status == 429:
                retry = number('Retry-After')
                self.hold = max(self.hold, now + (min(max(retry, 1.0), 120.0) if retry is not None else self.WINDOW))
                return
            left = number('X-RateLimit-Remaining')
            if left is not None and left <= self.RESERVE:
                self.hold = max(self.hold, now + self.LOW_GAP * (self.RESERVE + 1 - max(left, 0)))


ANILIST = Pacer()


class Transport(object):
    def __init__(self, opener=None, pacer=None):
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.pacer = pacer or ANILIST

    def fetch(self, origin, params=None, body=None, timeout=20.0, path=None):
        """bytes of one answer from a named origin. GET: `params` become the query string; POST: `body` is JSON. `path`:
        only for an origin with PATHS, and only a path its pattern allows."""
        if origin not in ORIGINS:
            raise TransportError('unknown_origin')
        method, base = ORIGINS[origin]
        if (method == 'POST') != (body is not None):
            raise TransportError('bad_request')
        if (path is not None) != (origin in PATHS) or (path is not None and not PATHS[origin].fullmatch(path)):
            raise TransportError('bad_request')
        url = base + (path or '') + ('?' + urllib.parse.urlencode(params) if params else '')
        data = json.dumps(body).encode('utf-8') if body is not None else None
        headers = {'User-Agent': USER_AGENT, 'Accept': ACCEPT.get(origin, 'application/json, application/rss+xml, text/xml')}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        paced = origin == 'anilist'
        if paced:
            timeout = self.pacer.admit(timeout)
        try:
            try:
                response = self.opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as answered:
                response = answered
            with response:
                status = response.getcode() if hasattr(response, 'getcode') else response.status
                if paced:
                    self.pacer.answered(status, getattr(response, 'headers', None))
                raw = response.read(MAX_BYTES + 1)
        except urllib.error.URLError as failed:
            # The origin's name does not exist in DNS (2026-11-16: feed.animetosho.org, AnimeTosho's DOCUMENTED feed host,
            # is NXDOMAIN on public resolvers): said as its own case, never as "could not be reached" in general.
            raise TransportError('unresolved' if isinstance(getattr(failed, 'reason', None), socket.gaierror) else 'network')
        except (OSError, ValueError):
            raise TransportError('network')
        if 300 <= status < 400:
            raise TransportError('redirect')
        if status != 200:
            raise TransportError('http_%d' % status)
        if len(raw) > MAX_BYTES:
            raise TransportError('too_large')
        return raw
