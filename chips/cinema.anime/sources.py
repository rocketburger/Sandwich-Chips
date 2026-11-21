"""Torrent source readers for the Anime extension: Nyaa (RSS search feed) and AnimeTosho (JSON search feed).

Formats as the services publish them (Nyaa: RSS 2.0 items with the nyaa: namespace, infoHash/seeders/size; AnimeTosho:
https://feed.animetosho.org/json, info_hash/seeders/total_size). Not verified live here: offline tests substitute the
transport. Otaku 5.3.4 was read only for which searches matter (an episode search, and a batch search for finished
shows); no Otaku code is copied. Each reader returns plain release records; release.py reads them and the provider
filters them for the chosen episode."""
import json
import re
import xml.etree.ElementTree as ET

# Siblings (release, transport) are given to this file by backend.load(): the host loads module files by path, not
# as a package, so they are not imported by name.

HASH = re.compile(r'[0-9a-f]{40}')
NYAA_NS = '{https://nyaa.si/xmlns/nyaa}'
_SIZE = re.compile(r'^\s*([\d.]+)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)\s*$', re.I)
_UNITS = {'b': 1, 'kib': 1024, 'mib': 1024 ** 2, 'gib': 1024 ** 3, 'tib': 1024 ** 4, 'kb': 1000, 'mb': 1000 ** 2, 'gb': 1000 ** 3, 'tb': 1000 ** 4}
MAX_ITEMS = 100


class SourceError(Exception):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


def _size(text):
    match = _SIZE.match(text or '')
    if not match:
        return None
    return int(float(match.group(1)) * _UNITS[match.group(2).lower()])


def _record(title, info_hash, seeders, size, origin):
    if not isinstance(title, str) or not title.strip() or not isinstance(info_hash, str):
        return None
    info_hash = info_hash.strip().lower()
    if not HASH.fullmatch(info_hash):
        return None
    return dict(title=' '.join(title.split())[:300], hash=info_hash, seeders=seeders if type(seeders) is int and seeders >= 0 else None,
                size=size if type(size) is int and size >= 0 else None, origin=origin)


def episode_queries(alias, episode, season, finished, last):
    """The searches for one alias: the episode itself, and (finished shows only) packs."""
    e = '%02d' % episode
    s = '%02d' % (season or 1)
    queries = ['%s "- %s"|"S%sE%s"|"E%s"' % (alias, e, s, e, e)]
    if finished:
        queries.append('%s "Batch"|"Complete"|"01-%02d"|"01 ~ %02d"' % (alias, last or episode, last or episode))
    return queries


class Nyaa(object):
    origin = 'nyaa'
    label = 'Nyaa'

    def __init__(self, transport):
        self.transport = transport

    def search(self, query, timeout):
        try:
            raw = self.transport.fetch('nyaa', params=dict(page='rss', q=query, c='1_0', f='0'), timeout=timeout)
        except transport.TransportError as failed:
            raise SourceError('rate_limited' if failed.code == 'http_429' else 'unresolved' if failed.code == 'unresolved' else 'unreachable')
        if b'<!DOCTYPE' in raw[:2048].upper() or b'<!ENTITY' in raw:
            raise SourceError('bad_response')
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise SourceError('bad_response')
        out = []
        for item in root.iter('item'):
            if len(out) >= MAX_ITEMS:
                break
            seeders = (item.findtext(NYAA_NS + 'seeders') or '').strip()
            record = _record(item.findtext('title'), item.findtext(NYAA_NS + 'infoHash'), int(seeders) if seeders.isdigit() else None,
                             _size(item.findtext(NYAA_NS + 'size')), self.origin)
            if record:
                out.append(record)
        return out


class AnimeTosho(object):
    """AnimeTosho's documented JSON feed. Diagnosed 2026-11-17 (public reads): feed.animetosho.org does not exist in DNS,
    and the same feed on the site's own host (animetosho.org/feed/json) answers HTTP 403 "Feed disabled"; the site's
    front page carries the operator's notice (2026-09-16) that the feed/storage server is shut down and all feeds and API
    requests stop working. Both refusing is reported as "retired" (the provider holds it off for hours, says why, and
    tries again later); one failing alone is an ordinary outage."""
    origin = 'animetosho'
    label = 'AnimeTosho'

    def __init__(self, transport):
        self.transport = transport

    def search(self, query, timeout):
        try:
            try:
                raw = self.transport.fetch('animetosho', params=dict(q=query), timeout=timeout)
            except transport.TransportError as failed:
                if failed.code != 'unresolved':
                    raise
                try:
                    raw = self.transport.fetch('animetosho_site', params=dict(q=query), timeout=timeout)
                except transport.TransportError as second:
                    raise SourceError('retired' if second.code in ('http_403', 'http_410', 'http_404') else 'unresolved')
            data = json.loads(raw.decode('utf-8'))
        except transport.TransportError as failed:
            raise SourceError('rate_limited' if failed.code == 'http_429' else 'unresolved' if failed.code == 'unresolved' else 'unreachable')
        except (UnicodeDecodeError, ValueError):
            raise SourceError('bad_response')
        if not isinstance(data, list):
            raise SourceError('bad_response')
        out = []
        for entry in data[:MAX_ITEMS]:
            if isinstance(entry, dict):
                record = _record(entry.get('title'), entry.get('info_hash'), entry.get('seeders'), entry.get('total_size'), self.origin)
                if record:
                    out.append(record)
        return out


def _json(origin, net, timeout, path=None, params=None):
    try:
        raw = net.fetch(origin, params=params, timeout=max(1.0, timeout), path=path)
        return json.loads(raw.decode('utf-8'))
    except transport.TransportError as failed:
        raise SourceError('rate_limited' if failed.code == 'http_429' else 'unresolved' if failed.code == 'unresolved' else 'unreachable')
    except (UnicodeDecodeError, ValueError):
        raise SourceError('bad_response')


def _langs(value):
    """'ja,en,fr-fr' (a site's STATED languages) -> ['ja', 'en', 'fr'] (base codes, at most 8)."""
    out = []
    for code in (value.split(',') if isinstance(value, str) else []):
        base = code.strip().lower().split('-')[0]
        if re.fullmatch(r'[a-z]{2,3}', base) and base not in out:
            out.append(base)
    return out[:8]


class _Memo(object):
    """A small bounded memory of lookups (ids found or not found), so an episode change does not ask again."""

    def __init__(self, limit=256):
        self.items, self.limit = {}, limit

    def get(self, key):
        return self.items.get(key, _Memo)

    def put(self, key, value):
        if len(self.items) >= self.limit:
            self.items.pop(next(iter(self.items)))
        self.items[key] = value


def _stopped(ctx):
    return bool(ctx.get('stop') and ctx['stop']())


class NekoBT(object):
    """nekoBT's public JSON API (https://nekobt.to/api/v1, read 2026-11-17). Matched by EXACT id, never by title: a media
    is used only when its stated AniList mapping (anilist.entries / alt_ids / primary) names this AniList entry, and the
    episode only through that entry's stated episode mapping (season, ranges) to nekoBT's own episode ids; torrents are
    those nekoBT lists for exactly those episode ids (a batch counts when it lists the episode). Languages are nekoBT's
    stated audio/subtitle fields. Each request is bounded; `stop` is checked between them."""
    origin = 'nekobt'
    label = 'nekoBT'
    by_id = True

    def __init__(self, transport):
        self.transport, self.media, self.episodes_of = transport, _Memo(), _Memo()

    @staticmethod
    def _entry(found, anilist):
        mapping = found.get('anilist') if isinstance(found.get('anilist'), dict) else {}
        for entry in mapping.get('entries') or []:
            if isinstance(entry, dict) and entry.get('anilist_id') == anilist:
                return entry
        primary = mapping.get('primary') if isinstance(mapping.get('primary'), dict) else {}
        if primary.get('id') == anilist or any(isinstance(a, dict) and a.get('anilist_id') == anilist for a in mapping.get('alt_ids') or []):
            return dict(anilist_id=anilist)
        return None

    def _media(self, ctx, until, clock):
        known = self.media.get(ctx['anilist'])
        if known is not _Memo:
            return known
        for alias in ctx['aliases'][:2]:
            if _stopped(ctx):
                return None
            data = _json('nekobt', self.transport, until - clock(), 'media/search', dict(query=alias[:120], limit=10))
            for found in ((data.get('data') or {}).get('results') or []) if isinstance(data, dict) else []:
                entry = self._entry(found, ctx['anilist']) if isinstance(found, dict) else None
                if entry is not None and isinstance(found.get('id'), str) and re.fullmatch(r'[a-z]?[0-9]{1,12}', found['id']):
                    self.media.put(ctx['anilist'], (found['id'], entry))
                    return found['id'], entry
        self.media.put(ctx['anilist'], None)
        return None

    def search(self, ctx, timeout):
        import time as _time
        clock = _time.monotonic
        until = clock() + timeout
        found = self._media(ctx, until, clock)
        if found is None or _stopped(ctx):
            return []
        media_id, entry = found
        params = dict(media_id=media_id, sort_by='seeders', limit=100)
        if not ctx.get('movie'):
            season, start, end, first = entry.get('season'), entry.get('dst_start'), entry.get('dst_end'), entry.get('src_start')
            if type(season) is not int or not all(type(v) is int for v in (start, end, first)) or not start <= ctx['episode'] <= end:
                return []          # no stated mapping for this episode: nothing claimed
            target = (season, first + ctx['episode'] - start)
            listed = self.episodes_of.get(media_id)
            if listed is _Memo:
                data = _json('nekobt', self.transport, until - clock(), 'media/' + media_id)
                listed = {}
                for ep in ((data.get('data') or {}).get('episodes') or []) if isinstance(data, dict) else []:
                    if isinstance(ep, dict) and type(ep.get('season')) is int and type(ep.get('episode')) is int and ep.get('id') is not None:
                        listed.setdefault((ep['season'], ep['episode']), []).append(str(ep['id']))
                self.episodes_of.put(media_id, listed)
            wanted = listed.get(target) or []
            if not wanted or _stopped(ctx):
                return []
            params.update(episode_ids=','.join(wanted[:8]), episode_match_any='true')
        data = _json('nekobt', self.transport, until - clock(), 'torrents/search', params)
        out = []
        for row in ((data.get('data') or {}).get('results') or [])[:MAX_ITEMS] if isinstance(data, dict) else []:
            if not isinstance(row, dict) or row.get('deleted') or row.get('hidden'):
                continue
            seeders, size = str(row.get('seeders') or ''), str(row.get('filesize') or '')
            record = _record(row.get('title'), row.get('infohash'), int(seeders) if seeders.isdigit() else None,
                             int(size) if size.isdigit() else None, self.origin)
            if record:
                record.update(matched='id', audio=_langs(row.get('audio_lang')), subtitles=_langs(row.get('sub_lang')),
                              pack=row.get('batch') is True)
                out.append(record)
        return out


_STATS = re.compile(r'\U0001F464\s*(\d+)')            # seeders, as Torrentio writes them
_SIZE_IN = re.compile(r'\U0001F4BE\s*([\d.]+\s*[KMGT]?B)', re.I)
_SITE_IN = re.compile(r'⚙️?\s*([^\n]{1,40})')


class Torrentio(object):
    """Torrentio's public Stremio stream answers (https://torrentio.strem.fun, read 2026-11-17), by EXACT id: the Kitsu
    entry Kitsu's own public mapping states for this entry's MyAnimeList id (kitsu.app/api/edge/mappings), then
    stream/series/kitsu:<id>:<episode>.json (stream/movie/kitsu:<id>.json for a film). Torrentio numbers episodes per
    Kitsu entry, as AniList does per entry. Each answer row: release name, seeders, size and the index it came from."""
    origin = 'torrentio'
    label = 'Torrentio'
    by_id = True

    def __init__(self, transport):
        self.transport, self.kitsu = transport, _Memo()

    def _kitsu(self, mal, timeout):
        known = self.kitsu.get(mal)
        if known is not _Memo:
            return known
        data = _json('kitsu', self.transport, timeout, 'mappings', {'filter[externalSite]': 'myanimelist/anime', 'filter[externalId]': str(mal), 'include': 'item'})
        found = None
        for item in (data.get('included') or []) if isinstance(data, dict) else []:
            if isinstance(item, dict) and item.get('type') == 'anime' and isinstance(item.get('id'), str) and item['id'].isdigit():
                found = int(item['id'])
                break
        self.kitsu.put(mal, found)
        return found

    def search(self, ctx, timeout):
        import time as _time
        until = _time.monotonic() + timeout
        if not ctx.get('mal'):
            return []
        kitsu = self._kitsu(ctx['mal'], timeout)
        if kitsu is None or _stopped(ctx):
            return []
        path = 'stream/movie/kitsu:%d.json' % kitsu if ctx.get('movie') else 'stream/series/kitsu:%d:%d.json' % (kitsu, ctx['episode'])
        data = _json('torrentio', self.transport, until - _time.monotonic(), path)
        out = []
        for row in (data.get('streams') or [])[:MAX_ITEMS] if isinstance(data, dict) else []:
            if not isinstance(row, dict) or not isinstance(row.get('title'), str):
                continue
            lines = [l for l in row['title'].split('\n') if l.strip()]
            stats = next((l for l in lines if '\U0001F464' in l or '\U0001F4BE' in l), '')
            names = [l for l in lines if l is not stats]
            seeders = _STATS.search(stats)
            size = _SIZE_IN.search(stats)
            record = _record(names[0] if names else None, row.get('infoHash'), int(seeders.group(1)) if seeders else None,
                             _size(size.group(1).replace(' ', '') if size else None) if size else None, self.origin)
            if record:
                site = _SITE_IN.search(stats)
                record.update(matched='id', via_index=site.group(1).strip() if site else None,
                              pack=len(names) > 1)       # a torrent name and, below it, the episode's file: a pack
                # 2026-11-17: the file Torrentio names for THIS episode inside the torrent (its stated file name, else the
                # line under the torrent name): a hint Cinema's file selector uses when the file's own name agrees.
                hints = row.get('behaviorHints') if isinstance(row.get('behaviorHints'), dict) else {}
                named = hints.get('filename') if isinstance(hints.get('filename'), str) else (names[1] if len(names) > 1 else None)
                if named and 0 < len(named.strip()) <= 300 and not re.search(r'[\x00-\x1f/\\]', named.strip()):
                    record['file'] = named.strip()
                out.append(record)
        return out


def _norm(text):
    """Lower-case words only, accents folded ("Pokémon" = "Pokemon"), full-width forms folded (NFKD)."""
    import unicodedata
    text = ''.join(c for c in unicodedata.normalize('NFKD', text or '') if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text.lower()).split())


_SEASON_WORDS = re.compile(r'\b(?:season[ ._-]?\d{1,2}|\d{1,2}(?:st|nd|rd|th)[ ._-]season|s\d{1,2}|part[ ._-]?\d{1,2}|cour[ ._-]?\d{1,2})\b', re.I)


def base_name(alias):
    """A catalogue name without its season words ("X Season 2" -> "X"), or None when it has none."""
    stripped = ' '.join(_SEASON_WORDS.sub(' ', alias).split()).strip(' :-')
    return stripped if stripped and stripped != alias.strip() else None


def own_names(aliases, others):
    """This entry's OWN names (2026-11-17, user report: a spin-off special played as another special): its aliases minus
    any that is also the name of a related entry (a parent series, a sibling): such a name says "this franchise", not
    "this work". The title stays even if every alias is shared."""
    taken = {_norm(o) for o in others if isinstance(o, str) and _norm(o)}
    own = [a for a in aliases if _norm(a) and _norm(a) not in taken]
    return own or aliases[:1]


def names_work(texts, aliases, season):
    """True when a release title or its named file NAMES this work: one of its own full names inside it; or, for a later
    season released under the series' base name, the base name WITH that same season stated ("X S02E05"). An index's id
    claim alone is not this (Torrentio associates releases with Kitsu ids by its own title guess: "Pokemon Mystery
    Dungeon 1" came back for the 2012 Gates to Infinity shorts)."""
    texts = [t for t in texts if isinstance(t, str) and t.strip()]
    flat = [_norm(t) for t in texts]
    for alias in aliases:
        name = _norm(alias)
        if len(name) >= 4 and any(re.search(r'(?<![a-z0-9])%s(?![a-z0-9])' % re.escape(name), f) for f in flat):
            return True
    for alias in aliases:
        base = base_name(alias)
        name = _norm(base) if base else ''
        if season and len(name) >= 4:
            for raw, f in zip(texts, flat):
                if re.search(r'(?<![a-z0-9])%s(?![a-z0-9])' % re.escape(name), f) and release.season_of(raw) == season:
                    return True
    return False


def relevant(record, aliases, episode):
    """A release for this title (one of its names, normalized, inside the release title) that covers this episode.
    A later season's entry is often released under the show's base name with the season in it ("X S02E05"): the base
    name counts only when the release names a season itself (the provider then requires it to be the entry's season)."""
    name = _norm(record['title'])
    named = any(_norm(a) and _norm(a) in name for a in aliases)
    if not named and release.season_of(record['title']) is not None:
        named = any(base_name(a) and _norm(base_name(a)) in name for a in aliases)
    if not named:
        return False
    found = release.episodes_of(record['title'])
    if found is None:
        return bool(re.search(r'\b(batch|complete)\b', record['title'], re.I))
    return found[0] <= episode <= found[1]
