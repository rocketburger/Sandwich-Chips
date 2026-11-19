"""AniBridge mappings as a Cinema identity plugin (provides "identity"; cinema_identity). See NOTICE.md.

Dataset (v3, mappings.schema.json): {"$meta": {...}, "$schema": ..., "$includes": [...], "<source>": {"<target>":
{"<source range>": "<target range>" | null}}}, descriptors "provider:id[:scope]". Mappings are DIRECTIONAL: only the
entries whose SOURCE is anilist:<id> or mal:<id> are read (never a reverse edge). "$includes" is never followed.

For one AniList/MAL entry this answers, typed (display grouping only):
- work: the one TMDB / TVDB / IMDb SHOW it maps to, only when every show target of that provider names the same series
  and a regular season (s1+, never s0 specials) - "an explicit unambiguous normal-series mapping";
- installment: that show's season ("<id>:s<n>"), only when the entry maps to exactly one season of it;
- item: the one TMDB / IMDb MOVIE it maps to, only when exactly one.
Ranges are read only to skip explicit unmappings (null); episode correspondences are never answered."""
import json
import os
import re

DESCRIPTOR = re.compile(r'^(\w+):(\w+)(?::(\w+))?$')
SOURCES = ('anilist', 'mal')
SHOWS = {'tmdb_show': 'tmdb.tv', 'tvdb_show': 'tvdb', 'imdb_show': 'imdb'}
SEASONS = {'tmdb_show': 'tmdb.tv.season', 'tvdb_show': 'tvdb.season'}
MOVIES = {'tmdb_movie': 'tmdb.movie', 'imdb_movie': 'imdb'}
INDEX_VERSION = 1
_held = {'digest': None, 'index': None}


class NoData(Exception):
    """The dataset is not here yet (first use, offline): declined. Cinema keys identity answers on the dataset generation,
    so the first answer after the data arrives is a fresh lookup (this short memory only spares the plugin meanwhile)."""
    retry_after = 5


def _prune(pairs):
    """json object hook: a range map ({"1-12": "1-12"}) is reduced to whether anything stays mapped (True) or every range
    is explicitly unmapped (False), so the parse keeps no episode data (memory bound)."""
    if pairs and all(k[:1].isdigit() for k, _ in pairs):
        return any(v is not None for _, v in pairs)
    return dict(pairs)


def build_index(raw):
    """{"anilist:<id>" | "mal:<id>": [target descriptors still mapped]} from a parsed (pruned) dataset."""
    index = {}
    for source, targets in raw.items():
        if source.startswith('$') or not isinstance(targets, dict):
            continue
        m = DESCRIPTOR.match(source)
        if not m or m.group(1) not in SOURCES or m.group(3):
            continue
        kept = [t for t, mapped in targets.items() if mapped is True and DESCRIPTOR.match(t)
                and DESCRIPTOR.match(t).group(1) in set(SHOWS) | set(MOVIES)]
        if kept:
            index[source] = sorted(kept)[:32]
    return index


MIN_SOURCES = 1000      # the 2026-11-16 v3 build indexes 21,936 anilist/mal sources; far fewer means a broken payload


def _load(path):
    with open(path, 'r', encoding='utf-8') as stream:
        raw = json.load(stream, object_pairs_hook=_prune)
    meta = raw.get('$meta') if isinstance(raw, dict) else None
    version = meta.get('schema_version') if isinstance(meta, dict) else None
    if not (isinstance(version, str) and version.split('.')[0] == '3'):
        raise ValueError('not an AniBridge v3 dataset')
    return build_index(raw)


def _save(cache_dir, digest, index):
    os.makedirs(cache_dir, exist_ok=True)
    cached = os.path.join(cache_dir, 'index-%s.json' % digest[:16])
    tmp = cached + '.part'
    with open(tmp, 'w', encoding='utf-8') as stream:
        json.dump(dict(version=INDEX_VERSION, index=index), stream)
    os.replace(tmp, cached)


def validate_dataset(path, digest, cache_dir):
    """Cinema's publication check (cinema_identity.Dataset): the WHOLE payload parses, states schema v3, and indexes
    enough sources; its index is built now (so the first search after a refresh does not build it). False: not published."""
    try:
        index = _load(path)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if len(index) < MIN_SOURCES:
        return False
    _save(cache_dir, digest, index)
    return True


def _index(fetch, path, meta):
    digest = meta.get('digest')
    if _held['digest'] == digest and _held['index'] is not None:
        return _held['index']
    cached = os.path.join(fetch.cache_dir, 'index-%s.json' % digest[:16]) if fetch.cache_dir and digest else None
    index = None
    if cached and os.path.isfile(cached):
        try:
            with open(cached, 'r', encoding='utf-8') as stream:
                held = json.load(stream)
            if held.get('version') == INDEX_VERSION:
                index = held['index']
        except (OSError, ValueError, KeyError):
            index = None
    if index is None:            # a dataset published before indexes existed, or a lost index file
        index = _load(path)
        if fetch.cache_dir and digest:
            _save(fetch.cache_dir, digest, index)
    _held.update(digest=digest, index=index)
    return index


def relations(targets):
    """Typed relations for one source entry's mapped targets (see the module text)."""
    shows, movies = {}, {}
    for target in targets:
        provider, ident, scope = DESCRIPTOR.match(target).groups()
        if provider in SHOWS:
            shows.setdefault(provider, set()).add((ident.lower(), scope or ''))
        elif provider in MOVIES and not scope:
            movies.setdefault(provider, set()).add(ident.lower())
    out, work, seasons = [], {}, {}
    for provider, found in shows.items():
        series = {i for i, _ in found}
        scopes = {s for _, s in found}
        regular = all(re.fullmatch(r's([1-9]\d*)', s or '') for s in scopes) if provider != 'imdb_show' else True
        if len(series) == 1 and regular:
            (ident,) = series
            work[SHOWS[provider]] = ident
            if provider in SEASONS and len(scopes) == 1:
                seasons[SEASONS[provider]] = '%s:%s' % (ident, next(iter(scopes)))
    if work:
        out.append(dict(level='work', ids=work))
    if seasons:
        out.append(dict(level='installment', ids=seasons))
    item = {MOVIES[p]: next(iter(ids)) for p, ids in movies.items() if len(ids) == 1}
    if item:
        out.append(dict(level='item', ids=item))
    return out


def lookup(identity, settings, fetch):
    ids = identity.get('ids') or {}
    keys = ['%s:%s' % (ns, ids[ns]) for ns in SOURCES if isinstance(ids.get(ns), str) and ids[ns].isdigit()]
    if not keys:
        return None
    path, meta = fetch.dataset()
    if path is None:
        raise NoData()
    index = _index(fetch, path, meta)
    for key in keys:      # AniList first: the anime provider's own id
        found = relations(index.get(key) or [])
        if found:
            return dict(relations=found, revision='%s (digest %s)' % (meta.get('generation'), (meta.get('digest') or '')[:12]), cache_seconds=86400)
    return {'no_record': True, 'cache_seconds': 86400}
