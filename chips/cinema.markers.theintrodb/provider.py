"""TheIntroDB marker provider (a Cinema metadata plugin that "provides": "markers").

API v4, read from its public OpenAPI document (https://theintrodb.org/openapi.yaml, 4.0.0, read 2026-11-11)
and its package docs (theintrodb.github.io/theintrodb-npm: functions, migration v3 to v4):
    GET https://api.theintrodb.org/v4/media
        one id: tvdb_id (integer, preferred) | tmdb_id (integer) | imdb_id (^tt[0-9]{7,8}$)
        TV: season (0..9999) AND episode (0..10000), in TVDB's "default" order unless "mapping" names another
            TVDB order (default, official, dvd, alternate, absolute, regional; "aired" is TVDB's "official").
            No conversion from TMDB's own episode order is documented, so only "tvdb-default" is placed.
        details=true adds per-segment confidence (0..1) and submission_count, and group_duration_ms (the
            median length of the release group the timestamps were measured on).
        duration_ms is not sent (a later decision); title hints (episode_name) are never sent.
    200: {tvdb_id, type, season?, episode?, group_duration_ms?, intro?, recap?, credits?, preview?}; each
        segment list may hold several entries (all are kept). start_ms/end_ms nullable integers (ms):
        intro/recap start_ms null = at or near the beginning; credits/preview end_ms null = to the end of the
        media; an end_ms of 0 = no segment. A type with no submissions is omitted.
    404 {error, code: no_submissions | episode_not_found | media_not_found}: nothing to return (no match).
    400 / 422 / 429 / 5xx: failures (raised: the registry remembers them briefly and the lookup is retried).
Bearer key optional (only adds the caller's own pending submissions). Rate limits (documented): 30 requests
per 10 s and 5,000 per day anonymous.
"""
import re

BASE = 'https://api.theintrodb.org/v4/media'
KINDS = ('intro', 'recap', 'credits', 'preview')
NULL_START_OK = ('intro', 'recap')      # null start_ms: at or near the beginning
NULL_END_OK = ('credits', 'preview')    # null end_ms: to the end of the media
PLACEABLE_NUMBERING = {'tvdb-default': 'default'}
MAX_PER_KIND = 4
IMDB = re.compile(r'tt[0-9]{7,8}')


def _int_id(value, high):
    return int(value) if isinstance(value, str) and value.isdigit() and 1 <= int(value) <= high else None


def _id_param(ids, prefix=''):
    """The one id to address the media with, in the API's own priority order (tvdb, tmdb, imdb)."""
    tvdb = _int_id(ids.get(prefix + 'tvdb'), 100000000)
    if tvdb:
        return {'tvdb_id': tvdb}
    tmdb = _int_id(ids.get(prefix + 'tmdb'), 10000000)
    if tmdb:
        return {'tmdb_id': tmdb}
    imdb = ids.get(prefix + 'imdb')
    if isinstance(imdb, str) and IMDB.fullmatch(imdb):
        return {'imdb_id': imdb}
    return None


def request(identity):
    """(params) for an identity, or None when it cannot be asked exactly."""
    ids = {str(k).lower(): str(v) for k, v in (identity.get('ids') or {}).items()}
    kind = identity.get('media_type')
    if kind == 'movie':
        params = _id_param(ids)
    elif kind == 'episode':
        mapping = PLACEABLE_NUMBERING.get(identity.get('numbering'))
        season, number = identity.get('season'), identity.get('episode')
        if mapping is None or type(season) is not int or type(number) is not int:
            return None
        params = _id_param(ids, 'tvshow.')
        if params is not None:
            params.update(season=season, episode=number, mapping=mapping)
    else:
        return None
    if params is None:
        return None
    params['details'] = 'true'
    return params


def _segment(kind, raw):
    if not isinstance(raw, dict):
        return None
    start, end = raw.get('start_ms'), raw.get('end_ms')
    if start is not None and not (type(start) is int and start >= 0):
        return None
    if end is not None and not (type(end) is int and end >= 0):
        return None
    if end == 0:
        return None  # documented: an end of 0 means no segment
    if start is None and kind not in NULL_START_OK:
        return None
    if end is None and kind not in NULL_END_OK:
        return None
    if start is not None and end is not None and end <= start:
        return None
    confidence = raw.get('confidence')
    count = raw.get('submission_count')
    return dict(kind=kind, start_ms=start, end_ms=end,
                provider_confidence=float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and 0 <= confidence <= 1 else None,
                submissions=count if type(count) is int and count >= 0 else None)


def parse(data, identity):
    """{measured_ms, markers} from a 200 body, or None when it holds no usable segment."""
    if not isinstance(data, dict) or data.get('type') not in ('movie', 'tv'):
        raise ValueError('unexpected response')
    if (identity.get('media_type') == 'movie') != (data['type'] == 'movie'):
        raise ValueError('unexpected media type')
    if identity.get('media_type') == 'episode' and (data.get('season') != identity.get('season') or data.get('episode') != identity.get('episode')):
        raise ValueError('coordinates not echoed')  # the API echoes the caller's coordinates; anything else is not this query
    markers = []
    for kind in KINDS:
        entries = data.get(kind)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ValueError('unexpected segment list')
        markers.extend(m for m in (_segment(kind, e) for e in entries[:MAX_PER_KIND]) if m)
    if not markers:
        return None
    measured = data.get('group_duration_ms')
    return dict(measured_ms=measured if type(measured) is int and measured > 0 else None, markers=markers)


def lookup(identity, settings, fetch):
    params = request(identity)
    if params is None:
        return None
    key = (settings.get('api_key') or '').strip()
    headers = {'Authorization': 'Bearer ' + key} if key else None
    try:
        data = fetch(BASE, params=params, headers=headers)
    except Exception as exc:
        if getattr(exc, 'code', None) == 404:
            return None  # documented "nothing to return" (no submissions, episode or media not found)
        raise
    return parse(data, identity)
