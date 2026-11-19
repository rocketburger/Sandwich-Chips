"""ChaptersDB.com chapter-candidate provider (a Cinema metadata plugin that "provides": "chapters").

The current ChaptersDB.com (not the defunct original ChapterDb/Plex archive). Its public read API v1 is documented
in the site's own API page (https://chaptersdb.com/api, a client-rendered page whose text was read from the public
client bundle, 2026-11-11) and was verified with anonymous GETs (research in ..\\outputs\\Chapter-provider-research-
2026-11-11). Documented as free, no authentication. No redistribution licence or rate policy was found: this
plugin reads per title on demand and never mirrors the database.

    GET https://chaptersdb.com/api/v1/show/:id       "Use ID, slug, TVDB ID, or IMDB ID". For movies, returns chapters
        {id, slug, title, type, year, tvdbId, imdbId, chapters: [set], chapterCount}
    GET https://chaptersdb.com/api/v1/episode/:id    "Use ID, slug, or TVDB ID"
        {id, slug, title, tvdbId, show: {id, slug, title, type}}
    GET https://chaptersdb.com/api/v1/chapters/:episodeId   chapter sets "sorted by rating"
        {episodeId, episodeTitle, chapters: [set], count}
    set: {id, entries: [{time: "HH:MM:SS.mmm", name}], note, upvotes, downvotes, uploaderName, createdAt}
    errors: {"error": "..."} with 400, 404 ("Show not found or not approved"), 500

Rules here (provider-specific): a movie is asked ONLY by its exact IMDb id, and the answer must echo it (type
"movie", imdbId equal). An episode is asked only by an id explicitly scoped to the episode ("episode.tvdb"): the
episode record must echo that TVDB id, and its chapters are then read by the record's own id (never a slug, a
title or a parsed SxxEyy label; a bare number is ambiguous on /show/). Times must parse strictly as
HH:MM:SS.mmm; a set with any entry that does not is handed on as unusable (the core drops it whole). Entries are
returned as the API lists them (sometimes out of order); the core sorts. uploaderName and createdAt are not
carried. A 404 is no match; any other failure raises (retried later by the registry).
"""
import re

BASE = 'https://chaptersdb.com/api/v1'
TIME = re.compile(r'([0-9]{2}):([0-5][0-9]):([0-5][0-9])\.([0-9]{3})')
IMDB = re.compile(r'tt[0-9]{7,9}')
TVDB = re.compile(r'[1-9][0-9]{0,9}')
REF = re.compile(r'[A-Za-z0-9._:-]{1,80}')
MAX_SETS, MAX_ENTRIES = 8, 128


def time_ms(text):
    """Strict HH:MM:SS.mmm to integer milliseconds, or None."""
    match = TIME.fullmatch(text) if isinstance(text, str) else None
    if not match:
        return None
    h, m, s, ms = (int(g) for g in match.groups())
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def parse_set(raw, title_ref):
    if not isinstance(raw, dict) or not (isinstance(raw.get('id'), str) and REF.fullmatch(raw['id'])):
        return None
    entries = raw.get('entries')
    chapters = None
    if isinstance(entries, list) and 0 < len(entries) <= MAX_ENTRIES:
        chapters = []
        for entry in entries:
            start = time_ms(entry.get('time')) if isinstance(entry, dict) else None
            name = entry.get('name') if isinstance(entry, dict) else None
            if start is None or not (name is None or isinstance(name, str)):
                chapters = None  # one bad entry: the whole set is unusable (never partially repaired)
                break
            chapters.append(dict(start_ms=start, title=name))
    up, down = raw.get('upvotes'), raw.get('downvotes')
    return dict(set_ref=raw['id'], title_ref=title_ref, note=raw.get('note') if isinstance(raw.get('note'), str) else None,
                votes=dict(up=up, down=down) if type(up) is int and type(down) is int else None, chapters=chapters)


def _get(fetch, path):
    try:
        return fetch(BASE + path)
    except Exception as exc:
        if getattr(exc, 'code', None) == 404:
            return None  # documented: not found (or not approved)
        raise


def _sets(listed, title_ref):
    if not isinstance(listed, list):
        raise ValueError('unexpected chapters')
    return [s for s in (parse_set(x, title_ref) for x in listed[:MAX_SETS]) if s]


def lookup(identity, settings, fetch):
    kind, ids = identity.get('media_type'), identity.get('ids') or {}
    if kind == 'movie':
        imdb = ids.get('imdb')
        if not (isinstance(imdb, str) and IMDB.fullmatch(imdb)):
            return None
        data = _get(fetch, '/show/' + imdb)
        if data is None:
            return None
        if not isinstance(data, dict) or data.get('type') != 'movie' or data.get('imdbId') != imdb:
            raise ValueError('not this movie')  # the exact id must be echoed: anything else is not this query
        ref = data.get('id') if isinstance(data.get('id'), str) and REF.fullmatch(data['id']) else None
        sets = _sets(data.get('chapters', []), ref)
        return dict(sets=sets) if sets else None
    if kind == 'episode':
        tvdb = ids.get('episode.tvdb')
        if not (isinstance(tvdb, str) and TVDB.fullmatch(tvdb)):
            return None
        episode = _get(fetch, '/episode/' + tvdb)
        if episode is None:
            return None
        if not isinstance(episode, dict) or episode.get('tvdbId') != tvdb or not (isinstance(episode.get('id'), str) and REF.fullmatch(episode['id'])):
            raise ValueError('not this episode')
        data = _get(fetch, '/chapters/' + episode['id'])
        if data is None:
            return None
        if not isinstance(data, dict) or data.get('episodeId') != episode['id']:
            raise ValueError('not this episode')
        sets = _sets(data.get('chapters', []), episode['id'])
        return dict(sets=sets) if sets else None
    return None
