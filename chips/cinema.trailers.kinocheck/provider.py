"""KinoCheck trailer provider (a Cinema metadata plugin that "provides": "trailers").

API (https://api.kinocheck.com/, read 2026-11-12):
    GET https://api.kinocheck.com/movies?tmdb_id=<id> | imdb_id=<tt...>  [&language=en|de]
    GET https://api.kinocheck.com/shows?tmdb_id=<id>  | imdb_id=<tt...>  [&language=en|de]
        Exactly one id per request. /shows has no season or episode filter: a show's trailer is the show's.
    200: {id, tmdb_id, imdb_id, title, language, trailer: video | null, videos: [video], ...}
         video: {youtube_video_id, title, language, categories: [..], thumbnail, url, ...}
         An unknown id answers {error, message, ...} (no match).
    The documented video resource names a YouTube id; it documents no direct MP4/HLS URL. Its web `url` is a
    KinoCheck page, not media: never returned, followed or scraped.
    Free client limit documented as 1000 requests per day; an optional X-Api-Key (and X-Api-Host) raises it.

Returned: {trailers: [{transport: "youtube", video, title, language, categories}]}, KinoCheck's own selected
trailer first (when it is in the requested language), then other Trailer-category videos in that language.
None when there is none. Raises on transient trouble (rate limit, network, server error): Cinema's registry
remembers a failure briefly and retries later. TLS is verified (Cinema's fetch); the key is a header, never in a
URL; errors carry no key.
"""
import re
import threading
import time

BASE = 'https://api.kinocheck.com'
LANGUAGES = ('en', 'de')
VIDEO = re.compile(r'[A-Za-z0-9_-]{11}')
IMDB = re.compile(r'tt[0-9]{5,10}')
TMDB = re.compile(r'[1-9][0-9]{0,9}')
MAX_TRAILERS = 4
_backoff_until = 0.0
_backoff_lock = threading.Lock()


class RateLimited(Exception):
    pass


class ProviderError(Exception):
    pass


def request(identity):
    """(path, params) for an identity, or None when it cannot be asked exactly (no title guesses, no episodes)."""
    kind = identity.get('media_type')
    path = {'movie': '/movies', 'tvshow': '/shows'}.get(kind)
    if path is None:
        return None
    ids = {str(k).lower(): str(v) for k, v in (identity.get('ids') or {}).items()}
    if TMDB.fullmatch(ids.get('tmdb', '')):
        return path, {'tmdb_id': ids['tmdb']}
    if IMDB.fullmatch(ids.get('imdb', '')):
        return path, {'imdb_id': ids['imdb']}
    return None


def _video(raw, language):
    if not isinstance(raw, dict) or not isinstance(raw.get('youtube_video_id'), str) or not VIDEO.fullmatch(raw['youtube_video_id']):
        return None
    lang = raw.get('language') if isinstance(raw.get('language'), str) else None
    if lang and lang.lower()[:2] != language:
        return None
    categories = [c for c in raw.get('categories') or [] if isinstance(c, str)][:4] if isinstance(raw.get('categories'), list) else []
    title = raw.get('title') if isinstance(raw.get('title'), str) else None
    return dict(transport='youtube', video=raw['youtube_video_id'], title=(title or '').strip()[:160] or None,
                language=(lang or language).lower()[:2], categories=categories)


def trailers(body, language):
    """The trailer list for one answer: the selected trailer, then Trailer-category videos, in that language."""
    if not isinstance(body, dict) or body.get('error'):
        return []
    out, seen = [], set()
    first = _video(body.get('trailer'), language)
    videos = body.get('videos') if isinstance(body.get('videos'), list) else []
    candidates = ([first] if first else []) + [v for v in (_video(x, language) for x in videos) if v and 'Trailer' in v['categories']]
    for video in candidates:
        if video['video'] not in seen:
            seen.add(video['video'])
            out.append(video)
        if len(out) >= MAX_TRAILERS:
            break
    return out


def lookup(identity, settings, fetch):
    global _backoff_until
    asked = request(identity)
    if asked is None:
        return None
    path, params = asked
    language = (settings.get('language') or 'en').strip().lower()
    language = language if language in LANGUAGES else 'en'
    params = dict(params, language=language)
    headers = {}
    key = (settings.get('api_key') or '').strip()
    if key:
        headers['X-Api-Key'] = key
        host = (settings.get('api_host') or '').strip()
        if host and re.fullmatch(r'[A-Za-z0-9.-]{1,120}', host):
            headers['X-Api-Host'] = host
    with _backoff_lock:
        if time.monotonic() < _backoff_until:
            raise RateLimited()
    try:
        body = fetch(BASE + path, params, headers)
    except Exception as exc:
        code = getattr(exc, 'code', None)
        if code == 404:
            return None
        if code == 429:
            retry = 60
            try:
                retry = max(1, min(3600, int((getattr(exc, 'headers', None) or {}).get('Retry-After', 60))))
            except (TypeError, ValueError):
                pass
            with _backoff_lock:
                _backoff_until = time.monotonic() + retry
            raise RateLimited()
        if code:
            raise ProviderError('KinoCheck answered %s' % code)  # status only, never the URL or key
        raise
    found = trailers(body, language)
    return dict(trailers=found) if found else None
