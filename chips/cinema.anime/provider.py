"""The Anime extension's cinema.media-provider (contract 1.1) implementation: search, detail, episodes, sources, resolve.

Cinema owns the screens and Settings; this provider owns the data and the logic. Flow: search (AniList titles) ->
detail (facts, related seasons/specials/movies as parts) -> episodes (one entry's own numbering) -> sources (Nyaa and
AnimeTosho releases for that episode, read by release.py, checked against the user's debrid cache through Accounts &
Services, filtered and sorted by the declared controls) -> resolve a source (its files, through Accounts' debrid.resolve,
each marked with whether it is the chosen episode) -> resolve a file (one https stream Cinema's playback opens).

Locators are opaque and short: al:<id> (a title), ep:<id>:<n> (an episode), pg:<n> (an episodes page), src:<token> and
file:<token> (server-held: a release's hash, a file's link). Tokens live in this instance's memory only, bounded and
expiring; a stale one answers locator_expired. Nothing is written anywhere but the module's own preference storage.

Siblings (release, transport, controls, catalog, sources) are given by backend.load(). No Kodi, no add-on code: this
file imports nothing from xbmc*, Kodi bridges or any add-on."""
import re
import secrets
import threading
import time
from collections import OrderedDict

ACCOUNTS = ('cinema.accounts', 1, 'cinema.accounts')
PAGE = 100
TOKEN_TTL, TOKENS_MAX = 1800.0, 512
DETAIL_TTL, DETAILS_MAX = 600.0, 64
NODE_TTL, NODES_MAX = 1800.0, 2000     # franchise entries read in batches (franchise.py), shared by search and detail
SEARCH_REQUESTS = 3                    # extra AniList requests one search page may make to join its franchise cards
MAX_ALIASES = 3
PER_SITE = 4          # searches per source site for one episode
SNAPSHOT_TTL, SNAPSHOTS_MAX = 300, 16   # one episode's complete discovery, reused by filter changes (Codex R4)
UNRESOLVED_HOLD = 600  # seconds a site whose feed address did not resolve is not searched again (said in the notes)
CHECK_CHUNK, CHECK_MAX = 50, 200
CHECK_BUDGET, CHECK_CHUNK_TIME = 30.0, 16.0   # the cache check's own time (Accounts allows a check 15 s), per chunk
CHECK_WHY = {None: 'no reason was given', 'timed_out': 'no answer in time', 'busy': 'Accounts & Services was busy',
             'rate_limited': 'the service is limiting requests just now', 'network': 'the service could not be reached',
             'service_error': 'the service reported an error', 'bad_response': 'its answer could not be read',
             'too_large': 'its answer was too large', 'authentication_failed': 'the service did not accept the connection',
             'permission_denied': 'the service refused this request', 'not_connected': 'no service is connected',
             'needs_attention': 'the connection needs attention in Accounts & Services', 'provider_unavailable': 'Accounts & Services is not available',
             'invalid_request': 'the request was not accepted'}
FORMAT_MEDIA = dict(TV='tvshow', TV_SHORT='tvshow', ONA='tvshow', MOVIE='movie', SPECIAL='special', OVA='special', MUSIC='special')
# Browse's format filter (2026-11-16): a choice -> AniList formats (None: any).
FORMAT_FILTER = dict(any=None, tv=('TV', 'TV_SHORT'), movie=('MOVIE',), special=('SPECIAL', 'OVA'), ona=('ONA',))
RETIRED_HOLD = 6 * 3600   # a source whose every documented address refuses (AnimeTosho, 2026-11-17) is asked again after this
NOTE_CODE = dict(rate_limited='busy', unreachable='provider_unavailable', unresolved='provider_unavailable', bad_response='provider_failed',
                 retired='provider_unavailable')
NOTE_TEXT = dict(rate_limited='%s is limiting searches just now; try again shortly.', unreachable='%s could not be reached.',
                 unresolved='%s\'s feed address does not exist in DNS right now, so it was not searched; the other sources are listed. '
                            'Turn it off under Filters to stop this note.',
                 retired='%s answered nothing: both of its documented feed addresses refuse (its operator announced the feed and API '
                         'shutdown on 2026-09-16). The other sources are listed; turn it off under Filters to stop this note.',
                 bad_response='%s answered something this extension could not read.')
FALLBACK_TEXT = 'No sources match your filters. Showing available sources for this search.'
EPISODE_FACTS_TIMEOUT = 5.0   # one ani.zip read per entry (episode_meta.py), cached; a slow or failed read leaves numbers only


ORIGIN_LABEL = dict(nyaa='Nyaa', nekobt='nekoBT', torrentio='Torrentio', animetosho='AnimeTosho')


def _named_first(rows):
    """The chosen order, with sources whose name does not name this work (an index's id claim only) after all others."""
    return [r for r in rows if (r.get('identity') or {}).get('state') != 'unverified'] + \
           [r for r in rows if (r.get('identity') or {}).get('state') == 'unverified']


def fail(code, message=None):
    out = dict(status='error', code=code)
    if message:
        out['message'] = message
    return out


class _Expiring(object):
    """A bounded, expiring map of server-held values (oldest dropped first)."""

    def __init__(self, ttl, limit, clock):
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self.lock = threading.Lock()
        self.items = OrderedDict()

    def put(self, key, value):
        with self.lock:
            self.items[key] = (self.clock() + self.ttl, value)
            self.items.move_to_end(key)
            while len(self.items) > self.limit:
                self.items.popitem(last=False)

    def get(self, key):
        with self.lock:
            found = self.items.get(key)
            if found is None:
                return None
            if found[0] <= self.clock():
                del self.items[key]
                return None
            return found[1]

    def clear(self):
        with self.lock:
            self.items.clear()


class Debrid(object):
    """The user's debrid connection, through Accounts & Services only (cinema.accounts/1, submit/result/cancel). This
    extension never sees a key: it asks which hashes are cached and for one cached torrent's files."""

    def __init__(self, services, clock=time.monotonic, sleep=time.sleep):
        self.services, self.clock, self.sleep = services, clock, sleep

    def _accounts(self):
        try:
            if hasattr(self.services, 'get_one'):            # by contract (Cinema 2026-11-17+, D5): any one implementation
                return self.services.get_one(*ACCOUNTS[:2])
            return self.services.get(*ACCOUNTS)
        except Exception:
            return None

    def provider(self):
        """(accounts, provider name) of a provider offering debrid with a usable connection, or (None, reason).

        Usable (2026-11-16, restart fix): "connected", else "unknown" (Accounts' word for a stored connection not verified
        since Cinema started; it is not "not connected"). The request is then tried and Accounts' answer decides; a
        verified connection is preferred over an unverified one."""
        accounts = self._accounts()
        if accounts is None:
            return None, 'accounts_off'
        try:
            status = accounts.status()
        except Exception:
            return None, 'accounts_off'
        found = {}
        # Each provider's own display label, as Accounts states it (2026-11-17 modularity audit: no producer table here).
        self.labels = {e['provider']: e['label'] for e in (status.get('providers', []) if isinstance(status, dict) else [])
                       if isinstance(e, dict) and isinstance(e.get('provider'), str) and isinstance(e.get('label'), str) and e['label']}
        for entry in status.get('providers', []) if isinstance(status, dict) else []:
            if isinstance(entry, dict) and 'debrid.availability' in (entry.get('operations') or []) \
                    and entry.get('connection') in ('connected', 'unknown'):
                found.setdefault(entry['connection'], entry['provider'])
        for connection in ('connected', 'unknown'):
            if connection in found:
                return accounts, found[connection]
        return None, 'not_connected'

    def ask(self, accounts, provider, operation, args, cancelled, until, why=None):
        """('done', value) | ('refused', code) | ('cancelled', None) | ('timed_out', None). `why` (a dict, optional) is
        given the provider's own fixed reason for a refusal (Accounts' `detail`), when it stated one."""
        try:
            answer = accounts.submit(operation, provider, args)
        except Exception:
            return 'refused', 'provider_unavailable'
        if not answer.get('ok'):
            return 'refused', answer.get('code')
        request = answer['request']
        while True:
            try:
                got = accounts.result(request)
            except Exception:
                return 'refused', 'provider_unavailable'
            if got.get('state') == 'done':
                return 'done', got.get('value')
            if got.get('state') in ('refused', 'cancelled'):
                if why is not None and isinstance(got.get('detail'), str):
                    why['detail'] = got['detail']
                return 'refused', got.get('code')
            if cancelled() or self.clock() >= until:
                try:
                    accounts.cancel(request)
                except Exception:
                    pass
                return ('cancelled', None) if cancelled() else ('timed_out', None)
            self.sleep(0.05)


class AnimeProvider(object):
    def __init__(self, catalog, readers, debrid, stored, spawn=None, clock=time.monotonic, sleep=time.sleep, facts=None):
        """catalog: catalog.Catalog; readers: {origin: reader}; debrid: Debrid; stored(): the module's storage dict (the
        persistent control values); spawn(fn): tracked work (ctx.spawn); facts: episode_meta.EpisodeFacts (None: none)."""
        self.catalog, self.readers, self.debrid, self.stored, self.facts = catalog, readers, debrid, stored, facts
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.clock, self.sleep = clock, sleep
        self.tokens = _Expiring(TOKEN_TTL, TOKENS_MAX, clock)
        self.snapshots = _Expiring(SNAPSHOT_TTL, SNAPSHOTS_MAX, clock)
        self.recent = _Expiring(SNAPSHOT_TTL, SNAPSHOTS_MAX, clock)   # incomplete discoveries: for a view change only
        self.details = _Expiring(DETAIL_TTL, DETAILS_MAX, clock)
        self.nodes = _Expiring(NODE_TTL, NODES_MAX, clock)
        self.stopped = False

    def stop(self):
        self.stopped = True
        self.tokens.clear()
        self.details.clear()
        self.nodes.clear()

    # ---- descriptor -------------------------------------------------------------------------------------------------
    def describe(self):
        on = lambda: dict(state='supported', interaction='none', auth='none')
        return dict(label='Anime', operations=dict(
            search=on(), detail=on(), episodes=on(), sources=on(), resolve=on(), lookup=on(),
            browse=self.browse_entry()),
            sources=[dict(id='anilist', label='AniList', operations=['search', 'detail', 'episodes'])] +
                    [dict(id=origin, label=reader.label, operations=['sources']) for origin, reader in self.readers.items()],
            controls=controls.CONTROLS, identities=['anilist', 'mal'])

    def _values(self, per_call=None):
        try:
            stored = self.stored() or {}
        except Exception:
            stored = {}
        return controls.effective(stored, per_call)

    # ---- titles -----------------------------------------------------------------------------------------------------
    @classmethod
    def _title_item(cls, media):
        facts = [f for f in (str(media['year']) if media['year'] else None,
                             (media['format'] or '').replace('_', ' ').title() or None,
                             '%d episodes' % media['episodes'] if media['episodes'] else None) if f]
        ids = {'anilist': str(media['id'])}
        if media.get('mal'):
            ids['mal'] = str(media['mal'])
        return dict(title=media['title'], kind='item', media_type=FORMAT_MEDIA.get(media['format'], 'unknown'), year=media['year'],
                    detail=' · '.join(facts) or None, source='anilist', identity=dict(ids=dict(show=ids)),
                    locator=dict(browse='al:%d' % media['id'], play=None), interaction='none',
                    # 2026-11-16: AniList's own cover (a URL Cinema registers as an artwork handle) and what the entry IS,
                    # with "anime" as AniList states it, so a mixed search can filter by it without guessing.
                    art=dict(poster=media['cover']) if media.get('cover') else None, media=cls._ref(media, None))

    @staticmethod
    def _info(media):
        """What AniList states about the entry, as provider information for Cinema's card (data only)."""
        out = dict(overview=media.get('description'), genres=media.get('genres') or None, studio=media.get('studios') or None,
                   premiered=media.get('premiered'), runtime=media.get('duration'), source='AniList')
        if media.get('score'):
            out['score'] = dict(value=media['score'] / 10.0, source='AniList')
        return {k: v for k, v in out.items() if v} if len([v for v in out.values() if v]) > 1 else {}

    def _failed(self, failed):
        return fail('busy' if failed.code == 'rate_limited' else 'provider_unavailable' if failed.code == 'unreachable' else 'provider_failed',
                    NOTE_TEXT.get(failed.code, '%s') % 'AniList')

    def _search_page(self, query, page, cancelled):
        try:
            found, more = self.catalog.search(query, page, self._values()['timeout'])
        except catalog.CatalogError as failed:
            return self._failed(failed)
        if cancelled():
            return fail('cancelled')
        notes = [] if found or page > 1 else [dict(source='anilist', code='no_results', message='AniList found no title by that name.')]
        following = None
        if more and page < 25:
            token = secrets.token_urlsafe(9)
            self.tokens.put(('sq', token), query)
            following = 'sq:%s:%d' % (token, page + 1)
        return dict(status='ok', items=self._items(found), next=following, notes=notes)

    def _items(self, found, timeout=20.0):
        """One page's titles (search or browse): ONE card per franchise (2026-11-17, user report: "Slime" season 1 and
        "Slime 2nd Season" were two cards, the first listing only itself). Page entries that AniList relates over the
        franchise relations (franchise.CHAIN, through OVAs and films, plus a side entry's hop anchor) are one card, led by the page's first (most relevant)
        of them: opening it opens that entry, with every season, OVA and film of the franchise on its card. The page's own
        relations are used first; at most SEARCH_REQUESTS batched reads add what joins them. A read that fails or runs out
        leaves cards apart (never joined on a guess); titles are never compared."""
        seed = {m['id']: dict(edges=m['edges']) for m in found if isinstance(m.get('edges'), list)}
        groups, reached = self._page_groups([m['id'] for m in found], seed, timeout)
        items, led = [], {}
        for m in found:
            group = groups.get(m['id'], m['id'])
            if group in led:
                led[group]['_also'] += 1
                # The exact entries this card stands for (2026-11-17, user report "the umbrella disappears once the
                # add-ons answer"): stated so a consumer joining results from other sources can place another source's
                # title for one of these entries UNDER this card (by exact id), never as a separate per-season title.
                led[group]['members'].append(dict(ids=self._entry_ids(m), media_type=FORMAT_MEDIA.get(m['format'], 'unknown')))
                continue
            item = self._title_item(m)
            item['_also'], item['members'] = 0, []
            led[group] = item
            items.append(item)
        # Every other entry the bounded franchise read reached for a card (not only this page's matches: an English query
        # matches a few entries by name), by exact id, so another source's title for any of them joins this card too.
        for lead, item in led.items():
            listed = {m['ids']['anilist'] for m in item['members']} | {str(lead)}
            for node in reached.get(lead, ()):
                if str(node['id']) not in listed and node.get('format'):
                    listed.add(str(node['id']))
                    item['members'].append(dict(ids=self._entry_ids(node), media_type=FORMAT_MEDIA.get(node['format'], 'unknown')))
            del item['members'][64:]
        for item in items:
            also = item.pop('_also')
            if also:
                item['detail'] = ' · '.join(filter(None, [item['detail'], '%d more of this series matched' % also]))
            if not item['members']:
                del item['members']
        return items

    @staticmethod
    def _entry_ids(media):
        out = {'anilist': str(media['id'])}
        if media.get('mal'):
            out['mal'] = str(media['mal'])
        return out

    def _page_groups(self, ids, seed, timeout):
        """{page id: its group's first page id} for page entries joined over franchise relations (union of what was read)."""
        def read(chunk):
            return self._read_nodes(chunk, timeout)
        try:
            nodes, reached, _complete, _failure = franchise.explore(ids, read, seed=seed, requests=SEARCH_REQUESTS, entries=120, sides=False)
        except Exception:
            return {}, {}
        parent = {i: i for i in nodes}

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        # Joined over PREQUEL/SEQUEL, and an entry outside any chain to its ONE hop anchor (franchise.hop_anchor), or to the
        # entry naming it as PARENT/SIDE_STORY when it names no other anchor itself: never through side relations
        # generally, so a crossover special does not merge two shows' cards (2026-11-19).
        for ident, node in nodes.items():
            joined = [o for k, o, _f in node.get('edges') or () if k in franchise.CHAIN]
            joined += [o for k, o, _f in node.get('edges') or () if k in franchise.HOP and o in nodes
                       and not franchise.in_chain(nodes[o]) and franchise.hop_anchor(nodes[o]) in (None, ident)]
            anchor = franchise.hop_anchor(node)
            for other in joined + ([anchor] if anchor is not None else []):
                if other in parent:
                    a, b = find(ident), find(other)
                    if a != b:
                        parent[max(a, b)] = min(a, b)
        first = {}
        out = {}
        for ident in ids:
            if ident not in parent:
                continue
            out[ident] = first.setdefault(find(ident), ident)
        # {group lead: every READ entry of its component} (exact ids from AniList's own answers; seeds without a
        # format are page entries, listed already).
        members = {}
        for ident, node in nodes.items():
            lead = first.get(find(ident))
            if lead is not None and node.get('format') and 'id' in node:
                members.setdefault(lead, []).append(node)
        return out, members

    def _read_nodes(self, ids, timeout):
        """({id: node}, AniList requests made): cached entries first, the rest in one batched read."""
        got, missing = {}, []
        for ident in ids:
            node = self.nodes.get(ident)
            if node is None:
                missing.append(ident)
            else:
                got[ident] = node
        if not missing:
            return got, 0
        fetched = self.catalog.nodes(missing, timeout)
        for ident, node in fetched.items():
            self.nodes.put(ident, node)
        got.update(fetched)
        return got, 1

    def _franchise(self, media_id, timeout):
        """The franchise of one entry (franchise.build), read through the node cache within franchise.REQUESTS."""
        nodes, reached, complete, failure = franchise.explore([media_id], lambda chunk: self._read_nodes(chunk, timeout))
        return franchise.build(media_id, nodes, reached, complete, failure)

    @staticmethod
    def _search_works(found):
        """{entry id: its series' earliest entry id} for the episodic entries of ONE search page whose series is
        established from the page alone, by work.build's rule: walking back over stated episodic PREQUEL edges reaches,
        through exactly one prequel at every step and no cycle, an entry of this page that states no episodic prequel.
        A chain leaving the page, a branch, or an entry whose relations were not stated: nothing (no guessed series).
        Titles are never compared; movies, specials and OVAs never join a series here (user report: one listing with its
        seasons, never a remake/movie/special merged into a season)."""
        by_id = {m['id']: m for m in found if m.get('format') in work.EPISODIC and isinstance(m.get('links'), dict)}
        out = {}
        for ident in by_id:
            root, seen = ident, {ident}
            while True:
                prequels = by_id[root]['links']['PREQUEL']
                if not prequels:
                    out[ident] = root
                    break
                if len(prequels) > 1 or prequels[0] not in by_id or prequels[0] in seen:
                    break
                root = prequels[0]
                seen.add(root)
        return out

    def search(self, request, cancelled):
        if self.stopped:
            return fail('provider_unavailable')
        return self._search_page(request['query'], 1, cancelled)

    # ---- browse: public catalogue lists, no account (2026-11-16) ------------------------------------------------------
    @staticmethod
    def _season(now=None):
        """(season, year) of the current anime season: Winter Jan-Mar, Spring Apr-Jun, Summer Jul-Sep, Fall Oct-Dec."""
        t = time.gmtime(now)
        return catalog.SEASONS[(t.tm_mon - 1) // 3], t.tm_year

    @classmethod
    def _next_season(cls, now=None):
        season, year = cls._season(now)
        index = catalog.SEASONS.index(season)
        return (catalog.SEASONS[0], year + 1) if index == 3 else (catalog.SEASONS[index + 1], year)

    def browse_entry(self, now=None):
        """The descriptor's browse entry: declared sections and filters (Cinema renders them; this only answers)."""
        year = time.gmtime(now).tm_year
        filters = [dict(key='format', label='Format', kind='choice', choices=list(FORMAT_FILTER), default='any',
                        labels=dict(any='Any', tv='TV', movie='Movie', special='Special / OVA', ona='ONA')),
                   dict(key='year', label='Year', kind='choice', choices=['any'] + [str(y) for y in range(year + 1, year - 30, -1)],
                        default='any', labels=dict(any='Any year')),
                   dict(key='genre', label='Genre', kind='choice', choices=list(catalog.GENRES), default='Action')]
        sections = [dict(id='trending', label='Trending now', filters=['format']),
                    dict(id='season', label='This season', filters=['format']),
                    dict(id='upcoming', label='Next season', filters=['format']),
                    dict(id='popular', label='Popular', filters=['format', 'year']),
                    dict(id='top', label='Top rated', filters=['format', 'year']),
                    dict(id='genre', label='By genre', filters=['genre', 'format', 'year'])]
        return dict(state='supported', interaction='none', auth='none', sections=sections, filters=filters)

    def browse(self, request, cancelled):
        if self.stopped:
            return fail('provider_unavailable')
        if 'locator' in request:   # a search's next page (sq:<token>:<page>)
            head, _, rest = request['locator'].partition(':')
            token, _, page = rest.rpartition(':')
            query = self.tokens.get(('sq', token)) if head == 'sq' else None
            if query is None or not page.isdigit() or not 1 < int(page) <= 25:
                return fail('locator_expired')
            return self._search_page(query, int(page), cancelled)
        section, values = request.get('section'), request.get('filters') or {}
        page = self._id(request['next'], 'bp') if request.get('next') else [1]
        if not page or len(page) != 1 or page[0] > 50:
            return fail('invalid_request')
        year = int(values['year']) if str(values.get('year', 'any')).isdigit() else None
        ask = dict(page=page[0], formats=FORMAT_FILTER.get(values.get('format', 'any')))
        if section == 'trending':
            ask.update(sort='TRENDING_DESC')
        elif section in ('season', 'upcoming'):
            season, season_year = self._season() if section == 'season' else self._next_season()
            ask.update(sort='POPULARITY_DESC', season=season, year=season_year)
        elif section in ('popular', 'top', 'genre'):
            ask.update(sort='SCORE_DESC' if section == 'top' else 'POPULARITY_DESC', year=year)
            if section == 'genre':
                ask['genre'] = values.get('genre', 'Action')
        else:
            return fail('invalid_request')
        try:
            found, more = self.catalog.discover(timeout=self._values()['timeout'], **ask)
        except catalog.CatalogError as failed:
            return self._failed(failed)
        if cancelled():
            return fail('cancelled')
        notes = [] if found or page[0] > 1 else [dict(source='anilist', code='no_results', message='AniList lists nothing here for these filters.')]
        return dict(status='ok', items=self._items(found),
                    next='bp:%d' % (page[0] + 1) if more and page[0] < 50 else None, notes=notes)

    # ---- media references (what a thing IS; cinema_media_context) ----------------------------------------------------
    @staticmethod
    def _ref(media, built, episode=None):
        """The reference Cinema carries for an entry or one of its episodes. "anime" is stated by the catalogue (AniList
        lists only anime as type ANIME), never inferred from this extension's name. The work is derived (work.py)."""
        inst = {'anilist': str(media['id'])}
        if media.get('mal'):
            inst['mal'] = str(media['mal'])
        # Only an ESTABLISHED work is an identity (work.py): an unestablished root is never carried, so nothing can follow,
        # bind or continue on it.
        in_work = built is not None and built.get('identity') == 'established' \
            and any(i['ids'].get('anilist') == inst['anilist'] for i in built['installments'])
        kind = 'episode' if episode is not None else 'movie' if media['format'] == 'MOVIE' else 'special' if media['format'] in ('SPECIAL', 'OVA') else 'installment'
        return dict(kind=kind, ids={} if episode is not None else dict(inst), label=media['title'],
                    installment=dict(ids=inst, label=media['title']),
                    work=dict(ids=built['ids'], label=built['label'], provenance=built['provenance']) if in_work else None,
                    # The episode's position in THIS entry's own numbering: the same season its episode list states (0 for an
                    # OVA/special entry, else the entry's stated season, else 1: one AniList entry is one numbering unit). It
                    # used to be the raw stated season (usually none), so a Play had no position and could never be identified
                    # for in-player Information (approved playback UX item 3, 2026-11-17).
                    episode=dict(number=episode, season=0 if media['format'] in ('SPECIAL', 'OVA') else (media.get('season') or 1),
                                 special=media['format'] in ('SPECIAL', 'OVA')) if episode is not None else None,
                    classifications=[dict(name='anime', by='catalog:anilist', confidence='stated')],
                    provenance=[dict(by='catalog:anilist', what='AniList Media %d, type ANIME' % media['id'])])

    def _work(self, media_id, timeout):
        """The continuing series (the franchise's seasons, franchise.build's `work`), or None when it could not be read."""
        try:
            return self._franchise(media_id, timeout)['work']
        except Exception:
            return None

    def lookup(self, request, cancelled):
        """Items for stated ids (anilist or mal): a watchlist entry opening here."""
        timeout = self._values()['timeout']
        ids = request['ids']
        try:
            if 'anilist' in ids and ids['anilist'].isdigit():
                found = self._detail(int(ids['anilist']), timeout)[0]
            elif 'mal' in ids and ids['mal'].isdigit():
                found = self.catalog.by_mal(int(ids['mal']), timeout)
            else:
                return dict(status='ok', items=[], next=None, notes=[dict(code='no_results', message='This extension looks up AniList or MyAnimeList ids only.')])
        except catalog.CatalogError as failed:
            return fail('provider_unavailable' if failed.code in ('unreachable', 'rate_limited') else 'provider_failed')
        if cancelled():
            return fail('cancelled')
        if found is None or ('mal' in ids and 'anilist' in ids and str(found['mal']) != ids['mal']):
            return dict(status='ok', items=[], next=None, notes=[dict(code='no_results', message='AniList states no entry with these ids.')])
        return dict(status='ok', items=[self._title_item(found)], next=None, notes=[])

    def _detail(self, media_id, timeout):
        cached = self.details.get(media_id)
        if cached is None:
            cached = self.catalog.detail(media_id, timeout)
            self.details.put(media_id, cached)
        return cached

    @staticmethod
    def _id(locator, prefix):
        head, _, rest = locator.partition(':')
        if head != prefix:
            return None
        parts = rest.split(':')
        if not all(p.isdigit() and 0 < len(p) <= 9 for p in parts):
            return None
        return [int(p) for p in parts]

    def detail(self, request, cancelled):
        ids = self._id(request['locator'], 'al')
        if not ids or len(ids) != 1:
            return fail('invalid_request')
        try:
            media, aired, parts = self._detail(ids[0], self._values()['timeout'])
        except catalog.CatalogError as failed:
            return fail('provider_unavailable' if failed.code in ('unreachable', 'rate_limited') else 'provider_failed')
        if cancelled():
            return fail('cancelled')
        try:
            umbrella = self._franchise(media['id'], self._values()['timeout'])
        except Exception:
            umbrella = franchise.build(media['id'], {}, [], False, 'unreachable')
        built = umbrella.pop('work')
        if cancelled():
            return fail('cancelled')
        facts = dict(format=media['format'], status=media['status'], year=media['year'], episode_count=media['episodes'],
                     aired_count=aired, season=media['season'], aliases=media['aliases'])
        return dict(status='ok', item=self._title_item(media), facts={k: v for k, v in facts.items() if v is not None},
                    parts=[dict(label=p['title'], relation=p['relation'], locator='al:%d' % p['id'],
                                art=dict(poster=p['cover']) if p.get('cover') else None) for p in parts],
                    episodes='al:%d' % media['id'], media=self._ref(media, built), work=built,
                    # 2026-11-17: the franchise umbrella as DATA (sections of entries, each with its own locator and ids);
                    # Cinema's card template places them. A partial read says so and offers a retry.
                    franchise=umbrella,
                    # 2026-11-16: AniList's stated information and images (data; Cinema's one card draws them).
                    info=self._info(media), art={k: v for k, v in (('poster', media.get('cover')), ('backdrop', media.get('banner'))) if v})

    # ---- episodes ---------------------------------------------------------------------------------------------------
    def episodes(self, request, cancelled):
        ids = self._id(request['locator'], 'al')
        page = self._id(request['next'], 'pg') if request.get('next') else [1]
        if not ids or len(ids) != 1 or not page or len(page) != 1:
            return fail('invalid_request')
        try:
            media, aired, _parts = self._detail(ids[0], self._values()['timeout'])
        except catalog.CatalogError as failed:
            return fail('provider_unavailable' if failed.code in ('unreachable', 'rate_limited') else 'provider_failed')
        if cancelled():
            return fail('cancelled')
        numbers = catalog.episode_numbers(media, aired)
        start = (page[0] - 1) * PAGE
        shown = numbers[start:start + PAGE]
        special = media['format'] in ('SPECIAL', 'OVA')
        season = 0 if special else (media['season'] or 1)
        built = self._work(media['id'], self._values()['timeout']) if shown else None
        # 2026-11-17: each episode's OWN facts (title in the chosen language, air date, overview, still) from ani.zip for
        # exactly this AniList entry and only for the numbers listed here; none of them is ever the show's.
        facts, state = (self.facts.read(media['id'], EPISODE_FACTS_TIMEOUT, numbers=shown) if self.facts is not None and shown else (None, 'none'))
        # 2026-11-19 (user report: a show airing now had no titles or pictures): what AniList's own streamingEpisodes
        # state fills an episode's title or still that ani.zip does not state (ani.zip's own facts come first).
        streamed = episode_meta.streaming(media.get('streaming'))
        try:
            language = controls.display(self.stored() or {}, 'episode_titles')
        except Exception:
            language = 'english'
        items = []
        for n in shown:
            own, more = (facts or {}).get(n) or {}, streamed.get(n) or {}
            named = episode_meta.name(own, language)
            still = own.get('still')
            sources = [episode_meta.SOURCE] if named or still or own.get('aired') or own.get('overview') else []
            if not named and episode_meta.name(more, language):
                named = episode_meta.name(more, language)
                sources.append(episode_meta.STREAMING_SOURCE)
            if not still and more.get('still'):
                still = more['still']
                if episode_meta.STREAMING_SOURCE not in sources:
                    sources.append(episode_meta.STREAMING_SOURCE)
            item = dict(title=named or ('Special %d' if special else 'Episode %d') % n, kind='item', media_type='episode', season=season,
                        episode=n, show=media['title'], source='anilist', locator=dict(browse='ep:%d:%d' % (media['id'], n), play=None),
                        interaction='none', number=n, special=special, aired=True, media=self._ref(media, built, episode=n))
            stated = dict(name=named, still=still, aired_on=own.get('aired'), overview=own.get('overview'))
            item.update({k: v for k, v in stated.items() if v})
            if sources:
                item['facts_source'] = '; '.join(sources)
            items.append(item)
        notes = []
        if state == 'failed':
            notes.append(dict(source='anizip', code='info', message='Episode titles and pictures could not be read just now (ani.zip); '
                                                                     'episodes are listed by number. They are tried again in a few minutes.'))
        if not numbers:
            notes.append(dict(source='anilist', code='no_results', message='AniList does not list any aired episode of this entry yet.'))
        elif media['status'] == 'RELEASING':
            notes.append(dict(source='anilist', code='no_results', message='Still airing: only aired episodes are listed.'))
        return dict(status='ok', items=items, next='pg:%d' % (page[0] + 1) if start + PAGE < len(numbers) else None, notes=notes)

    # ---- sources ----------------------------------------------------------------------------------------------------
    def _gather(self, jobs, cancelled, until):
        """Run reader searches in parallel, bounded by `until`; ({origin: [records]}, {origin: error code}). Late
        answers are dropped (Python cannot stop a running fetch; its result is simply not used)."""
        lock, state = threading.Lock(), dict(open=True, left=len(jobs))
        found, failed = {}, {}
        asked, answered = {}, {}      # per origin: queries asked / answered in time (fixed counts only)
        for origin, _reader, _query in jobs:
            asked[origin] = asked.get(origin, 0) + 1

        def job(origin, reader, query):
            def run():
                try:
                    got, code = reader.search(query, max(1.0, until - self.clock())), None
                except sources.SourceError as error:
                    got, code = None, error.code
                except Exception:
                    got, code = None, 'bad_response'
                with lock:
                    if state['open']:
                        answered[origin] = answered.get(origin, 0) + 1
                        if got is not None:
                            found.setdefault(origin, []).extend(got)
                        elif code:
                            failed.setdefault(origin, code)
                    state['left'] -= 1
            return run
        for origin, reader, query in jobs:
            try:
                self.spawn(job(origin, reader, query))
            except Exception:
                with lock:
                    state['left'] -= 1
                    failed.setdefault(origin, 'unreachable')
        while True:
            with lock:
                if state['left'] <= 0:
                    break
            if cancelled() or self.stopped or self.clock() >= until:
                break
            self.sleep(0.02)
        with lock:
            state['open'] = False
            return {k: list(v) for k, v in found.items()}, dict(failed), state['left'] > 0, (dict(asked), dict(answered))

    def _discover(self, media, episode, aliases, values, cancelled, until, own=None):
        """(rows, notes, jobs, failed, complete): what the sites list for one episode, relevant rows with cached flags.
        rows None: cancelled. complete: every site answered every query in time (only then may it be reused)."""
        finished = media['status'] == 'FINISHED'
        jobs, held_off = [], []
        # A site whose feed address did not resolve a moment ago (2026-11-16: feed.animetosho.org is NXDOMAIN) is not
        # searched again for UNRESOLVED_HOLD: a slow DNS failure would otherwise hold every search's other sites until the
        # time limit. Said, never hidden; tried again after the hold.
        unresolved = getattr(self, 'unresolved', None)
        if unresolved is None:
            unresolved = self.unresolved = {}
        # The module stopping ends its searches too (2026-11-17 modularity audit), not only the consumer's cancel or time.
        stop = lambda: cancelled() or self.stopped or self.clock() >= until
        for origin, reader in self.readers.items():
            if values.get('source_' + origin) != 'on':
                continue
            held = unresolved.get(origin)
            if held is not None and self.clock() - held[0] < held[1]:
                held_off.append((origin, held))
                continue
            if getattr(reader, 'by_id', False):
                # Matched by the index's own id mapping (one bounded job: lookups, then the episode's torrents).
                jobs.append((origin, reader, dict(anilist=media['id'], mal=media.get('mal'), episode=episode, movie=media['format'] == 'MOVIE',
                                                  aliases=list(media['aliases'][:MAX_ALIASES]), stop=stop)))
                continue
            # Episode searches for every name first, then batch searches; at most PER_SITE per site (rate limits).
            asked = [sources.episode_queries(alias, episode, media['season'], finished, media['episodes']) for alias in aliases]
            ordered = [q[0] for q in asked] + [q[1] for q in asked if len(q) > 1]
            for query in list(dict.fromkeys(ordered))[:PER_SITE]:
                jobs.append((origin, reader, query))
        notes = []
        for origin, (since, hold, code) in held_off:
            label = self.readers[origin].label
            left = max(1, int((hold - (self.clock() - since)) // 60) + 1)
            why = ('its documented feed addresses refused a while ago (its operator announced the feed shutdown on 2026-09-16)'
                   if code == 'retired' else 'its feed address did not exist in DNS a few minutes ago')
            notes.append(dict(source=origin, source_label=label, code='provider_unavailable',
                              message='%s was not searched: %s. It is tried again in about %d min; the other sources are listed.' % (label, why, left)))
        if not jobs and not held_off:
            notes.append(dict(code='no_results', message='Every source site is switched off in this search\'s controls.'))
        found, failed, overran, (asked, answered) = self._gather(jobs, cancelled, until) if jobs else ({}, {}, False, ({}, {}))
        if cancelled():
            return None, None, jobs, failed, False
        for origin, code in failed.items():
            if code in ('unresolved', 'retired'):
                unresolved[origin] = (self.clock(), RETIRED_HOLD if code == 'retired' else UNRESOLVED_HOLD, code)
        for origin, code in sorted(failed.items()):
            label = self.readers[origin].label
            notes.append(dict(source=origin, source_label=label, code=NOTE_CODE.get(code, 'provider_failed'), message=NOTE_TEXT.get(code, '%s failed.') % label))
        if overran:
            # Privacy-safe fixed counts (no query, title or URL): which site's searches the limit cut, so a thin first
            # list can be told apart from filters (user report, 2026-11-16).
            counts = ', '.join('%s answered %d of %d' % (self.readers[o].label, answered.get(o, 0), n) for o, n in sorted(asked.items()))
            notes.append(dict(code='timed_out', message='Some searches were still running at the %d-second time limit; their answers are '
                                                        'not included (%s). Search again, or raise the time limit under Filters.' % (values['timeout'], counts)))
        records = {}
        season = media['season'] or 1
        for origin in self.readers:
            for record in found.get(origin, []):
                # A text search's record must name the title and the episode, and the entry's season. A record an index
                # matched by id (nekoBT, Torrentio) is that index's CLAIM (2026-11-17: Torrentio answered the 2012 Gates to
                # Infinity shorts with the 2006 "Pokemon Mystery Dungeon 1" special): it is kept, but "named" only when its
                # release title or the file it names names this work; otherwise "unverified", listed last and never played
                # by itself. Never hidden: a correct release with an unusual name stays choosable.
                if record.get('matched') == 'id':
                    named = sources.names_work([record['title'], record.get('file')], own or media['aliases'], media['season'])
                    record = dict(record, identity='named' if named else 'unverified')
                elif sources.relevant(record, own or media['aliases'], episode) and release.season_of(record['title']) in (None, season):
                    record = dict(record, identity='named')
                else:
                    continue
                kept = records.get(record['hash'])
                # one torrent from several indexes: a NAMED claim wins over an unverified one, then the most seeders
                if kept is None or (record['identity'], -(record['seeders'] or 0)) < (kept['identity'], -(kept['seeders'] or 0)):
                    records[record['hash']] = record
        rows = [self._row(r) for r in records.values()]
        for row in rows:
            row['_count'] = row['range'][1] - row['range'][0] + 1 if row['range'] else (media['episodes'] or 1) if row['pack'] else 1
        cached_notes = self._check_cached(rows, cancelled, until)
        notes.extend(cached_notes)
        if cancelled():
            return None, None, jobs, failed, False
        complete = bool(jobs) and not held_off and not failed and not overran and not any(n.get('code') in ('timed_out', 'provider_failed', 'provider_unavailable') for n in cached_notes)
        return rows, notes, jobs, failed, complete

    def sources(self, request, cancelled):
        ids = self._id(request['locator'], 'ep')
        if not ids or len(ids) != 2:
            return fail('invalid_request')
        media_id, episode = ids
        values = self._values(request.get('controls'))
        try:
            media, aired, _parts = self._detail(media_id, values['timeout'])
        except catalog.CatalogError:
            return fail('provider_unavailable', 'AniList could not be reached for this title.')
        # The sites' time limit starts AFTER the title's details are read (2026-11-16, user report "sources empty until I
        # changed a filter"): it used to start before, so an uncached or slow AniList read on the FIRST search of an
        # episode used up the sites' time and their answers were dropped at the limit; any second search (any filter
        # change) found the details cached and got the whole limit.
        until = self.clock() + values['timeout']
        if episode not in catalog.episode_numbers(media, aired):
            return fail('invalid_request', 'This entry has no such episode.')
        # The entry's OWN names (2026-11-17): a synonym that is also a related entry's title names the franchise, not this
        # work, and is neither searched nor accepted as naming it.
        own = sources.own_names(media['aliases'], [p.get('title') for p in _parts or []])
        aliases = own[:MAX_ALIASES]
        base = next((sources.base_name(a) for a in aliases if sources.base_name(a)), None)
        if base and base not in aliases:
            aliases = aliases + [base]          # "X Season 2" is searched as "X" too (its S02E05 query names the season)
        # One episode's discovery, kept briefly (Codex R4): what the sites listed (and which were cached) depends only on the
        # acquisition choices (sites, time limit); every other control (resolution, audio, codec, HDR, packs, cached,
        # size, release title, order) filters the SAME gathered rows. A complete discovery is reused for SNAPSHOT_TTL so a
        # filter change compares like with like; an incomplete one (a site failed or the limit cut it) is never reused,
        # so searching again really searches again. Memory only, bounded, per session; no links are kept.
        acquire = (media_id, episode, values['timeout']) + tuple(values.get('source_' + origin) for origin in sorted(self.readers))
        held = self.snapshots.get(acquire)
        # Showing only matching sources again, or the relaxed list again (2026-11-17, "reuse"): a VIEW of what this search
        # already acquired, even an incomplete one (kept briefly for exactly this), never a new fetch.
        if held is None and request.get('reuse') is True:
            held = self.recent.get(acquire)
        if held is not None and self.clock() - held['at'] <= SNAPSHOT_TTL:
            rows, notes, jobs, failed = [dict(r) for r in held['rows']], list(held['notes']), held['jobs'], {}
            notes.append(dict(code='info', message='Filtered from this episode\'s search %d s ago. To search the sites again, change '
                                                   'Where to search or the time limit under Filters, or reopen this episode later.' % int(self.clock() - held['at'])))
        else:
            rows, notes, jobs, failed, complete = self._discover(media, episode, aliases, values, cancelled, until, own=own)
            if rows is None:
                return fail('cancelled')
            kept = dict(at=self.clock(), rows=[dict(r) for r in rows], notes=list(notes), jobs=len(jobs))
            if complete:
                self.snapshots.put(acquire, kept)
            else:
                self.recent.put(acquire, kept)
        original = None
        if values['audio'] == 'sub':        # an inference from a stated country, said as one on every search that uses it
            original, said = controls.inferred(media.get('country'))
            notes.append(dict(code='no_results' if original is None else 'info', message=said))
        shown = _named_first(controls.order([r for r in rows if controls.keep(r, values, original)], values))
        fallback = None
        if rows and not shown:
            # Which controls left them out, each with its value in force (a source may be left out by several), so the
            # viewer changes the right one under Filters instead of guessing (user report, alpha aq).
            counted = sorted(controls.left_out(rows, values, original).items(), key=lambda kv: -kv[1])
            relaxed = [dict(key=k, label=controls.said(k, values), count=n) for k, n in counted]
            if request.get('strict') is True:
                why = '; '.join('%s leaves out %d' % (r['label'], r['count']) for r in relaxed[:3])   # the note stays short (300)
                notes.append(dict(code='no_results', message='%d sources were found; none passes this search\'s controls (%s). '
                                                             'Change them under Filters.' % (len(rows), why)))
                fallback = dict(active=False, available=len(rows), relaxed=relaxed[:8])
            else:
                # Zero-match fallback (2026-11-17, user request): the sources THIS search already acquired are shown, with
                # which preferences were relaxed and, per row, which of them it does not meet. Nothing is fetched again,
                # nothing is saved, nothing plays by itself; the episode, the title and playability stay as they are (they
                # are what made these rows), and "Show only matching sources" restores the strict list (strict=True).
                # Order: the chosen sort; "Cached only" relaxed still puts cached sources first.
                shown = _named_first(controls.order(rows, dict(values, cached='first') if values['cached'] == 'only' else values))
                for row in shown:
                    row['mismatch'] = [dict(key=k, label=controls.said(k, values)) for k in controls.rejections(row, values, original)]
                fallback = dict(active=True, message=FALLBACK_TEXT, available=len(rows), relaxed=relaxed[:8])
        elif not rows and jobs and not failed:
            notes.append(dict(code='no_results', message='No release for episode %d was found.' % episode))
        out = []
        # What resolve needs to find THIS episode's file in a pack (2026-11-17): the entry's own numbering facts, and per
        # source the season its release title names and the file an index named for this episode.
        scope = self._scope(media, episode, _parts)
        scope['names'] = own[:8]
        for row in shown[:200]:
            token = 'src:' + secrets.token_hex(8)
            row.pop('_count', None)
            self.tokens.put(token, dict(scope, hash=row.pop('_hash'), hint=row.pop('_file', None), episode=episode, media=media_id,
                                        pack=row['pack'], pack_season=release.season_of(row['release']),
                                        identity=(row.get('identity') or {}).get('state') or 'named', release=row['release']))
            row['locator'] = token
            out.append(row)
        answer = dict(status='ok', sources=out, applied=values, notes=notes)
        if fallback:
            answer['fallback'] = fallback
        return answer

    @staticmethod
    def _row(record):
        title = record['title']
        found = release.episodes_of(title)
        # Languages: the release title's, plus what an index STATES in its own fields (nekoBT's audio/subtitle languages).
        audio = list(dict.fromkeys(release.audio_languages(title) + list(record.get('audio') or [])))[:8]
        subtitles = list(dict.fromkeys(release.subtitle_languages(title) + list(record.get('subtitles') or [])))[:8]
        state = record.get('identity') or 'named'
        identity = dict(state=state, message=None if state == 'named' else
                        'Not verified: %s lists it for this title by id, but its name does not name this title. Cinema will not play '
                        'it by itself.' % ORIGIN_LABEL.get(record['origin'], record['origin']))
        return dict(_hash=record['hash'], _file=record.get('file'), identity=identity, locator=None, label=title, kind='torrent', origin=record['origin'], quality=release.quality(title),
                    size=record['size'], seeders=record['seeders'], release=title, tags=release.tags(title),
                    audio=audio, subtitles=subtitles, pack=release.is_pack(title) or record.get('pack') is True,
                    range=list(found) if found else None, cached=None, via=None,
                    # The index's own name for the chooser (display only, 2026-11-17); Torrentio's upstream site stays
                    # unstated (it is the index's claim, not a fact this extension checked).
                    origin_label=ORIGIN_LABEL.get(record['origin'], record['origin']))

    def _check_cached(self, rows, cancelled, until):
        """Mark each row cached True/False through Accounts (known only with a connected debrid provider)."""
        if not rows:
            return []
        accounts, provider = self.debrid.provider()
        if accounts is None:
            return [dict(code='needs_connection', message='Cached or not is unknown: connect a debrid service in Accounts & Services.'
                         if provider == 'not_connected' else 'Cached or not is unknown: Accounts & Services is off.')]
        # 2026-11-17 (Codex AZ review: 77 sources all "unknown", one resolve worked). The check has its OWN bounded time
        # (not what the source search left over), each chunk's answer is kept as it arrives (a later chunk failing never
        # discards an earlier one), a chunk refused as busy (the previous request still holds Accounts' slot) is asked
        # again within that time, and the note says exactly how many are known and why the rest are not. A refusal is
        # never read as "not cached"; nothing reconnects or changes the account.
        hashes = [r['_hash'] for r in rows][:CHECK_MAX]
        known, failures = {}, []
        deadline = self.clock() + CHECK_BUDGET
        for start in range(0, len(hashes), CHECK_CHUNK):
            chunk = hashes[start:start + CHECK_CHUNK]
            while True:
                why = {}
                outcome, value = self.debrid.ask(accounts, provider, 'debrid.availability', dict(hashes=chunk), cancelled,
                                                 min(deadline, self.clock() + CHECK_CHUNK_TIME), why=why)
                if outcome == 'refused' and value == 'busy' and self.clock() + 1.0 < deadline and not cancelled():
                    self.sleep(0.5)
                    continue
                break
            if outcome == 'done':
                known.update(value['available'])
                for row in rows:
                    if row['_hash'] in value['available']:
                        row['cached'], row['via'] = value['available'][row['_hash']], provider
                continue
            if outcome == 'cancelled':
                return []
            reason = why.get('detail') or ('timed_out' if outcome == 'timed_out' else value)
            failures.append((len(chunk), CHECK_WHY.get(reason) or CHECK_WHY[None]))
            if why.get('detail') in ('authentication_failed', 'permission_denied', 'not_connected', 'needs_attention') or self.clock() >= deadline:
                failures.append((len(hashes) - start - len(chunk), None))   # the rest is not asked: it would fail the same way
                break
        unchecked = sum(n for n, _ in failures) + max(0, len(rows) - len(hashes))
        if not unchecked:
            return []
        reasons = '; '.join(dict.fromkeys(r for _, r in failures if r)) or CHECK_WHY[None]
        label = (getattr(self.debrid, 'labels', None) or {}).get(provider) or provider
        if known:
            message = 'Cached or not is known for %d of %d sources; for the other %d the check through %s did not complete (%s).' % (
                len(known), len(rows), unchecked, label, reasons)
        else:
            message = 'Cached or not is unknown: the check through %s did not complete (%s).' % (label, reasons)
        return [dict(code='provider_failed', message=message)]

    # ---- which file of a pack (2026-11-17) -------------------------------------------------------------------------
    def _scope(self, media, episode, parts):
        """This entry's numbering as resolve states it to Cinema's file selector: its season, its count, whether it is a
        special, whether its own numbers ARE its season's (not a later part/cour), whether it is the series' first entry
        (its numbers then ARE absolute ones), and the episode's stated season-local and absolute numbers (ani.zip)."""
        special = media['format'] in ('SPECIAL', 'OVA')
        names = [media['title']] + list(media.get('aliases') or [])
        later_part = any(re.search(r'(?<![a-z0-9])(?:part|cour)[ ._-]?(?:[2-9]|ii)(?![0-9a-z])', n or '', re.I) for n in names)
        first = bool(parts) and not any(p.get('relation_type') == 'PREQUEL' for p in parts) and not special
        facts = None
        if self.facts is not None:
            got, _state = self.facts.read(media['id'], EPISODE_FACTS_TIMEOUT)
            facts = (got or {}).get(episode)
        return dict(special=special, count=media['episodes'], title=media['title'], first=first, later_part=later_part,
                    season=media['season'] or (1 if first else None), numbers=dict(facts) if facts else None)

    @staticmethod
    def _target(held):
        """resolve's `target` (cinema.media-provider 1.1): the episode asked for, in every numbering ESTABLISHED for it."""
        episode, facts = held['episode'], held.get('numbers') or {}
        forms = []
        if facts.get('season') is not None and facts.get('number') is not None:
            forms.append(dict(scheme='season', season=facts['season'], episode=facts['number']))
        if facts.get('absolute') and not held['special']:
            forms.append(dict(scheme='absolute', episode=facts['absolute']))
        if held.get('first') and not facts.get('absolute'):
            forms.append(dict(scheme='absolute', episode=episode))       # the series' first entry: its numbers are absolute
        season = facts['season'] if facts.get('season') else held.get('season')
        season_local = (facts.get('number') == episode) if facts.get('number') is not None else (held.get('season') is not None and not held.get('later_part'))
        if season_local and held.get('season') is not None and not any(f['scheme'] == 'season' for f in forms) and not held['special']:
            forms.append(dict(scheme='season', season=held['season'], episode=episode))
        word = 'Special' if held['special'] else 'Episode'
        return dict(episode=episode, season=season if not held['special'] else 0, count=held.get('count'), special=held['special'],
                    single=not held['pack'], pack_season=held.get('pack_season'), season_local=bool(season_local), forms=forms,
                    # 2026-11-17: whether the SOURCE names this work (an id claim alone is "unverified": its hint is not
                    # trusted and nothing plays by itself), and the work's own names (files naming it are matched first)
                    hint=held.get('hint') if held.get('identity') != 'unverified' else None,
                    identity=held.get('identity') or 'named', names=list(held.get('names') or [])[:8],
                    label=('%s %d of %s' % (word, episode, held.get('title') or ''))[:120].strip())

    # ---- resolve ----------------------------------------------------------------------------------------------------
    def resolve(self, request, cancelled):
        locator = request['locator']
        held = self.tokens.get(locator)
        if locator.startswith('file:'):
            if held is None or 'link' not in held:
                return fail('locator_expired')
            return dict(status='ok', stream=dict(url=held['link'], label=held['name'], mime=None))
        if not locator.startswith('src:'):
            return fail('invalid_request')
        if held is None or 'hash' not in held:
            return fail('locator_expired')
        accounts, provider = self.debrid.provider()
        if accounts is None:
            return fail('needs_connection', 'Playing a torrent source needs a debrid service connected in Accounts & Services.'
                        if provider == 'not_connected' else 'Playing a torrent source needs Accounts & Services turned on, with a debrid service connected.')
        outcome, value = self.debrid.ask(accounts, provider, 'debrid.resolve', dict(hash=held['hash']), cancelled, self.clock() + 30.0)
        if outcome == 'cancelled':
            return fail('cancelled')
        if outcome == 'timed_out':
            return fail('timed_out')
        if outcome != 'done':
            if value == 'busy':
                return fail('busy')
            # Told apart (2026-11-17): the service has no file list for it (not cached / unknown there) vs. anything else.
            return fail('provider_failed', 'Your debrid service has no file list for this source (it is probably not cached there). '
                                           'Choose another source, preferably one marked Cached.' if value == 'not_found' else
                        'Your debrid service could not give this source\'s files (it may not be cached there, or the service is '
                        'busy). Try again, or choose another source marked Cached.')
        # Every file of the source, videos first (a bound of 500), with the container its LINK names when its path names
        # none. Which file is the episode is NOT decided here: Cinema's one file selector does it, from these names and
        # the target below (cinema_episode_files; contract 1.1 resolve `target`). `match` stays as this provider's own
        # reading for consumers that do not select.
        listed = []
        for entry in value['files']:
            container = release.link_container(entry['link']) if not release.is_video(entry['path']) else None
            listed.append((not (release.is_video(entry['path']) or container), entry['path'].lower(), entry, container))
        out = []
        for _, _, entry, container in sorted(listed, key=lambda f: (f[0], f[1]))[:500]:
            number = release.file_episode(entry['path'])
            token = 'file:' + secrets.token_hex(8)
            self.tokens.put(token, dict(link=entry['link'], name=entry['path'].replace('\\', '/').rsplit('/', 1)[-1]))
            item = dict(locator=token, path=entry['path'], size=entry['size'], match='exact' if number == held['episode'] else 'none', episode=number)
            if container:
                item['container'] = container
            out.append(item)
        return dict(status='ok', files=out, target=self._target(held))
