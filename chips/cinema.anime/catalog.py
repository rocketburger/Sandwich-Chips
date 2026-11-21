"""Anime catalogue from AniList's public GraphQL API (read-only, no account or token): titles, facts, related entries
and episode numbers. Documented at https://docs.anilist.co (Media, Page, MediaRelation). Two fixed query texts; answers
are checked field by field. AniList lists one entry per season/cour, special or movie, so "episodes" here are the
episodes OF ONE ENTRY (1..N, its own numbering); sequels and specials are related entries ("parts"), never merged."""
import hashlib
import json
import re
import threading
import time

# Siblings (release, transport) are given to this file by backend.load(): the host loads module files by path, not
# as a package, so they are not imported by name.

LISTED = ('id idMal format status episodes seasonYear title { romaji english userPreferred } synonyms '
          'coverImage { extraLarge large }')
# Search also reads each entry's typed relations (2026-11-16), so seasons of one series found together can be named as
# that series (provider._search_works: the same earliest-entry rule as work.build, over stated PREQUEL edges only).
SEARCH = ('query ($q: String, $page: Int) { Page(page: $page, perPage: 20) { pageInfo { hasNextPage } '
          'media(search: $q, type: ANIME, isAdult: false) { ' + LISTED +
          ' relations { edges { relationType node { id type format } } } } } }')
EPISODIC_FORMATS = ('TV', 'TV_SHORT', 'ONA')   # = work.EPISODIC
# What AniList's public catalogue states about one entry (2026-11-16): its cover and banner, description (plain text
# asked; AniList may still mark line breaks), genres, main studio, start date, mean score (0-100) and the stated
# duration of one episode (or of the film), in minutes.
DETAIL = ('query ($id: Int) { Media(id: $id, type: ANIME) { id idMal format status episodes seasonYear duration countryOfOrigin '
          'title { romaji english userPreferred } synonyms nextAiringEpisode { episode airingAt } '
          'coverImage { extraLarge large } bannerImage description(asHtml: false) genres averageScore '
          'studios(isMain: true) { nodes { name } } startDate { year month day } '
          'relations { edges { relationType node { id type format status episodes title { userPreferred } '
          'coverImage { large } nextAiringEpisode { episode airingAt } } } } '
          'streamingEpisodes { title thumbnail url site } } }')
# streamingEpisodes (2026-11-19, user report: a show airing now had no episode titles or pictures, ani.zip not stating
# them yet): AniList's own list of the entry's episodes on its legal streaming sites ("Episode 3 - Name" and a
# thumbnail). Kept as bounded strings here; episode_meta.streaming reads the number and the name from them.
STREAMING_MAX = 2000
# Discovery (2026-11-16): one fixed query text; the variables choose the list. AniList documents these Page.media
# arguments (https://docs.anilist.co/reference/query: sort MediaSort, season MediaSeason, seasonYear, status, genre,
# format_in). A variable left out of the request is not applied; a variable SENT as null is not the same thing to
# AniList (format_in null: HTTP 500; status null: no titles), so discover() never sends one.
DISCOVER = ('query ($page: Int, $sort: [MediaSort], $season: MediaSeason, $seasonYear: Int, $status: MediaStatus, '
            '$genre: String, $format: [MediaFormat]) { Page(page: $page, perPage: 24) { pageInfo { hasNextPage } '
            'media(type: ANIME, isAdult: false, sort: $sort, season: $season, seasonYear: $seasonYear, status: $status, '
            'genre: $genre, format_in: $format) { ' + LISTED + ' relations { edges { relationType node { id type format } } } } } }')
GENRES = ('Action', 'Adventure', 'Comedy', 'Drama', 'Ecchi', 'Fantasy', 'Horror', 'Mahou Shoujo', 'Mecha', 'Music',
          'Mystery', 'Psychological', 'Romance', 'Sci-Fi', 'Slice of Life', 'Sports', 'Supernatural', 'Thriller')
SORTS = ('TRENDING_DESC', 'POPULARITY_DESC', 'SCORE_DESC', 'START_DATE_DESC')
SEASONS = ('WINTER', 'SPRING', 'SUMMER', 'FALL')
ART = re.compile(r'https://s4\.anilist\.co/file/anilistcdn/[A-Za-z0-9/_.%-]{1,300}')
BY_MAL = ('query ($mal: Int) { Media(idMal: $mal, type: ANIME) { id idMal format status episodes seasonYear '
          'title { romaji english userPreferred } synonyms } }')
# Franchise reads (2026-11-17, user report "Slime S1 shows only S1"): up to 50 entries in ONE request (Page.media id_in,
# documented at docs.anilist.co), each with its start date, airing state and EVERY typed relation to another anime entry
# (the neighbour's id, format, status, episode count, title and cover), so a whole franchise is read in a few requests
# instead of one per entry. franchise.py walks them; nothing here decides what belongs together.
NODES = ('query ($ids: [Int], $page: Int) { Page(page: $page, perPage: 50) { pageInfo { hasNextPage } '
         'media(id_in: $ids, type: ANIME) { id idMal format status episodes seasonYear startDate { year month day } '
         'title { romaji english userPreferred } synonyms coverImage { large } nextAiringEpisode { episode airingAt } '
         'relations { edges { relationType node { id type format status episodes seasonYear title { userPreferred } '
         'coverImage { large } } } } } } }')
NODES_MAX = 50
# Several entries' details in ONE request (0.6.0, 2026-11-21: AniList's 30-90 requests a minute): the DETAIL fields over
# Page.media id_in. Details asked for together (Home's rows, a page of titles) wait BATCH_WAIT for each other and go
# as one request of at most DETAILS_MAX entries (kept small: each carries its relations and streaming episodes).
DETAILS = DETAIL.replace('query ($id: Int) { Media(id: $id, type: ANIME) {',
                         'query ($ids: [Int]) { Page(page: 1, perPage: %d) { media(id_in: $ids, type: ANIME) {' % 10) + ' }'
DETAILS_MAX, BATCH_WAIT = 10, 0.08
# Answers kept on disk across restarts (0.6.0; Cinema's ctx.cache): an entry that is finished changes rarely; one that
# airs is kept only until its next episode airs (then its aired count is stale); lists and searches briefly.
KEEP = dict(finished=3 * 86400, airing=86400, unknown=3600, discover=3600, search=6 * 3600, mal=7 * 86400, node=2 * 86400)
COUNTRY = re.compile(r'[A-Z]{2}')
FORMATS = ('TV', 'TV_SHORT', 'MOVIE', 'SPECIAL', 'OVA', 'ONA', 'MUSIC')
STATUS = ('FINISHED', 'RELEASING', 'NOT_YET_RELEASED', 'CANCELLED', 'HIATUS')
RELATION = {'SEQUEL': 'sequel', 'PREQUEL': 'prequel', 'SIDE_STORY': 'side_story', 'SPIN_OFF': 'side_story',
            'ALTERNATIVE': 'alternative', 'SUMMARY': 'other', 'PARENT': 'other'}
MAX_EPISODES = 2000
RELATION_TYPES = ('SEQUEL', 'PREQUEL', 'SIDE_STORY', 'SPIN_OFF', 'ALTERNATIVE', 'SUMMARY', 'PARENT', 'CHARACTER', 'ADAPTATION',
                  'SOURCE', 'COMPILATION', 'CONTAINS', 'OTHER')
_TITLE = re.compile(r'[^\x00-\x1f\x7f]{1,200}')
HTML_BREAK = re.compile(r'<br\s*/?>', re.I)
HTML_TAG = re.compile(r'<[^<>]{0,200}>')


class CatalogError(Exception):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


def _title(raw):
    t = raw.get('title') if isinstance(raw.get('title'), dict) else {}
    names = [t.get(k) for k in ('userPreferred', 'english', 'romaji')]
    names = [n.strip() for n in names if isinstance(n, str) and _TITLE.fullmatch(n.strip())]
    return names


def _media(raw):
    if not isinstance(raw, dict) or type(raw.get('id')) is not int or raw['id'] <= 0:
        return None
    names = _title(raw)
    if not names:
        return None
    synonyms = [s.strip() for s in raw.get('synonyms') or [] if isinstance(s, str) and _TITLE.fullmatch(s.strip())][:8]
    count = raw.get('episodes') if type(raw.get('episodes')) is int and 0 < raw['episodes'] <= MAX_EPISODES else None
    year = raw.get('seasonYear') if type(raw.get('seasonYear')) is int and 1900 < raw['seasonYear'] < 2200 else None
    aliases = []
    for name in names + synonyms:
        if name not in aliases:
            aliases.append(name)
    return dict(id=raw['id'], mal=raw.get('idMal') if type(raw.get('idMal')) is int else None, title=names[0], aliases=aliases[:10],
                format=raw.get('format') if raw.get('format') in FORMATS else None, status=raw.get('status') if raw.get('status') in STATUS else None,
                episodes=count, year=year,
                # AniList's stated country of origin (a country, NOT a language: see controls.INFERRED_LANGUAGE).
                country=raw.get('countryOfOrigin') if isinstance(raw.get('countryOfOrigin'), str) and COUNTRY.fullmatch(raw['countryOfOrigin']) else None,
                **_stated(raw))


def _episodic_links(raw):
    """{'PREQUEL': [ids], 'SEQUEL': [ids]}: the entry's stated PREQUEL/SEQUEL relations to episodic ANIME entries, or None
    when the answer states no relations at all (unknown: no series is derived from it)."""
    edges = (raw.get('relations') or {}).get('edges') if isinstance(raw.get('relations'), dict) else None
    if not isinstance(edges, list):
        return None
    out = {'PREQUEL': [], 'SEQUEL': []}
    for edge in edges:
        node = edge.get('node') if isinstance(edge, dict) else None
        kind = edge.get('relationType') if isinstance(edge, dict) else None
        if kind in out and isinstance(node, dict) and node.get('type') == 'ANIME' and node.get('format') in EPISODIC_FORMATS \
                and type(node.get('id')) is int and node['id'] > 0 and node['id'] not in out[kind]:
            out[kind].append(node['id'])
    return out


def _edges(raw):
    """[(relation type, neighbour id, neighbour format)]: every typed relation to another ANIME entry an answer states,
    or None when it states no relations at all (unknown, never "none")."""
    edges = (raw.get('relations') or {}).get('edges') if isinstance(raw.get('relations'), dict) else None
    if not isinstance(edges, list):
        return None
    out = []
    for edge in edges:
        node = edge.get('node') if isinstance(edge, dict) else None
        kind = edge.get('relationType') if isinstance(edge, dict) else None
        if kind in RELATION_TYPES and isinstance(node, dict) and node.get('type') == 'ANIME' and type(node.get('id')) is int and node['id'] > 0:
            out.append((kind, node['id'], node.get('format') if node.get('format') in FORMATS else None))
    return out


def _start(raw):
    start = raw.get('startDate') if isinstance(raw.get('startDate'), dict) else {}
    y, m, d = (start.get(k) for k in ('year', 'month', 'day'))
    if type(y) is not int or not 1900 < y < 2200:
        return None
    return (y, m if type(m) is int and 1 <= m <= 12 else 0, d if type(d) is int and 1 <= d <= 31 else 0)


def _neighbours(raw):
    """{id: what the answer states about each related ANIME entry} (title, format, status, episodes, year, cover)."""
    out = {}
    edges = (raw.get('relations') or {}).get('edges') if isinstance(raw.get('relations'), dict) else None
    for edge in edges if isinstance(edges, list) else []:
        node = edge.get('node') if isinstance(edge, dict) else None
        if not isinstance(node, dict) or node.get('type') != 'ANIME' or type(node.get('id')) is not int or node['id'] in out:
            continue
        names = _title(node)
        if not names:
            continue
        out[node['id']] = dict(id=node['id'], title=names[0], format=node.get('format') if node.get('format') in FORMATS else None,
                               status=node.get('status') if node.get('status') in STATUS else None,
                               episodes=node.get('episodes') if type(node.get('episodes')) is int and 0 < node['episodes'] <= MAX_EPISODES else None,
                               year=node.get('seasonYear') if type(node.get('seasonYear')) is int and 1900 < node['seasonYear'] < 2200 else None,
                               cover=_image((node.get('coverImage') or {}).get('large') if isinstance(node.get('coverImage'), dict) else None))
    return out


def _image(value):
    return value if isinstance(value, str) and ART.fullmatch(value) else None


def _stated(raw):
    """What AniList states beyond identity (2026-11-16): images on AniList's own CDN only, plain-text description,
    genres, main studio, a full start date, the 0-100 mean score and the stated duration in minutes. Unknown stays None."""
    cover = raw.get('coverImage') if isinstance(raw.get('coverImage'), dict) else {}
    description = raw.get('description') if isinstance(raw.get('description'), str) else ''
    description = HTML_BREAK.sub('\n', description)
    description = ' '.join(HTML_TAG.sub('', description).replace('&amp;', '&').replace('&quot;', '"').replace('&#039;', "'").split())
    genres = [g for g in (raw.get('genres') or []) if isinstance(g, str) and _TITLE.fullmatch(g)][:8] if isinstance(raw.get('genres'), list) else []
    nodes = ((raw.get('studios') or {}).get('nodes') if isinstance(raw.get('studios'), dict) else None) or []
    studios = [n['name'].strip() for n in nodes if isinstance(n, dict) and isinstance(n.get('name'), str) and _TITLE.fullmatch(n['name'].strip())][:3]
    start = raw.get('startDate') if isinstance(raw.get('startDate'), dict) else {}
    y, m, d = start.get('year'), start.get('month'), start.get('day')
    premiered = '%04d-%02d-%02d' % (y, m, d) if all(type(v) is int for v in (y, m, d)) and 1900 < y < 2200 and 1 <= m <= 12 and 1 <= d <= 31 else None
    score = raw.get('averageScore')
    return dict(cover=_image(cover.get('extraLarge')) or _image(cover.get('large')), banner=_image(raw.get('bannerImage')),
                description=description[:2000] or None, genres=genres, studios=studios, premiered=premiered,
                score=score if type(score) is int and 0 < score <= 100 else None,
                duration=raw.get('duration') if type(raw.get('duration')) is int and 0 < raw['duration'] <= 1440 else None)


class _Slot(object):
    def __init__(self):
        self.done, self.raw, self.error = threading.Event(), None, None


def _keep(raw, kind):
    """Seconds an answer about this entry stays on disk: finished entries long; airing ones until their next episode airs
    (and at most a day); anything else an hour."""
    status = raw.get('status') if isinstance(raw, dict) else None
    if status in ('FINISHED', 'CANCELLED'):
        return KEEP['node'] if kind == 'node' else KEEP['finished']
    nxt = raw.get('nextAiringEpisode') if isinstance(raw, dict) and isinstance(raw.get('nextAiringEpisode'), dict) else {}
    at = nxt.get('airingAt')
    if type(at) is int:
        return max(300, min(KEEP['airing'], at - time.time() + 600))
    return KEEP['unknown']


class Catalog(object):
    def __init__(self, transport, cache=None, sleep=time.sleep):
        """cache: Cinema's ctx.cache (get(key), put(key, value, ttl)) or None: answers then live only in memory."""
        self.transport, self.cache, self.sleep = transport, cache, sleep
        self.lock = threading.Lock()
        self.waiting, self.leading = {}, False

    # ---- the disk cache (0.6.0) -------------------------------------------------------------------------------------
    def _held(self, key):
        if self.cache is None:
            return None
        try:
            return self.cache.get(key)
        except Exception:
            return None

    def _hold(self, key, value, ttl):
        if self.cache is not None and value is not None:
            try:
                self.cache.put(key, value, ttl)
            except Exception:
                pass        # a cache that cannot be written only costs a re-read

    @staticmethod
    def _key(kind, variables):
        return '%s:%s' % (kind, hashlib.sha256(json.dumps(variables, sort_keys=True).encode('utf-8')).hexdigest()[:32])

    def _ask(self, query, variables, timeout):
        try:
            raw = self.transport.fetch('anilist', body=dict(query=query, variables=variables), timeout=timeout)
            data = json.loads(raw.decode('utf-8'))
        except transport.TransportError as failed:
            raise CatalogError('rate_limited' if failed.code == 'http_429' else 'unreachable')
        except (UnicodeDecodeError, ValueError):
            raise CatalogError('bad_response')
        if not isinstance(data, dict) or data.get('errors') or not isinstance(data.get('data'), dict):
            raise CatalogError('bad_response')
        return data['data']

    def search(self, query, page=1, timeout=20.0):
        """([media], has_next)."""
        key = self._key('search', dict(q=query, page=page))
        data = self._held(key)
        if not isinstance(data, dict):
            data = self._ask(SEARCH, dict(q=query, page=page), timeout)
            self._hold(key, data, KEEP['search'])
        found = data.get('Page') if isinstance(data.get('Page'), dict) else None
        if found is None or not isinstance(found.get('media'), list):
            raise CatalogError('bad_response')
        info = found.get('pageInfo') if isinstance(found.get('pageInfo'), dict) else {}
        out = []
        for raw in found['media'][:20]:
            media = _media(raw)
            if media:
                media['links'] = _episodic_links(raw)
                media['edges'] = _edges(raw)       # every typed relation: one franchise card per search (2026-11-17)
                out.append(media)
        return out, info.get('hasNextPage') is True

    def nodes(self, ids, timeout=20.0):
        """{id: node} for up to NODES_MAX entries in one request. A node: the entry (as _media states it) with its start
        date, aired count, every typed relation (edges) and what is stated about each neighbour (neighbours). An id
        AniList does not answer for is simply absent (the caller says what could not be read)."""
        ids = [i for i in dict.fromkeys(ids) if type(i) is int and i > 0][:NODES_MAX]
        if not ids:
            return {}
        # Each entry kept on disk on its own (0.6.0): only the ones not held are asked, still in one request.
        raws = {}
        for ident in ids:
            held = self._held('node:%d' % ident)
            if isinstance(held, dict) and held.get('id') == ident:
                raws[ident] = held
        missing = [i for i in ids if i not in raws]
        if missing:
            data = self._ask(NODES, dict(ids=missing, page=1), timeout)
            found = data.get('Page') if isinstance(data.get('Page'), dict) else None
            if found is None or not isinstance(found.get('media'), list):
                raise CatalogError('bad_response')
            for raw in found['media'][:NODES_MAX]:
                if isinstance(raw, dict) and raw.get('id') in missing:
                    raws[raw['id']] = raw
                    self._hold('node:%d' % raw['id'], raw, _keep(raw, 'node'))
        out = {}
        for raw in [raws[i] for i in ids if i in raws]:
            media = _media(raw)
            if media is None or media['id'] not in ids:
                continue
            nxt = raw.get('nextAiringEpisode') if isinstance(raw.get('nextAiringEpisode'), dict) else None
            media.update(start=_start(raw), next_airing=_airing(nxt), edges=_edges(raw) or [], neighbours=_neighbours(raw),
                         aired=nxt['episode'] - 1 if nxt and type(nxt.get('episode')) is int and 0 < nxt['episode'] <= MAX_EPISODES + 1 else None,
                         season=release.season_of(media['title']) or next((release.season_of(a) for a in media['aliases'] if release.season_of(a)), None))
            out[media['id']] = media
        return out

    def _detail_raw(self, media_id, timeout):
        """AniList's Media answer for one entry: from disk, else asked TOGETHER with every other detail asked within
        BATCH_WAIT (one request for up to DETAILS_MAX). The first caller leads: it waits, then asks for what is waiting
        and hands each caller its own entry. Raises CatalogError."""
        key = 'detail:%d' % media_id
        held = self._held(key)
        if isinstance(held, dict) and held.get('id') == media_id:
            return held
        with self.lock:
            slot = self.waiting.get(media_id)
            if slot is None:
                slot = self.waiting[media_id] = _Slot()
            lead = not self.leading
            if lead:
                self.leading = True
        if lead:
            while True:
                self.sleep(BATCH_WAIT)
                with self.lock:
                    ids = list(self.waiting)[:DETAILS_MAX]
                    slots = {i: self.waiting.pop(i) for i in ids}
                    more = bool(self.waiting)
                    if not more:
                        self.leading = False
                try:
                    if len(ids) == 1:
                        data = self._ask(DETAIL, dict(id=ids[0]), timeout)
                        found = [data.get('Media')]
                    else:
                        data = self._ask(DETAILS, dict(ids=ids), timeout)
                        page = data.get('Page') if isinstance(data.get('Page'), dict) else {}
                        found = page.get('media') if isinstance(page.get('media'), list) else None
                        if found is None:
                            raise CatalogError('bad_response')
                    for raw in found:
                        if isinstance(raw, dict) and raw.get('id') in slots:
                            slots[raw['id']].raw = raw
                            self._hold('detail:%d' % raw['id'], raw, _keep(raw, 'detail'))
                except CatalogError as failed:
                    for s in slots.values():
                        s.error = failed.code
                finally:
                    for s in slots.values():
                        s.done.set()
                if not more:
                    break
        if not slot.done.wait(timeout + 5):
            raise CatalogError('unreachable')
        if slot.error:
            raise CatalogError(slot.error)
        if slot.raw is None:
            raise CatalogError('bad_response')
        return slot.raw

    def detail(self, media_id, timeout=20.0):
        """(media, aired_count or None, parts)."""
        raw = self._detail_raw(media_id, timeout)
        media = _media(raw)
        if media is None or media['id'] != media_id:
            raise CatalogError('bad_response')
        nxt = raw.get('nextAiringEpisode') if isinstance(raw.get('nextAiringEpisode'), dict) else None
        aired = nxt['episode'] - 1 if nxt and type(nxt.get('episode')) is int and 0 < nxt['episode'] <= MAX_EPISODES + 1 else None
        media['next_airing'] = _airing(nxt)
        parts = []
        edges = (raw.get('relations') or {}).get('edges') if isinstance(raw.get('relations'), dict) else None
        for edge in edges if isinstance(edges, list) else []:
            node = edge.get('node') if isinstance(edge, dict) else None
            if not isinstance(node, dict) or node.get('type') != 'ANIME' or type(node.get('id')) is not int:
                continue
            names = _title(node)
            if not names:
                continue
            relation = 'movie' if node.get('format') == 'MOVIE' else 'special' if node.get('format') in ('SPECIAL', 'OVA', 'ONA') and edge.get('relationType') not in ('SEQUEL', 'PREQUEL') \
                else RELATION.get(edge.get('relationType'), 'other')
            node_next = node.get('nextAiringEpisode') if isinstance(node.get('nextAiringEpisode'), dict) else None
            parts.append(dict(id=node['id'], title=names[0], relation=relation, relation_type=edge.get('relationType') if edge.get('relationType') in RELATION_TYPES else 'OTHER',
                              cover=_image((node.get('coverImage') or {}).get('large') if isinstance(node.get('coverImage'), dict) else None),
                              format=node.get('format') if node.get('format') in FORMATS else None,
                              status=node.get('status') if node.get('status') in STATUS else None,
                              episodes=node.get('episodes') if type(node.get('episodes')) is int and 0 < node['episodes'] <= MAX_EPISODES else None,
                              next_airing=_airing(node_next)))
        media['season'] = release.season_of(media['title']) or next((release.season_of(a) for a in media['aliases'] if release.season_of(a)), None)
        media['streaming'] = _streaming(raw.get('streamingEpisodes'))
        return media, aired, parts[:32]


    def discover(self, page=1, sort='POPULARITY_DESC', season=None, year=None, status=None, genre=None, formats=None, timeout=20.0):
        """One page of a public catalogue list: ([media], has_next). Every variable is checked against fixed choices."""
        if sort not in SORTS or (season is not None and season not in SEASONS) or (status is not None and status not in STATUS) \
                or (genre is not None and genre not in GENRES) or any(f not in FORMATS for f in formats or ()) \
                or (year is not None and not (type(year) is int and 1940 <= year <= 2100)) or not (type(page) is int and 1 <= page <= 50):
            raise CatalogError('bad_request')
        # An unset filter is LEFT OUT of the variables, never sent as null (2026-11-16, user report "Browse headings, no
        # titles"): AniList answers a null format_in with HTTP 500 and a null status with an empty list, so every Browse
        # section failed or came back empty. Public reads through this transport confirmed both; omitted variables apply
        # no filter.
        variables = dict(page=page, sort=[sort], season=season, seasonYear=year, status=status, genre=genre,
                         format=list(formats) if formats else None)
        variables = {k: v for k, v in variables.items() if v is not None}
        key = self._key('discover', variables)
        data = self._held(key)
        if not isinstance(data, dict):
            data = self._ask(DISCOVER, variables, timeout)
            self._hold(key, data, KEEP['discover'])
        found = data.get('Page') if isinstance(data.get('Page'), dict) else None
        if found is None or not isinstance(found.get('media'), list):
            raise CatalogError('bad_response')
        info = found.get('pageInfo') if isinstance(found.get('pageInfo'), dict) else {}
        out = []
        for raw in found['media'][:24]:
            media = _media(raw)
            if media:
                media['links'] = _episodic_links(raw)    # a page's seasons of one series are one listing (2026-11-16)
                media['edges'] = _edges(raw)
                out.append(media)
        return out, info.get('hasNextPage') is True

    def by_mal(self, mal, timeout=20.0):
        """The AniList entry that states this MyAnimeList id, or None. 0.6.0: the id is mapped by ani.zip's public
        mappings (not AniList's limited API), kept on disk, and the entry read as a detail (itself held); AniList's own
        idMal lookup only when ani.zip does not answer. The entry must still state that MAL id itself."""
        key = 'mal:%d' % mal
        anilist = self._held(key)
        if type(anilist) is not int:
            anilist = None
            try:
                raw = json.loads(self.transport.fetch('anizip', params={'mal_id': mal}, timeout=min(timeout, 10.0)).decode('utf-8'))
                mapped = (raw.get('mappings') or {}) if isinstance(raw, dict) else {}
                if mapped.get('mal_id') == mal and type(mapped.get('anilist_id')) is int and mapped['anilist_id'] > 0:
                    anilist = mapped['anilist_id']
                    self._hold(key, anilist, KEEP['mal'])
            except (transport.TransportError, UnicodeDecodeError, ValueError, AttributeError):
                anilist = None
        if anilist is not None:
            try:
                media = self.detail(anilist, timeout)[0]
                return media if media['mal'] == mal else None
            except CatalogError:
                pass
        data = self._ask(BY_MAL, dict(mal=mal), timeout)
        media = _media(data.get('Media')) if data.get('Media') is not None else None
        return media if media is not None and media['mal'] == mal else None


def _streaming(raw):
    """[{title, thumbnail}] as AniList states its streamingEpisodes (strings only, bounded; either may be None)."""
    out = []
    for e in raw[:STREAMING_MAX] if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        title = e.get('title') if isinstance(e.get('title'), str) and 0 < len(e['title']) <= 300 else None
        thumb = e.get('thumbnail') if isinstance(e.get('thumbnail'), str) and 0 < len(e['thumbnail']) <= 500 else None
        if title or thumb:
            out.append(dict(title=title, thumbnail=thumb))
    return out


def _airing(raw):
    if not isinstance(raw, dict) or type(raw.get('episode')) is not int or not 0 < raw['episode'] <= MAX_EPISODES + 1:
        return None
    return dict(episode=raw['episode'], at=raw['airingAt'] if type(raw.get('airingAt')) is int and raw['airingAt'] > 0 else None)


def episode_numbers(media, aired):
    """The episode numbers of one entry that can be offered: all of a finished one; the aired ones of a running one (an
    unknown count with no airing information offers none, said by the caller)."""
    if media['status'] == 'RELEASING' and aired is not None:
        return list(range(1, aired + 1))
    if media['episodes']:
        return list(range(1, media['episodes'] + 1))
    if aired:
        return list(range(1, aired + 1))
    return []
