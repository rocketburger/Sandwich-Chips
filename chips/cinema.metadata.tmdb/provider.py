"""TMDB metadata plugin for Cinema (optional; installed separately, see README.md).

Resolves an item conservatively, by stable ids first:
  movie   ids.tmdb -> /movie/{id}; else ids.imdb -> /find (one movie result);
          else a search that must give exactly one exact-title match (and year, if known)
  tvshow  ids.tmdb -> /tv/{id}; else ids.tvdb / ids.imdb -> /find (one tv result);
          else the same strict search on the show title
  episode the show is found from ids.tvshow.tmdb, else ids.tvshow.tvdb / .imdb via /find,
          else the episode's own ids.imdb / ids.tvdb via /find (tv_episode_results, which
          must agree on season and episode), else a strict search on the show title.
          A bare ids.tmdb on an episode is ignored (addons use it for either the show or
          the episode). Season and episode numbers are required and checked.
Anything ambiguous or unmatched returns None. Returned: overview, backdrop, cast,
director, studio, country, a content rating for the configured country (certification,
not a review score), premiered. Never: scores or stream availability.

Trailers (1.2; 1.3 ranking): trailers(query, settings, fetch) answers Cinema's trailer lookups (cinema_trailers) by
exact id only ({media_type: movie | tvshow, ids: {tmdb | imdb}, season?}; an IMDb id through /find): the YouTube
trailers and teasers TMDB lists, as {"trailers": [{transport: "youtube", video: <key>, title, language, categories}]}.
With a season (an episode's), that season's own videos (/tv/{id}/season/{n}/videos) come first, the show's after.
Ranking: TMDB's official flag and type, then the name: a spoof, parody, fan trailer, reaction or review is dropped; a
special's, recap's or clip's ranks low; "Official Trailer" and the matching season's name rank high; newest first among
equals. Cinema plays them in YouTube's official embedded player, as KinoCheck's.

The title hub (1.1): also title, year, poster, tagline, genres, runtime (minutes), crew
(directors, writers, creators), related (TMDB recommendations), collection (the other
films of a movie's collection) and a `ref` on the title, on every cast and crew entry and
on every related title: {"kind": "movie" | "tvshow" | "person", "tmdb": <id>}. Cinema
keeps a ref behind an opaque token and later asks resolve(ref, settings, fetch) for that
identity's own hub: a title (the same fields) or a person (name, biography, birthday,
deathday, place, department, image, known_for).

Requests: HTTPS through Cinema's `fetch` (bounded size and time), at most four per lookup,
a 12 s budget per lookup, and after a 429 no requests for the given Retry-After (or 30 s).
The API Read Access Token is sent as a bearer header (never in a URL); a v3 API key is
sent as the api_key parameter. Errors are raised without the key; Cinema logs only the
exception type.
"""
import re
import threading
import time
import unicodedata

API = 'https://api.themoviedb.org/3'
IMAGE = 'https://image.tmdb.org/t/p/'
BUDGET = 12.0
MAX_REQUESTS = 4
_backoff_until = 0.0
_backoff_lock = threading.Lock()


class RateLimited(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class ProviderError(Exception):
    pass


class Client(object):
    def __init__(self, key, fetch, language):
        self.key, self.fetch, self.language = key, fetch, language
        self.deadline = time.monotonic() + BUDGET
        self.requests = 0

    def get(self, path, **params):
        global _backoff_until
        with _backoff_lock:
            if time.monotonic() < _backoff_until:
                raise RateLimited()
        if time.monotonic() > self.deadline or self.requests >= MAX_REQUESTS:
            raise BudgetExceeded()
        self.requests += 1
        params = {k: v for k, v in params.items() if v not in (None, '')}
        params.setdefault('language', self.language)
        headers = {}
        # A Read Access Token (a JWT: three dot-separated parts) goes in a header; a v3
        # API key can only be a query parameter.
        if self.key.count('.') == 2 and len(self.key) > 60:
            headers['Authorization'] = 'Bearer ' + self.key
        else:
            params['api_key'] = self.key
        try:
            return self.fetch(API + path, params, headers)
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if code == 404:
                return None
            if code == 429:
                retry = 30
                try:
                    retry = max(1, min(300, int((getattr(exc, 'headers', None) or {}).get('Retry-After', 30))))
                except (TypeError, ValueError):
                    pass
                with _backoff_lock:
                    _backoff_until = time.monotonic() + retry
                raise RateLimited()
            if code:
                raise ProviderError('TMDB answered %s' % code)  # Status only, never the URL.
            raise


def _norm(text):
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def _year(date):
    return int(date[:4]) if isinstance(date, str) and re.match(r'\d{4}-', date) else None


def _date(value):
    return value if isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\d', value) else None


def _image(path, size):
    return IMAGE + size + path if isinstance(path, str) and re.fullmatch(r'/[A-Za-z0-9_.-]+', path) else None


def _int(value):
    return int(value) if isinstance(value, int) or (isinstance(value, str) and value.isdigit()) else None


def _unique(results, title, year, title_keys, date_key):
    """Exactly one result whose title equals ours (and year, when known)."""
    want = _norm(title)
    if not want:
        return None
    exact = [r for r in results or [] if isinstance(r, dict) and want in {_norm(r.get(k)) for k in title_keys}]
    if year:
        exact = [r for r in exact if _year(r.get(date_key)) == year]
    return exact[0] if len(exact) == 1 else None


def _find(client, external_id, source):
    return client.get('/find/' + external_id, external_source=source) or {}


def _ref(kind, value):
    ident = _int(value)
    return dict(kind=kind, tmdb=ident) if ident and ident > 0 else None


def _entry(person, role):
    entry = dict(name=person['name'])
    if isinstance(role, str) and role.strip():
        entry['role'] = role.strip()[:80]
    image = _image(person.get('profile_path'), 'w185')
    if image:
        entry['image'] = image
    ref = _ref('person', person.get('id'))
    if ref:
        entry['ref'] = ref
    return entry


def _cast(people, limit=20):
    out, seen = [], set()
    for person in people or []:
        if not isinstance(person, dict) or not isinstance(person.get('name'), str) or not person['name'] or person['name'] in seen:
            continue
        seen.add(person['name'])
        role = person.get('character')
        if not role and isinstance(person.get('roles'), list) and person['roles'] and isinstance(person['roles'][0], dict):
            role = person['roles'][0].get('character')  # TV aggregate credits
        out.append(_entry(person, role))
        if len(out) >= limit:
            break
    return out


CREW_JOBS = ('Director', 'Screenplay', 'Writer', 'Story', 'Novel', 'Creator')


def _crew(crew, creators=(), limit=8):
    """Directors, writers and (shows) creators, in that order, one entry per person and job."""
    out, seen = [], set()
    rows = [dict(p, job='Creator') for p in creators or [] if isinstance(p, dict)] + [p for p in crew or [] if isinstance(p, dict)]
    for job in CREW_JOBS:
        for person in rows:
            if person.get('job') != job or not isinstance(person.get('name'), str) or not person['name'] or person['name'] in seen:
                continue
            seen.add(person['name'])
            out.append(_entry(person, job))
            if len(out) >= limit:
                return out
    return out


def _titles(results, kind=None, limit=20, exclude=None):
    """Title cards: title, year, poster and a ref. kind: the kind of every row, else each row's media_type."""
    out, seen = [], set()
    for row in results or []:
        if not isinstance(row, dict):
            continue
        row_kind = kind or {'movie': 'movie', 'tv': 'tvshow'}.get(row.get('media_type'))
        ref = _ref(row_kind, row.get('id')) if row_kind else None
        title = row.get('title') or row.get('name')
        if not ref or not isinstance(title, str) or not title.strip() or (row_kind, ref['tmdb']) in seen or ref['tmdb'] == exclude:
            continue
        seen.add((row_kind, ref['tmdb']))
        entry = dict(title=title.strip()[:120], ref=ref)
        year = _year(row.get('release_date') or row.get('first_air_date'))
        if year:
            entry['year'] = year
        image = _image(row.get('poster_path'), 'w342')
        if image:
            entry['image'] = image
        if isinstance(row.get('character'), str) and row['character'].strip():
            entry['role'] = row['character'].strip()[:80]
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def _names(rows, limit=3):
    return [r['name'] for r in rows or [] if isinstance(r, dict) and isinstance(r.get('name'), str) and r['name']][:limit]


def _directors(crew):
    return [p['name'] for p in crew or [] if isinstance(p, dict) and p.get('job') == 'Director' and p.get('name')][:3]


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _minutes(value):
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, int)), None)
    return value if isinstance(value, int) and 0 < value < 1000 else None


def _hub(data, kind, title_key, date_key):
    """The title hub fields common to movies and shows."""
    return dict(title=_text(data.get(title_key), 200), year=_year(data.get(date_key)), ref=_ref(kind, data.get('id')),
                poster=_image(data.get('poster_path'), 'w342'), tagline=_text(data.get('tagline'), 200),
                genres=_names(data.get('genres'), 3),
                related=_titles((data.get('recommendations') or {}).get('results'), kind, exclude=_int(data.get('id'))))


def _imdb(value):
    """The title's IMDb id as TMDB states it (tt + digits), or None: lets Cinema find it in add-ons that state only IMDb."""
    return value if isinstance(value, str) and re.fullmatch(r'tt\d{5,10}', value) else None


def _clean(record):
    return {k: v for k, v in record.items() if v not in (None, '', [], {})}


# Movies ------------------------------------------------------------------------------
def _movie_id(client, identity, ids):
    if _int(ids.get('tmdb')):
        return _int(ids.get('tmdb'))
    imdb = ids.get('imdb')
    if isinstance(imdb, str) and re.fullmatch(r'tt\d{5,10}', imdb):
        found = _find(client, imdb, 'imdb_id').get('movie_results') or []
        return found[0].get('id') if len(found) == 1 else None
    year = identity.get('year')
    search = client.get('/search/movie', query=identity.get('title'), year=year) or {}
    match = _unique(search.get('results'), identity.get('title'), year, ('title', 'original_title'), 'release_date')
    return match.get('id') if match else None


def _certification(data, region):
    for country in (data.get('release_dates') or {}).get('results') or []:
        if country.get('iso_3166_1') == region:
            dates = sorted(country.get('release_dates') or [], key=lambda r: (r.get('type') != 3, r.get('type') or 9))
            for entry in dates:
                if entry.get('certification'):
                    return entry['certification']
    return None


def movie(client, identity, ids, region):
    ident = _movie_id(client, identity, ids)
    if not ident:
        return None
    data = client.get('/movie/%d' % ident, append_to_response='credits,release_dates,recommendations')
    if not data:
        return None
    credits = data.get('credits') or {}
    out = dict(
        imdb=_imdb(data.get('imdb_id')),
        overview=data.get('overview'), backdrop=_image(data.get('backdrop_path'), 'w1280'),
        cast=_cast(credits.get('cast')), director=_directors(credits.get('crew')), crew=_crew(credits.get('crew')),
        studio=_names(data.get('production_companies')), country=_names(data.get('production_countries'), 2),
        rated=_certification(data, region), premiered=_date(data.get('release_date')), runtime=_minutes(data.get('runtime')),
        **_hub(data, 'movie', 'title', 'release_date'))
    out['collection'] = _collection(client, data.get('belongs_to_collection'), _int(data.get('id')))
    return _clean(out)


def _collection(client, collection, current):
    """The other films of the movie's collection, in release order. Optional: a failure here keeps the rest."""
    ident = _int(collection.get('id')) if isinstance(collection, dict) else None
    if not ident:
        return None
    try:
        data = client.get('/collection/%d' % ident) or {}
    except (BudgetExceeded, ProviderError, OSError, ValueError):
        return None
    parts = sorted([p for p in data.get('parts') or [] if isinstance(p, dict)], key=lambda p: p.get('release_date') or '9999')
    items = _titles(parts, 'movie', exclude=current)
    name = _text(data.get('name') or collection.get('name'), 120)
    return dict(name=name, items=items) if name and items else None


# Series and episodes -------------------------------------------------------------------
def _show_from_find(client, ids, prefix):
    for key, source in ((prefix + 'tvdb', 'tvdb_id'), (prefix + 'imdb', 'imdb_id')):
        value = ids.get(key)
        if isinstance(value, str) and (value.isdigit() or re.fullmatch(r'tt\d{5,10}', value)):
            found = _find(client, value, source).get('tv_results') or []
            if len(found) == 1:
                return found[0].get('id')
            return None  # Ambiguous or unknown: do not try weaker evidence on top.
    return None


def _show_search(client, title, year=None):
    search = client.get('/search/tv', query=title, first_air_date_year=year) or {}
    match = _unique(search.get('results'), title, year, ('name', 'original_name'), 'first_air_date')
    return match.get('id') if match else None


def _rating(show, region):
    for row in (show.get('content_ratings') or {}).get('results') or []:
        if row.get('iso_3166_1') == region and row.get('rating'):
            return row['rating']
    return None


def tvshow(client, identity, ids, region):
    ident = _int(ids.get('tmdb')) or _show_from_find(client, ids, '') or \
        (None if ids.get('tvdb') or ids.get('imdb') else _show_search(client, identity.get('title'), identity.get('year')))
    if not ident:
        return None
    data = client.get('/tv/%d' % ident, append_to_response='credits,content_ratings,recommendations,external_ids')
    if not data:
        return None
    credits = data.get('credits') or {}
    return _clean(dict(
        imdb=_imdb((data.get('external_ids') or {}).get('imdb_id')),
        overview=data.get('overview'), backdrop=_image(data.get('backdrop_path'), 'w1280'),
        cast=_cast(credits.get('cast')), crew=_crew(credits.get('crew'), data.get('created_by')),
        studio=_names(data.get('production_companies')),
        country=_names(data.get('production_countries'), 2), rated=_rating(data, region),
        premiered=_date(data.get('first_air_date')), runtime=_minutes(data.get('episode_run_time')),
        **_hub(data, 'tvshow', 'name', 'first_air_date')))


def episode(client, identity, ids, region):
    season, number = identity.get('season'), identity.get('episode')
    if not (isinstance(season, int) and season >= 0 and isinstance(number, int) and number >= 1):
        return None
    show = _int(ids.get('tvshow.tmdb')) or _show_from_find(client, ids, 'tvshow.')
    if not show:
        for key, source in (('imdb', 'imdb_id'), ('tvdb', 'tvdb_id')):
            value = ids.get(key)
            if isinstance(value, str) and (value.isdigit() or re.fullmatch(r'tt\d{5,10}', value)):
                found = _find(client, value, source).get('tv_episode_results') or []
                found = [r for r in found if isinstance(r, dict) and r.get('season_number') == season and r.get('episode_number') == number]
                show = _int(found[0].get('show_id')) if len(found) == 1 else None
                break
    if not show and identity.get('showtitle') and not any(ids.get(k) for k in ('tvshow.tvdb', 'tvshow.imdb', 'imdb', 'tvdb')):
        show = _show_search(client, identity.get('showtitle'))
    if not show:
        return None
    data = client.get('/tv/%d/season/%d/episode/%d' % (show, season, number), append_to_response='credits')
    if not data or data.get('season_number') != season or data.get('episode_number') != number:
        return None
    series = client.get('/tv/%d' % show, append_to_response='content_ratings') or {}
    credits = data.get('credits') or {}
    crew = data.get('crew') or credits.get('crew')
    return _clean(dict(
        overview=data.get('overview'), backdrop=_image(data.get('still_path'), 'w780'),
        cast=_cast(list(credits.get('cast') or []) + list(data.get('guest_stars') or credits.get('guest_stars') or [])),
        director=_directors(crew), crew=_crew(crew), studio=_names(series.get('production_companies')),
        country=_names(series.get('production_countries'), 2), rated=_rating(series, region),
        premiered=_date(data.get('air_date')), runtime=_minutes(data.get('runtime')),
        show=_ref('tvshow', show), genres=_names(series.get('genres'), 3)))


# People ------------------------------------------------------------------------------
# Talk and news shows, and appearances as oneself, are not what a person is known for.
NOT_KNOWN_FOR_GENRES = {10763, 10767}
SELF = re.compile(r'^(self|himself|herself|themselves|narrator \(voice\))$', re.I)


def person(client, ident):
    data = client.get('/person/%d' % ident, append_to_response='combined_credits')
    if not data or not isinstance(data.get('name'), str) or not data['name'].strip():
        return None
    cast = [c for c in ((data.get('combined_credits') or {}).get('cast') or []) if isinstance(c, dict)
            and not NOT_KNOWN_FOR_GENRES & set(c.get('genre_ids') or []) and not SELF.match(str(c.get('character') or '').strip())]
    crew = [c for c in ((data.get('combined_credits') or {}).get('crew') or []) if isinstance(c, dict) and c.get('job') in CREW_JOBS]
    rank = lambda c: -(c.get('vote_count') if isinstance(c.get('vote_count'), (int, float)) else 0)
    known = sorted(cast + [dict(c, character=c.get('job')) for c in crew], key=rank)
    return _clean(dict(
        kind='person', name=data['name'].strip()[:120], ref=_ref('person', data.get('id')),
        biography=_text(data.get('biography'), 4000), birthday=_date(data.get('birthday')), deathday=_date(data.get('deathday')),
        place=_text(data.get('place_of_birth'), 120), department=_text(data.get('known_for_department'), 40),
        image=_image(data.get('profile_path'), 'h632'), known_for=_titles(known)))


def _client(settings, fetch):
    key = (settings.get('api_key') or '').strip()
    if not key:
        return None, None
    language = (settings.get('language') or 'en-US').strip()
    language = language if re.fullmatch(r'[a-z]{2}(-[A-Z]{2})?', language) else 'en-US'
    region = (settings.get('region') or 'US').strip().upper()
    region = region if re.fullmatch(r'[A-Z]{2}', region) else 'US'
    return Client(key, fetch, language), region


def resolve(ref, settings, fetch):
    """The hub of one TMDB identity a lookup returned (a ref): a title or a person, or None."""
    if not isinstance(ref, dict):
        return None
    kind, ident = ref.get('kind'), ref.get('tmdb')
    if kind not in ('movie', 'tvshow', 'person') or type(ident) is not int or ident <= 0:
        return None
    client, region = _client(settings, fetch)
    if client is None:
        return None
    if kind == 'person':
        return person(client, ident)
    found = (movie if kind == 'movie' else tvshow)(client, dict(media_type=kind), {'tmdb': str(ident)}, region)
    return dict(found, kind=kind) if found else None


VIDEO_KEY = re.compile(r'[A-Za-z0-9_-]{11}')
VIDEO_TYPES = ('Trailer', 'Teaser')
# Names that mark a video as something other than the title's own trailer (a spoof, a special's or a recap), and names
# that mark the real one. Words only; TMDB's own type and "official" flag count for more.
NOT_TRAILER = re.compile(r'\b(spoof|parody|horror trailer|fan[- ]?made|fan trailer|reaction|unboxing|review)\b', re.I)  # dropped
OFF_NAME = re.compile(r'\b(recap|blooper|behind the scenes|christmas|holiday|special|reunion|interview|clip)\b', re.I)  # ranked low
REAL_NAME = re.compile(r'\b(official (main |final )?trailer|main trailer|final trailer)\b', re.I)
SEASON_NAME = re.compile(r'\b(season|series|s)\s*0*(\d{1,3})\b', re.I)


def _video_score(video, season=None):
    """How likely a video is the title's (or, with season, that season's) own trailer. Higher is better."""
    name = video.get('name') if isinstance(video.get('name'), str) else ''
    score = 0
    score += 40 if video.get('_season') else 0                 # listed under the episode's own season
    score += 20 if video.get('official') else 0
    score += 10 if video.get('type') == 'Trailer' else 0
    score += 8 if REAL_NAME.search(name) else 0
    score -= 30 if OFF_NAME.search(name) else 0
    named = SEASON_NAME.search(name)
    if season is not None and named:
        score += 12 if int(named.group(2)) == season else -6     # "Season 4 Trailer" for a season-4 episode, or another season's
    return score


def trailers(query, settings, fetch):
    """Title-level YouTube trailers TMDB lists for one exact id, best first; None when there are none."""
    if not isinstance(query, dict) or query.get('media_type') not in ('movie', 'tvshow'):
        return None
    client, _ = _client(settings, fetch)
    if client is None:
        return None
    kind, ids = query['media_type'], query.get('ids') or {}
    ident = _int(ids.get('tmdb'))
    if not ident and isinstance(ids.get('imdb'), str):
        found = _find(client, ids['imdb'], 'imdb_id').get('movie_results' if kind == 'movie' else 'tv_results') or []
        ident = _int(found[0].get('id')) if len(found) == 1 and isinstance(found[0], dict) else None
    if not ident or ident <= 0:
        return None
    language = client.language.split('-')[0] if getattr(client, 'language', None) else 'en'
    langs = '%s,en,null' % language
    season = query.get('season') if kind == 'tvshow' and type(query.get('season')) is int and 0 <= query['season'] <= 200 else None
    usable = lambda data: [v for v in (data or {}).get('results') or [] if isinstance(v, dict) and v.get('site') == 'YouTube'
                           and v.get('type') in VIDEO_TYPES and isinstance(v.get('key'), str) and VIDEO_KEY.fullmatch(v['key'])
                           and not NOT_TRAILER.search(v.get('name') if isinstance(v.get('name'), str) else '')]
    rows = []
    if season is not None:
        # An episode's season: that season's own trailers first (TMDB lists them per season), the show's after.
        rows = [dict(v, _season=True) for v in usable(client.get('/tv/%d/season/%d/videos' % (ident, season), include_video_language=langs))]
    rows += usable(client.get('/%s/%d/videos' % ('movie' if kind == 'movie' else 'tv', ident), include_video_language=langs))
    seen, unique = set(), []
    for v in rows:
        if v['key'] not in seen:
            seen.add(v['key'])
            unique.append(v)
    unique.sort(key=lambda v: str(v.get('published_at') or ''), reverse=True)
    unique.sort(key=lambda v: -_video_score(v, season))  # stable: newest first among equals
    out = []
    for v in unique[:6]:
        lang = v.get('iso_639_1') if isinstance(v.get('iso_639_1'), str) and re.fullmatch(r'[a-z]{2}', v['iso_639_1']) else None
        out.append(dict(transport='youtube', video=v['key'], title=_text(v.get('name'), 160), language=lang, categories=[v['type']]))
    return dict(trailers=out) if out else None


# Groups (1.4, 2026-11-18): identified relationship facts, never a title's identity and never playable ------------------
# groups({"text"}) -> candidates of three DIFFERENT meanings, each TMDB's own entity, labelled as such:
#   collection  a film collection TMDB lists (/search/collection; members from /collection/{id})
#   company     a production company (/search/company): titles crediting it (discover with_companies). A production
#               credit is not ownership, distribution, a parent company's catalogue or a canon.
#   topic       a keyword (/search/keyword): titles TMDB tags with it (discover with_keywords). Tagging is incomplete
#               and is not a canon or a complete brand catalogue.
# One page of each search (three requests, inside the four-request budget), at most GROUP_CANDIDATES of each, in TMDB's
# order. A failed search of one kind leaves the others ("partial"). Nothing is merged: similarly named companies stay
# apart, subsidiaries are not inferred, and no group is named after another (e.g. a keyword is never "the MCU").
# group({"ref", "page", "section", "sort"}) -> one group's facts and ONE page of members per section ("movie", "tvshow");
# with "section", only that section's next page. sort "released": by release date (a sorting, not a viewing order);
# "popular" (default for companies and topics): TMDB's popularity. Members are titles by exact id (ref {kind, tmdb}); a
# movie and a show with the same number are different titles.
GROUP_CANDIDATES = 6
GROUP_KINDS = ('collection', 'company', 'topic')
MAX_PAGE = 500     # TMDB's discover limit
COUNTRY = re.compile(r'[A-Z]{2}')


def _candidate(kind, row):
    ident = _int(row.get('id')) if isinstance(row, dict) else None
    name = _text(row.get('name'), 120) if isinstance(row, dict) else None
    if not ident or ident <= 0 or not name:
        return None
    out = dict(ref=dict(kind=kind, tmdb=ident), title=name)
    image = _image(row.get('poster_path') or row.get('logo_path'), 'w342' if kind == 'collection' else 'w185')
    if image:
        out['image'] = image
    country = row.get('origin_country')
    if kind == 'company' and isinstance(country, str) and COUNTRY.fullmatch(country):
        out['country'] = country     # tells same-named companies apart
    return out


def _relevant(name, words):
    """TMDB's company and keyword searches are fuzzy ("marcel" for another word). A candidate is kept only when every word
    of the query begins a word of its name (accents folded; a query with no Latin letters or digits keeps everything).
    Words only: no name is special."""
    if not words:
        return True
    have = _norm(name).split()
    return all(any(w.startswith(q) for w in have) for q in words)


def groups(query, settings, fetch):
    text = query.get('text') if isinstance(query, dict) else None
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 100:
        return None
    words = _norm(text).split()
    client, _ = _client(settings, fetch)
    if client is None:
        return None
    out, failed = [], 0
    for kind, path in (('collection', '/search/collection'), ('company', '/search/company'), ('topic', '/search/keyword')):
        try:
            data = client.get(path, query=text.strip(), page=1) or {}
        except RateLimited:
            raise
        except (BudgetExceeded, ProviderError, OSError, ValueError):
            failed += 1
            continue
        found = [c for c in (_candidate(kind, r) for r in (data.get('results') or [])) if c and _relevant(c['title'], words)][:GROUP_CANDIDATES]
        out.extend(found)
    if failed == 3:
        raise ProviderError('TMDB group search failed')
    return dict(groups=out, partial=failed > 0, cache_seconds=3600)


def _members(rows, kind):
    out = []
    for entry in _titles(rows, kind, limit=20):
        out.append(entry)
    return out


def _page_of(data, kind):
    page, pages = _int((data or {}).get('page')) or 1, min(MAX_PAGE, _int((data or {}).get('total_pages')) or 1)
    return dict(id=kind, items=_members((data or {}).get('results'), kind), page=page, pages=pages,
                total=_int((data or {}).get('total_results')))


def group(request, settings, fetch):
    if not isinstance(request, dict) or not isinstance(request.get('ref'), dict):
        return None
    kind, ident = request['ref'].get('kind'), request['ref'].get('tmdb')
    if kind not in GROUP_KINDS or type(ident) is not int or ident <= 0:
        return None
    page = request.get('page') if type(request.get('page')) is int and 1 <= request['page'] <= MAX_PAGE else 1
    section = request.get('section') if request.get('section') in ('movie', 'tvshow') else None
    sort = request.get('sort') if request.get('sort') in ('popular', 'released') else ('released' if kind == 'collection' else 'popular')
    client, _ = _client(settings, fetch)
    if client is None:
        return None
    if kind == 'collection':
        data = client.get('/collection/%d' % ident)
        if not data:
            return None
        parts = [p for p in data.get('parts') or [] if isinstance(p, dict)]
        if sort == 'released':
            parts.sort(key=lambda p: p.get('release_date') or '9999')
        else:
            parts.sort(key=lambda p: -(p.get('popularity') if isinstance(p.get('popularity'), (int, float)) else 0))
        items = _titles(parts, 'movie', limit=60)
        return _clean(dict(kind=kind, ref=dict(kind=kind, tmdb=ident), title=_text(data.get('name'), 120), overview=_text(data.get('overview'), 2000),
                           image=_image(data.get('poster_path'), 'w342'), backdrop=_image(data.get('backdrop_path'), 'w1280'), sort=sort,
                           complete=True, sections=[dict(id='movie', items=items, page=1, pages=1, total=len(items))]))
    order = {'movie': 'popularity.desc' if sort == 'popular' else 'primary_release_date.asc',
             'tvshow': 'popularity.desc' if sort == 'popular' else 'first_air_date.asc'}
    flt = {'company': 'with_companies', 'topic': 'with_keywords'}[kind]
    facts = {}
    if section is None:
        about = client.get('/company/%d' % ident if kind == 'company' else '/keyword/%d' % ident)
        if not about:
            return None
        facts = dict(title=_text(about.get('name'), 120), overview=_text(about.get('description'), 2000), image=_image(about.get('logo_path'), 'w185'))
        if kind == 'company':
            country = about.get('origin_country')
            facts['country'] = country if isinstance(country, str) and COUNTRY.fullmatch(country) else None
            parent = about.get('parent_company')
            facts['parent'] = _text(parent.get('name'), 120) if isinstance(parent, dict) else None   # a stated fact, never merged
    sections, partial = [], False
    for media in ([section] if section else ['movie', 'tvshow']):
        try:
            data = client.get('/discover/%s' % ('movie' if media == 'movie' else 'tv'), **{flt: str(ident), 'page': page, 'sort_by': order[media],
                                                                                        'include_adult': 'false'})
        except RateLimited:
            raise
        except (BudgetExceeded, ProviderError, OSError, ValueError):
            partial = True
            continue
        sections.append(_page_of(data, media))
    if not sections:
        raise ProviderError('TMDB group members could not be read')
    return _clean(dict(kind=kind, ref=dict(kind=kind, tmdb=ident), sort=sort, complete=False, partial=partial or None,
                       sections=sections, **facts))


def lookup(identity, settings, fetch):
    client, region = _client(settings, fetch)
    if client is None:
        return None
    ids = {str(k).lower(): str(v) for k, v in (identity.get('ids') or {}).items()}
    kind = identity.get('media_type')
    if kind == 'movie':
        return movie(client, identity, ids, region)
    if kind == 'tvshow':
        return tvshow(client, identity, ids, region)
    if kind == 'episode':
        return episode(client, identity, ids, region)
    return None
