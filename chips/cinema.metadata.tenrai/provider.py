"""Tenrai (MyAnimeList data): a Cinema metadata plugin (api 1, "provides": "details", "ids": ["mal"]).

What it does: for a title that STATES a MyAnimeList anime id, it asks Tenrai's public API (https://api.tenrai.org/v1,
beta; no account or key) for that exact entry and answers attributed facts, a cover and, through the optional
trailers(), YouTube trailer ids. Nothing else: no title search, no AniList or TMDB mapping, no user data, no writes.

    lookup(identity, settings, fetch)  -> facts | {"no_record": True, "cache_seconds": N} | None
    trailers(query, settings, fetch)   -> {"trailers": [...], "cache_seconds": N} | {"no_record": ...} | None

Rules (research/TENRAI-ENRICHMENT-PLAN-2026-11-15.md, revision 6a9a6ad):
- Exact identity: ids.mal only, a positive integer; the answer is used only when data.mal_id equals it. A MyAnimeList
  entry is ONE installment (a season, film or special), so the facts describe that entry, never a whole series, and
  never an episode. Episode counts, relations and numbering stay the video provider's.
- Requests: GET /anime/{id}/full; GET /anime/{id}/videos only for trailers, when /full lists no usable trailer.
  Fixed origin: every request asks the core's fetch for same_origin=True; a core without it gets no request at all.
- Budget, one per Cinema session (this module is loaded once): at most 3 requests a second and 90 a minute (Tenrai
  publishes 4 and 120 per IP); a request waits at most 1 s for its turn. A 429 sets a backoff from Retry-After
  (1-3600 s, else 60) that every later call honours WITHOUT a request; failures carry retry_after for the registry.
- Cache: normalized answers, keyed by (plugin, version, this module load, schema, endpoint, id), 256 entries and
  4 MiB at most; fresh for 24 h from retrieval, a "no record" (404 or a different id) for 6 h. A 200 that is not the
  documented envelope (JSON null, no data object, no integer mal_id) is a transient failure, never cached. Every answer carries
  cache_seconds = what is left of that lifetime, so the registry's copy expires at the same moment.
- Attribution and provenance: the registry names the plugin as the source; each answer carries retrieved_at (the
  UTC date Cinema retrieved it: not a claim about how current MyAnimeList's data is) and the score's own source.
  Tenrai's terms appreciate attribution; it is kept regardless. Artwork and text remain their owners'.
- Pictures: only https://cdn.myanimelist.net/ covers are passed on (the core caches them; the page gets a handle).
- Trailers: an 11-character YouTube id only (Cinema builds its own official embed; the API's URLs are ignored).
  embeddable false: excluded; null: unknown, offered. A region restriction is judged only against the configured
  region (setting "region"); without one, or when the restriction cannot be read, availability is unknown and the
  official player decides. Cinema never geolocates.
"""
import inspect
import json
import re
import secrets
import threading
import time
from collections import OrderedDict, deque

BASE = 'https://api.tenrai.org/v1'
PLUGIN = 'cinema.metadata.tenrai'
VERSION = '0.1.2'
SCHEMA = 1
INSTANCE = secrets.token_hex(6)        # this module load: a cache entry never crosses code versions or loads
SOURCE = 'MyAnimeList'                 # the score's own source (Tenrai serves MyAnimeList's figures)
LIFETIME = 24 * 3600
NO_RECORD = 6 * 3600
PER_SECOND, PER_MINUTE, WAIT = 3, 90, 1.0
MAX_ENTRIES, MAX_BYTES = 256, 4 * 1024 * 1024
MAL = re.compile(r'[1-9][0-9]{0,9}')
YOUTUBE = re.compile(r'[A-Za-z0-9_-]{11}')
REGION = re.compile(r'[A-Z]{2}')
ART = re.compile(r'https://cdn\.myanimelist\.net/[A-Za-z0-9/_.%-]{1,300}')
BOILERPLATE = re.compile(r'\s*\[(Written by MAL Rewrite|Source:[^\]]{0,80})\]\s*$', re.I)

# The clocks it uses (a test may model them); the plugin never sleeps longer than WAIT.
clock, wall, sleep = time.monotonic, time.time, time.sleep


class RateLimited(Exception):
    def __init__(self, retry_after):
        Exception.__init__(self, 'rate limited')
        self.retry_after = max(1, min(3600, int(retry_after)))


class Unsupported(Exception):
    """The core's fetch cannot pin the origin (no same_origin): nothing is requested."""


class ProviderError(Exception):
    pass


class Malformed(ProviderError):
    """A 200 whose JSON is not the documented envelope (null, a list, no data object, no integer mal_id). It is a
    transient failure (the registry keeps it briefly and asks again), NEVER a "no record": nothing says the entry is
    absent."""


_NONE = object()   # a cached "no record"
_ABSENT = object()   # a 404: Tenrai states there is no such entry
_lock = threading.Lock()
_cache = OrderedDict()          # key -> (value | _NONE, retrieved monotonic, retrieved date, size)
_cache_bytes = 0
_second, _minute = deque(), deque()
_backoff_until = 0.0


# Requests ---------------------------------------------------------------------------------------------------------
def _pinnable(fetch):
    try:
        params = inspect.signature(fetch).parameters
    except (TypeError, ValueError):
        return False
    return 'same_origin' in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def _admit():
    """Take one request from the budget (waiting at most WAIT), or raise RateLimited."""
    global _backoff_until
    deadline = clock() + WAIT
    while True:
        with _lock:
            now = clock()
            if now < _backoff_until:
                raise RateLimited(_backoff_until - now + 0.999)
            while _second and now - _second[0] >= 1.0:
                _second.popleft()
            while _minute and now - _minute[0] >= 60.0:
                _minute.popleft()
            if len(_second) < PER_SECOND and len(_minute) < PER_MINUTE:
                _second.append(now); _minute.append(now)
                return
            free = max(_second[0] + 1.0 if len(_second) >= PER_SECOND else now,
                       _minute[0] + 60.0 if len(_minute) >= PER_MINUTE else now)
        if free > deadline:
            raise RateLimited(free - now + 0.999)
        sleep(max(0.0, free - now))


def _get(fetch, path):
    """The JSON body of one GET to the fixed origin, or _ABSENT for 404; raises on anything transient (a JSON null
    included: it is not a statement of absence)."""
    global _backoff_until
    if not _pinnable(fetch):
        raise Unsupported('this Cinema cannot pin the request origin')
    _admit()
    try:
        body = fetch(BASE + path, None, {'Accept': 'application/json'}, same_origin=True)
    except Exception as exc:
        code = getattr(exc, 'code', None)
        if code == 404:
            return _ABSENT
        if code == 429:
            retry = 60
            try:
                retry = max(1, min(3600, int((getattr(exc, 'headers', None) or {}).get('Retry-After', 60))))
            except (TypeError, ValueError):
                pass
            with _lock:
                _backoff_until = max(_backoff_until, clock() + retry)
            raise RateLimited(retry)
        if code:
            raise ProviderError('Tenrai answered %s' % code)   # the status only: never the URL or the body
        raise
    if body is None:
        raise Malformed('Tenrai answered an empty document')
    return body


# Cache ------------------------------------------------------------------------------------------------------------
def _key(endpoint, mal):
    return (PLUGIN, VERSION, INSTANCE, SCHEMA, endpoint, mal)


def _held(endpoint, mal):
    """(value, retrieved date, seconds left) of a fresh entry, else None."""
    with _lock:
        entry = _cache.get(_key(endpoint, mal))
        if entry is None:
            return None
        value, at, date, _ = entry
        left = int((NO_RECORD if value is _NONE else LIFETIME) - (clock() - at))
        if left <= 0:
            _drop(_key(endpoint, mal))
            return None
        _cache.move_to_end(_key(endpoint, mal))
        return value, date, left


def _drop(key):
    global _cache_bytes
    entry = _cache.pop(key, None)
    if entry is not None:
        _cache_bytes -= entry[3]


def _keep(endpoint, mal, value):
    global _cache_bytes
    size = 64 if value is _NONE else len(json.dumps(value))
    date = time.strftime('%Y-%m-%d', time.gmtime(wall()))
    with _lock:
        _drop(_key(endpoint, mal))
        _cache[_key(endpoint, mal)] = (value, clock(), date, size)
        _cache_bytes += size
        while _cache and (len(_cache) > MAX_ENTRIES or _cache_bytes > MAX_BYTES):
            _drop(next(iter(_cache)))
    return value, date, NO_RECORD if value is _NONE else LIFETIME


def _entry(fetch, endpoint, mal, normalize):
    held = _held(endpoint, mal)
    if held is not None:
        return held
    body = _get(fetch, '/anime/%s/%s' % (mal, endpoint))
    # normalize raises Malformed for a body that is not the envelope (nothing is cached); None means the answer is
    # explicitly another entry (a different mal_id): a stated "no record", like a 404.
    value = normalize(body, mal) if body is not _ABSENT else None
    return _keep(endpoint, mal, _NONE if value is None else value)


# Normalizing (the documented and observed shape: research/tenrai-2026-11-15/live-check-observed.json) ---------------
def _text(value, limit):
    if not isinstance(value, str):
        return None
    value = ' '.join(value.split())
    return value[:limit] or None


def _names(rows, limit):
    out = []
    for row in rows if isinstance(rows, list) else []:
        name = _text(row.get('name'), 60) if isinstance(row, dict) else None
        if name and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def _date(prop):
    """YYYY-MM-DD only when the year, month and day are all stated; a partial date stays unknown."""
    if not isinstance(prop, dict):
        return None
    y, m, d = prop.get('year'), prop.get('month'), prop.get('day')
    if all(type(v) is int for v in (y, m, d)) and 1900 < y < 2200 and 1 <= m <= 12 and 1 <= d <= 31:
        return '%04d-%02d-%02d' % (y, m, d)
    return None


def _candidate(raw, title, category):
    """One trailer candidate from the API's trailer object: {video, title, category, embeddable, restriction}."""
    if not isinstance(raw, dict) or not isinstance(raw.get('youtube_id'), str) or not YOUTUBE.fullmatch(raw['youtube_id']):
        return None
    embeddable = raw.get('embeddable') if isinstance(raw.get('embeddable'), bool) else None
    return dict(video=raw['youtube_id'], title=_text(title, 120), category=category, embeddable=embeddable,
                restriction=raw.get('region_restriction') if isinstance(raw.get('region_restriction'), dict) else
                ('unknown' if raw.get('region_restriction') is not None else None))


def full(body, mal):
    """The normalized /full answer; None when it is explicitly ANOTHER entry (an integer mal_id that differs); raises
    Malformed when the body is not the envelope at all."""
    data = body.get('data') if isinstance(body, dict) else None
    if not isinstance(data, dict) or type(data.get('mal_id')) is not int:
        raise Malformed('Tenrai answered an unexpected document')
    if data['mal_id'] != int(mal):
        return None
    info = {}
    synopsis = data.get('synopsis')
    if isinstance(synopsis, str) and synopsis.strip():
        info['overview'] = BOILERPLATE.sub('', synopsis.strip())[:2000].strip() or None
    names = []
    for value in [data.get('title_english'), data.get('title'), data.get('title_japanese')] + \
            (data.get('title_synonyms') if isinstance(data.get('title_synonyms'), list) else []):
        text = _text(value, 120)
        if text and text not in names:
            names.append(text)
    if names:
        info['aliases'] = names[:8]
    images = data.get('images') if isinstance(data.get('images'), dict) else {}
    for kind in ('webp', 'jpg'):
        url = (images.get(kind) or {}).get('large_image_url') if isinstance(images.get(kind), dict) else None
        if isinstance(url, str) and ART.fullmatch(url):
            info['poster'] = url
            break
    genres = _names(data.get('genres'), 8)
    genres += [n for n in _names(data.get('themes'), 8) if n not in genres]
    if genres:
        info['genres'] = genres[:8]
    studios = _names(data.get('studios'), 3)
    if studios:
        info['studio'] = studios
    for key, limit in (('rating', 160), ('status', 40)):
        text = _text(data.get(key), limit)
        if text:
            info['rated' if key == 'rating' else 'status'] = text
    premiered = _date(((data.get('aired') or {}).get('prop') or {}).get('from') if isinstance(data.get('aired'), dict) else None)
    if premiered:
        info['premiered'] = premiered
    score = data.get('score')
    if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 < score <= 10:
        info['score'] = dict(value=float(score), source=SOURCE)
        if type(data.get('scored_by')) is int and data['scored_by'] > 0:
            info['score']['votes'] = data['scored_by']
    info = {k: v for k, v in info.items() if v}
    trailer = _candidate(data.get('trailer'), 'Trailer', 'Trailer')
    return dict(info=info, trailers=[trailer] if trailer else [])


def videos(body, mal):
    """The normalized /videos answer: promotional videos' trailer candidates (episodes and music videos ignored)."""
    data = body.get('data') if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise Malformed('Tenrai answered an unexpected document')
    out = []
    for promo in (data.get('promo') if isinstance(data.get('promo'), list) else [])[:16]:
        if isinstance(promo, dict):
            found = _candidate(promo.get('trailer'), promo.get('title'), 'Promo')
            if found and all(found['video'] != c['video'] for c in out):
                out.append(found)
    return dict(trailers=out)


# The plugin interface ---------------------------------------------------------------------------------------------
def _mal(identity):
    # The installment kinds (2026-11-16): a film, a series' season or cour, and a SPECIAL/OVA entry ("special": its own
    # installment with its own MyAnimeList id, never an episode of another one). An episode is never answered: a
    # MyAnimeList entry describes its installment, not an episode.
    if not isinstance(identity, dict) or identity.get('media_type') not in ('movie', 'tvshow', 'special'):
        return None
    value = (identity.get('ids') or {}).get('mal')
    value = str(value) if type(value) is int else value
    return value if isinstance(value, str) and MAL.fullmatch(value) else None


def lookup(identity, settings, fetch):
    mal = _mal(identity)
    if mal is None:
        return None
    value, date, left = _entry(fetch, 'full', mal, full)
    if value is _NONE or not value['info']:
        return dict(no_record=True, cache_seconds=left)
    return dict(value['info'], retrieved_at=date, cache_seconds=left)


def usable(candidate, region):
    """True when a candidate may be offered: never when not embeddable; a region restriction only with a region."""
    if candidate['embeddable'] is False:
        return False
    restriction = candidate['restriction']
    if not region or not isinstance(restriction, dict):
        return True   # no configured region, or nothing readable: unknown, the official player decides
    allowed, blocked = restriction.get('allowed'), restriction.get('blocked')
    if isinstance(allowed, list) and all(isinstance(c, str) for c in allowed):
        return region in allowed
    if isinstance(blocked, list) and all(isinstance(c, str) for c in blocked):
        return region not in blocked
    return True


def trailers(query, settings, fetch):
    if (settings or {}).get('trailers') == 'off':
        return None
    mal = _mal(query)
    if mal is None:
        return None
    region = (settings or {}).get('region') or ''
    region = region.strip().upper() if isinstance(region, str) and REGION.fullmatch(region.strip().upper()) else ''
    value, date, left = _entry(fetch, 'full', mal, full)
    if value is _NONE:
        return dict(no_record=True, cache_seconds=left)
    listed = [c for c in value['trailers'] if usable(c, region)]
    if not listed:   # only now, on demand: the entry's promotional videos
        more, _, more_left = _entry(fetch, 'videos', mal, videos)
        left = min(left, more_left)
        if more is not _NONE:
            listed = [c for c in more['trailers'] if usable(c, region)]
    if not listed:
        return dict(no_record=True, cache_seconds=left)
    return dict(trailers=[dict(transport='youtube', video=c['video'], title=c['title'], categories=[c['category']])
                          for c in listed[:8]], retrieved_at=date, cache_seconds=left)
