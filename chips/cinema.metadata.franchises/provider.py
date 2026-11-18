"""Cross-media franchises for Cinema (optional metadata plugin). Data from Wikidata (CC0).

UNIVERSAL: no franchise, title, alias or membership is named anywhere in this plugin or its data. A franchise is
whatever Wikidata's relations connect, found from the viewer's own words and traversed the same way for every franchise:

  groups({"text"})  Wikidata's entity search for the text gives SEED items (at most five). Each seed's own relations
                    give ROOTS: itself, its media franchise (P8345), the series it is part of (P179), what it is based on
                    (P144), and the works it follows or is followed by (P155, P156). A
                    candidate is offered only when at least one film or series with a TMDB id states a relation to a root
                    (one counting search). Its title is the franchise item's label, else the source work's, else the
                    seed's: whatever the data names. ref {"kind": "franchise", "key": "<seed ids>"}.
  group({"ref"})    The same roots, then the SERIES and FRANCHISE items that state a relation to a root (film series,
                    novel series, series of works, media franchises) join them; then the MEMBERS: films and series with a
                    TMDB id (P4947 film, P4983 series) that state based on (P144), part of the series (P179) or media
                    franchise (P8345) with any root. Each member says how it is linked (its series, else what it is based
                    on) and its form; the number of ALL linked works (books, games, ...) is reported.

Nothing here decides a viewing order (members are sorted by release date, said as a sorting), merges continuities or
claims completeness: Wikidata states what its editors linked; a relation it lacks is a coverage gap the card states.

Requests (1.1.0, 2026-11-18 reliability correction):
- HTTPS GET to www.wikidata.org only, through Cinema's fetch (same origin), with this plugin's own User-Agent: its name,
  its packaged version and the maintainer's public contact (Wikimedia's User-Agent policy). Nothing about the viewer.
- ONE shared, serial gate for every search, open and refresh of this plugin: one request in flight, request starts at
  most every 0.4 s (Settings "Request pace": 150 a minute, or 60), below Wikimedia's published 200 a minute for an
  identified client without an account. A card the viewer opened goes before a search, a search before a refresh; a
  search the viewer replaced by another one stops waiting. Every wait is bounded by its call's own deadline.
- HTTP 429 or 503, or a JSON maxlag / ratelimited error: nothing is asked again until the cooldown ends, for every
  caller and across a restart. A stated Retry-After (seconds or an HTTP date) is kept IN FULL; without one, 5 s, doubled
  per consecutive throttle (at most 300 s). A call that can wait within its deadline waits and asks again (twice at
  most); else it fails with the remaining time ("could not be read just now"), never "no franchise".
- groups at most 6 requests, group at most 16; claims are read five items at a time (a film's item carries hundreds of
  claims and Cinema's fetch is bounded at 2 MB); labels fifty at a time.

Lifetime (1.1.1, Codex BM review): each use_data() call starts a new ACTIVATION and withdraws the previous one. A call
works only through the activation it started under. Once that is withdrawn (turned off, on again, removed), the call
makes no further request, applies no answer (also no throttle), writes nothing anywhere, and returns no answer. No call
of a newer activation joins its work. A request already on the wire is not killed: it finishes, is counted
(STATS withdrawn), and its answer is unused. The gate stays shared, so requests stay serial across activations.

Data kept (Cinema's per-plugin data store, "data" in the manifest; in memory only without one): the facts this plugin
uses of each item it read (label, the relations above, dates, TMDB ids, revision, when read; never whole responses), its
searches' answers, and its last good answers. Facts are reused for a week, searches and search answers for a day, a
franchise card for six hours; afterwards the last good answer (at most 30 days old) is shown at once, said as such, while
it is read again in the background. Only an answer Wikidata gave is kept: "no franchise" only when the data says so, and
a throttled, failed or partial read is never stored as an answer. No key, no account."""
import email.utils
import json
import math
import os
import re
import threading
import time

API = 'https://www.wikidata.org/w/api.php'
CONTACT = 'https://github.com/rocketburger'       # the maintainer's public profile (a reachable contact)


def _packaged_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'manifest.json'), 'r', encoding='utf-8') as stream:
            value = json.load(stream).get('version')
        return value if isinstance(value, str) and re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}', value) else '0'
    except (OSError, ValueError, NameError):
        return '0'


VERSION = _packaged_version()
USER_AGENT = 'Sandwich-Franchises/%s (%s)' % (VERSION, CONTACT)
HEADERS = {'User-Agent': USER_AGENT}

QID = re.compile(r'Q[1-9]\d{0,10}')
KEY = re.compile(r'q[1-9]\d{0,10}(\.q[1-9]\d{0,10}){0,4}')
SEEDS, MAX_ROOTS, MAX_MEMBERS, BATCH = 5, 32, 40, 5
UP = ('P8345', 'P179', 'P144', 'P155', 'P156')      # a seed's own relations: its roots ('part of', P361, drifts: not used)
WORK = ('P577', 'P50', 'P57')    # a seed counts only when it is a WORK: it states a publication date, an author or a director
DOWN = ('P144', 'P179', 'P8345')                           # what a member states about a root
KEPT = tuple(sorted(set(UP + WORK + DOWN + ('P31', 'P4947', 'P4983'))))   # the only claims this plugin keeps of an item
SERIES_CLASSES = ('Q24856', 'Q7725310', 'Q1667921', 'Q196600', 'Q20937557')   # film series, series of works, novel series, media franchise
# A small ontology vocabulary (classes of works), never titles: what kind of adaptation a member is.
FORMS = {'Q202866': 'animated film', 'Q506240': 'television film', 'Q1259759': 'miniseries', 'Q336144': 'fan film',
         'Q2297927': 'parody film', 'Q581714': 'animated series', 'Q117467246': 'animated series', 'Q93204': 'short film'}

OPEN, SEARCH, REFRESH = 0, 1, 2                      # gate priority: a card the viewer opened first
INTERVALS = {'standard': 0.4, 'gentle': 1.0}         # seconds between request starts (150 or 60 a minute)
BACKOFF, BACKOFF_MAX, RETRIES = 5.0, 300.0, 2        # without a stated Retry-After (our own guess only)
FRESH = {'entity': 7 * 86400, 'query': 3600, 'groups': 86400, 'group': 21600}   # searches: an hour (a refresh asks again)
KEEP = 30 * 86400                                    # the oldest last-good answer still shown (said as such)
LIMITS = {'entities': 4000, 'queries': 600, 'answers': 300}
SCHEMA = 1
STALE_SECONDS = 120                                  # how long Cinema keeps a last-good answer before asking again
_mono, _wall = time.monotonic, time.time             # tests substitute clocks
STATS = dict(requests=0, withdrawn=0, throttled=0, answer_hits=0, entity_hits=0, query_hits=0, coalesced=0, stale=0, refreshed=0, errors={})


class RateLimited(Exception):
    """Wikidata asked for a pause. wait: the full remaining seconds; retry_after: the registry's bounded copy."""
    def __init__(self, seconds):
        super().__init__('rate limited')
        self.wait = max(1.0, float(seconds))
        self.retry_after = int(min(3600, max(1, math.ceil(self.wait))))


class Withdrawn(Exception):
    """The plugin was turned off or removed (its activation withdrawn) while this call ran: no answer from it."""
    retry_after = 1


class BudgetExceeded(Exception):
    retry_after = 60


class Superseded(Exception):
    """A search the viewer replaced by another one: it stops waiting (no request is made for it)."""


class ServiceError(Exception):
    """An answer that is an error (not throttling), or a Cinema too old for a fixed-origin fetch."""
    retry_after = 60


def retry_seconds(value, now):
    """A Retry-After header's full delay: delta-seconds or an HTTP date (RFC 9110). None: absent or unreadable."""
    if value is None:
        return None
    value = str(value).strip()
    if value.isdigit():
        return float(int(value))
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        import datetime
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(1.0, when.timestamp() - now)


class Gate(object):
    """The plugin's one gate to Wikidata: one request in flight, a minimum gap between request starts, a shared
    cooldown (wall clock, so it is kept across a restart), and waiting callers served by priority, then arrival."""

    def __init__(self):
        self.cond = threading.Condition()
        self.busy, self.next_at, self.cooldown_until, self.strikes = False, 0.0, 0.0, 0
        self.waiting, self.seq = [], 0

    def acquire(self, priority, deadline, cancelled=None, kept=None):
        with self.cond:
            self.seq += 1
            ticket = (priority, self.seq)
            self.waiting.append(ticket)
            try:
                while True:
                    if kept is not None and not kept.alive:
                        raise Withdrawn()                # its activation ended while it waited: no request
                    if cancelled is not None and cancelled():
                        raise Superseded()
                    now, cool = _mono(), self.cooldown_until - _wall()
                    if cool > 0 and now + cool > deadline:
                        raise RateLimited(cool)          # it would end after this call's deadline: no waiting for nothing
                    if now >= deadline:
                        raise BudgetExceeded()
                    if not self.busy and cool <= 0 and now >= self.next_at and min(self.waiting) == ticket:
                        self.busy = True
                        return
                    self.cond.wait(min(0.25, max(0.01, deadline - now), max(0.01, cool, self.next_at - now)))
            finally:
                self.waiting.remove(ticket)
                self.cond.notify_all()

    def release(self, interval):
        with self.cond:
            self.busy, self.next_at = False, _mono() + interval
            self.cond.notify_all()

    def answered(self):
        with self.cond:
            self.strikes = 0

    def throttle(self, stated, kept=None):
        """Start (or lengthen) the shared cooldown: a stated delay in full, else our backoff. Returns its seconds. The
        cooldown is kept (across a restart) in the calling activation's data, only while that activation is alive."""
        with self.cond:
            seconds = stated if stated is not None else min(BACKOFF_MAX, BACKOFF * (2 ** self.strikes))
            self.strikes += 1
            self.cooldown_until = max(self.cooldown_until, _wall() + seconds)
            until = self.cooldown_until
            self.cond.notify_all()
        STATS['throttled'] += 1
        if kept is not None:
            kept.put_cooldown(until)
        return until - _wall()


GATE = Gate()


class Facts(object):
    """ONE ACTIVATION of this plugin and what it keeps: compact item facts, search answers and last-good answers, each
    with when it was read. In memory, and in Cinema's data store when given one (use_data). Bounded by LIMITS.

    Every call captures the activation current when it starts and works only through it. use_data() withdraws the old
    activation (alive False) before a new one starts: from then on the old one writes nothing (memory, store or
    cooldown), its calls make no new request, an answer arriving for one is discarded (Withdrawn), and no new call
    joins its work. A request already on the wire is not killed; it is counted (STATS withdrawn) and its answer unused."""
    _ids = [0]

    def __init__(self, store=None):
        self.lock = threading.RLock()
        self.store, self.dirty, self.alive = None, set(), True
        Facts._ids[0] += 1
        self.id = Facts._ids[0]
        self.items = {name: {} for name in LIMITS}
        if store is not None:
            self.attach(store)

    def withdraw(self):
        with self.lock:
            self.alive, self.store, self.dirty = False, None, set()

    def attach(self, store):
        with self.lock:
            self.store, self.dirty = store, set()
            self.items = {name: {} for name in LIMITS}
            for name in LIMITS:
                blob = store.get(name) if store is not None else None
                if isinstance(blob, dict) and blob.get('schema') == SCHEMA and isinstance(blob.get('items'), dict):
                    self.items[name] = blob['items']
            state = store.get('state') if store is not None else None
            until = state.get('cooldown_until') if isinstance(state, dict) else None
        if isinstance(until, (int, float)) and until > _wall():
            with GATE.cond:
                GATE.cooldown_until = max(GATE.cooldown_until, float(until))

    def _get(self, name, key, fresh):
        with self.lock:
            entry = self.items[name].get(key)
        if isinstance(entry, dict) and isinstance(entry.get('t'), (int, float)) and _wall() - entry['t'] < fresh:
            return entry
        return None

    def _put(self, name, key, entry):
        with self.lock:
            if not self.alive:
                return                       # a withdrawn activation keeps nothing
            entry['t'] = _wall()
            self.items[name][key] = entry
            self.dirty.add(name)

    def entity(self, qid, claims):
        entry = self._get('entities', qid, FRESH['entity'])
        if entry is None or (claims and entry.get('c') is None and not entry.get('m')):
            return None
        return entry

    def put_entity(self, qid, raw, claims):
        if not isinstance(raw, dict) or 'missing' in raw:
            self._put('entities', qid, dict(m=1))
            return
        if not claims:
            held = self.entity(qid, True)
            if held is not None:
                return                       # an item already kept with its claims (and label)
        kept = None
        if claims:
            kept = {}
            for prop in KEPT:
                values = [_compact(_value(c)) for c in (raw.get('claims') or {}).get(prop) or []]
                values = [v for v in values if v is not None]
                if values:
                    kept[prop] = values[:50]
        self._put('entities', qid, dict(l=_label(raw), c=kept, r=raw.get('lastrevid') if type(raw.get('lastrevid')) is int else None))

    def query(self, key):
        entry = self._get('queries', key, FRESH['query'])
        return entry.get('v') if entry is not None else None

    def put_query(self, key, value):
        self._put('queries', key, dict(v=value))

    def answer(self, key):
        """(value, age seconds) of a kept answer at most KEEP old, else None."""
        entry = self._get('answers', key, KEEP)
        return (entry['v'], _wall() - entry['t']) if entry is not None and isinstance(entry.get('v'), dict) else None

    def put_answer(self, key, value):
        self._put('answers', key, dict(v=value))

    def put_cooldown(self, until):
        with self.lock:                      # held while writing: withdraw() cannot pass a write in progress
            if self.alive and self.store is not None:
                self.store.put('state', dict(schema=SCHEMA, cooldown_until=until))

    def flush(self):
        with self.lock:
            if not self.alive:
                return
            store, names, self.dirty = self.store, sorted(self.dirty), set()
            blobs = {}
            for name in names:
                items = self.items[name]
                old = _wall() - KEEP
                for key in [k for k, v in items.items() if not isinstance(v, dict) or v.get('t', 0) < old]:
                    del items[key]
                if len(items) > LIMITS[name]:
                    for key in sorted(items, key=lambda k: items[k].get('t', 0))[:len(items) - LIMITS[name]]:
                        del items[key]
                blobs[name] = dict(schema=SCHEMA, source='www.wikidata.org', items=dict(items))
            if store is not None:                # still under the lock: a withdrawal waits for the write, then wins
                for name, blob in blobs.items():
                    try:
                        if not store.put(name, blob):
                            self.dirty.add(name)      # written at the next flush
                    except Exception:
                        pass                          # keeping facts never breaks an answer


_state = dict(current=Facts(), latest_search=None)    # the current activation (in memory until use_data)
_inflight, _refreshing, _inflight_lock = {}, set(), threading.Lock()


def current():
    return _state['current']


def use_data(store):
    """Cinema's per-plugin data store, or None when the plugin is turned off or removed. Either way the previous
    activation is withdrawn first (see Facts); a store starts a new activation over its kept files."""
    old = _state['current']
    old.withdraw()
    _state['current'] = Facts(store)


class Client(object):
    """One call's requests: its own count and deadline, through the shared gate."""

    def __init__(self, fetch, limit, budget, priority, interval, cancelled=None, kept=None):
        self.fetch, self.limit, self.requests = fetch, limit, 0
        self.kept = kept if kept is not None else current()      # the activation this call belongs to
        self.deadline, self.priority, self.interval, self.cancelled = _mono() + budget, priority, interval, cancelled

    def get(self, **params):
        params['format'] = 'json'
        attempts = 0
        while True:
            if self.requests >= self.limit or _mono() > self.deadline:
                raise BudgetExceeded()
            GATE.acquire(self.priority, self.deadline, self.cancelled, self.kept)
            self.requests += 1
            STATS['requests'] += 1
            data, throttled, stated, failure = None, False, None, None
            try:
                data = self.fetch(API, dict(params), dict(HEADERS), same_origin=True)
            except TypeError:
                failure = ServiceError('this Cinema has no fixed-origin fetch')
            except Exception as exc:
                failure = exc
            finally:
                GATE.release(self.interval)
            if not self.kept.alive:
                STATS['withdrawn'] += 1        # on the wire when its activation ended: counted, its answer unused
                raise Withdrawn()
            if failure is not None:
                exc = failure
                status = getattr(exc, 'code', None)
                key = 'http-%d' % status if type(status) is int else type(exc).__name__[:40]
                STATS['errors'][key] = STATS['errors'].get(key, 0) + 1
                if status not in (429, 503):
                    raise exc
                throttled = True
                headers = getattr(exc, 'headers', None)
                stated = retry_seconds(headers.get('Retry-After') if headers is not None else None, _wall())
            error = data.get('error') if isinstance(data, dict) else None
            if isinstance(error, dict):
                code = re.sub(r'[^a-z0-9_-]', '', str(error.get('code')).lower())[:60] or 'unknown'
                STATS['errors'][code] = STATS['errors'].get(code, 0) + 1          # codes only, for diagnosis
                if code not in ('maxlag', 'ratelimited'):
                    raise ServiceError(code)
                throttled = True
            if not throttled:
                GATE.answered()
                return data
            seconds = GATE.throttle(stated, self.kept)
            attempts += 1
            if attempts > RETRIES or self.priority == REFRESH or _mono() + seconds > self.deadline:
                raise RateLimited(seconds)
            # else: acquire() waits out the cooldown (within this call's deadline) and the request is made again

    def cached(self, key, **params):
        held = self.kept.query(key)
        if held is not None:
            STATS['query_hits'] += 1
            return held
        data = self.get(**params)
        if not isinstance(data, dict):
            raise ServiceError('not an answer')
        return data

    def facts(self, ids, claims):
        """{qid: compact facts} for these items: kept ones reused, the others read (claims five at a time)."""
        need = [q for q in ids if self.kept.entity(q, claims) is None]
        STATS['entity_hits'] += len(ids) - len(need)
        size = BATCH if claims else 50
        for i in range(0, len(need), size):
            chunk = need[i:i + size]
            data = self.get(action='wbgetentities', ids='|'.join(chunk), props='labels|claims|info' if claims else 'labels|info',
                            languages='en|mul')
            if not isinstance(data, dict):
                raise ServiceError('not an answer')
            entities = data.get('entities') or {}
            for q in chunk:
                self.kept.put_entity(q, entities.get(q), claims)
        return {q: self.kept.entity(q, False) for q in ids}

    def search(self, query, limit, total=False):
        key = json.dumps(['search', query, limit, total])
        held = self.kept.query(key)
        if held is not None:
            STATS['query_hits'] += 1
            return held[0], held[1]
        params = dict(action='query', list='search', srsearch=query, srlimit=limit, srnamespace=0)
        if total:
            params['srinfo'] = 'totalhits'
        data = self.get(**params)
        if not isinstance(data, dict) or not isinstance(data.get('query'), dict):
            STATS['errors']['no-search-answer'] = STATS['errors'].get('no-search-answer', 0) + 1
            raise ServiceError('not a search answer')
        found = data['query']
        hits = [h.get('title') for h in found.get('search') or [] if isinstance(h, dict) and QID.fullmatch(str(h.get('title')))]
        count = (found.get('searchinfo') or {}).get('totalhits')
        self.kept.put_query(key, [hits, count if type(count) is int else None])
        return hits, count if type(count) is int else None


def _value(claim):
    v = (claim.get('mainsnak') or {}).get('datavalue', {}).get('value') if isinstance(claim, dict) else None
    return v.get('id') if isinstance(v, dict) and 'id' in v else v


def _compact(v):
    if isinstance(v, dict):
        return v.get('time') if isinstance(v.get('time'), str) else None     # a date: its time string only
    return v if isinstance(v, (str, int)) else None


def _values(facts, prop):
    return list(((facts or {}).get('c') or {}).get(prop) or [])


def _label(entity):
    labels = (entity or {}).get('labels') or {}
    for lang in ('en', 'mul'):
        v = (labels.get(lang) or {}).get('value')
        if isinstance(v, str) and v.strip():
            return v.strip()[:120]
    return None


def _year(facts):
    years = [int(m.group(1)) for m in (re.match(r'\+(\d{4})-', v) for v in _values(facts, 'P577') if isinstance(v, str)) if m]
    return min(years) if years else None


def _relates(roots, props=DOWN):
    return 'haswbstatement:' + '|'.join('%s=%s' % (p, q) for q in roots for p in props)


def _roots(client, seeds):
    """The seeds' facts and their roots (each seed and what it states it belongs to, is based on or follows)."""
    facts = client.facts(seeds, True)
    roots = []
    for q in seeds:
        f = facts.get(q)
        if not isinstance(f, dict) or f.get('m') or not any(_values(f, p) for p in WORK):
            continue       # a character, a place, a concept: never a root (its own relations would drift)
        for r in [q] + [v for p in UP for v in _values(f, p) if isinstance(v, str) and QID.fullmatch(v)]:
            if r not in roots:
                roots.append(r)
    return facts, roots[:MAX_ROOTS]


def _title(seed_facts, seeds, labels):
    """The franchise's name, as the data states it: a seed's media franchise, else what it is based on, else the seed."""
    for prop in ('P8345', 'P144'):
        for q in seeds:
            for v in _values(seed_facts.get(q), prop):
                if labels.get(v):
                    return labels[v]
    return next((labels.get(q) or (seed_facts.get(q) or {}).get('l') for q in seeds
                 if labels.get(q) or (seed_facts.get(q) or {}).get('l')), None)


def _labels(client, ids):
    return {q: (f or {}).get('l') for q, f in client.facts(ids, False).items()}


def _groups_live(text, client):
    found = client.cached(json.dumps(['seeds', text.casefold()]), action='wbsearchentities', search=text, language='en',
                          uselang='en', type='item', limit=SEEDS)
    seeds = [s.get('id') for s in found.get('search') or [] if isinstance(s, dict) and QID.fullmatch(str(s.get('id')))][:SEEDS]
    client.kept.put_query(json.dumps(['seeds', text.casefold()]), dict(search=[dict(id=q) for q in seeds]))
    if not seeds:
        return dict(groups=[], absent=True)
    seed_facts, roots = _roots(client, seeds)
    if not roots:
        return dict(groups=[], absent=True)
    _, total = client.search(_relates(roots) + ' haswbstatement:P4947|P4983', 1, total=True)
    if not total:
        return dict(groups=[], absent=True)
    title = _title(seed_facts, seeds, _labels(client, roots))
    if not title:
        return dict(groups=[], absent=True)
    key = '.'.join(q.lower() for q in seeds[:5])
    return dict(groups=[dict(ref=dict(kind='franchise', key=key), title=title)])


def _group_live(ref, client):
    seeds = [q.upper() for q in ref['key'].split('.')]
    partial = False
    seed_facts, roots = _roots(client, seeds)
    if not roots:
        return None
    # Series and franchise items linked to a root join the roots (e.g. a film series based on a source work).
    series, _ = client.search('haswbstatement:' + '|'.join('P31=%s' % c for c in SERIES_CLASSES) + ' ' + _relates(roots, DOWN), 20)
    for q in series:
        if q not in roots and len(roots) < MAX_ROOTS:
            roots.append(q)
    labels = _labels(client, roots)
    _, linked = client.search(_relates(roots), 1, total=True)
    members, _ = client.search(_relates(roots) + ' haswbstatement:P4947|P4983', MAX_MEMBERS)
    members = members[:MAX_MEMBERS]      # a root that is itself an identified film or series (a seed film) is a member too
    try:
        facts = client.facts(members, True)
    except (BudgetExceeded, RateLimited):
        facts, partial = {q: client.kept.entity(q, True) for q in members}, True      # what is already kept, said as partial
    sections, unidentified = {'movie': [], 'tvshow': []}, 0
    for qid in members:
        f = facts.get(qid)
        if not isinstance(f, dict):
            partial = True
            continue
        movie, show = (_values(f, 'P4947') or [None])[0], (_values(f, 'P4983') or [None])[0]
        tmdb, title = movie or show, f.get('l')
        if not (isinstance(tmdb, str) and tmdb.isdigit() and title):
            unidentified += 1
            continue
        kind = 'movie' if movie else 'tvshow'
        member = dict(title=title, ref=dict(kind=kind, tmdb=int(tmdb)), wikidata=qid)
        series_of = next((labels.get(v) for v in _values(f, 'P179') if v in roots and labels.get(v)), None)
        basis = next((labels.get(v) for v in _values(f, 'P144') if v in roots and labels.get(v)), None)
        form = next((FORMS[v] for v in _values(f, 'P31') if v in FORMS), None)
        for k, v in (('year', _year(f)), ('series', series_of), ('basis', basis), ('form', form)):
            if v:
                member[k] = v
        sections[kind].append(member)
    for items in sections.values():
        items.sort(key=lambda m: (m.get('year') or 9999, m['title']))
    out = dict(kind='franchise', ref=dict(ref), title=_title(seed_facts, seeds, labels) or 'Franchise', sort='released', complete=False,
               sections=[dict(id=k, items=v, page=1, pages=1, total=len(v)) for k, v in sections.items() if v],
               linked=linked if type(linked) is int else None, unidentified=unidentified,
               roots=[labels[q] for q in roots if labels.get(q)][:24])
    if partial:
        out['partial'] = True
    return out


def _coalesce(kept, key, compute, deadline):
    """One computation per activation and key at a time: a second identical call OF THE SAME activation waits for the
    first one's answer (or its error). A call of a newer activation never joins an older one's work."""
    slot_key = (kept.id, key)
    with _inflight_lock:
        slot = _inflight.get(slot_key)
        owner = slot is None
        if owner:
            slot = _inflight[slot_key] = dict(event=threading.Event())
    if not owner:
        STATS['coalesced'] += 1
        if not slot['event'].wait(max(0.0, deadline - _mono())):
            raise BudgetExceeded()
        if 'error' in slot:
            raise slot['error']
        return slot['value']
    try:
        slot['value'] = compute()
        return slot['value']
    except Exception as exc:
        slot['error'] = exc
        raise
    finally:
        with _inflight_lock:
            _inflight.pop(slot_key, None)
        slot['event'].set()


CALLS = {'groups': (6, 20.0, SEARCH), 'group': (16, 40.0, OPEN)}


def _keep(kept, key, value):
    """Only a whole answer Wikidata gave is kept as last good; a partial read never replaces one."""
    if isinstance(value, dict) and not value.get('partial'):
        kept.put_answer(key, value)


def _refresh(kept, key, kind, live, fetch, interval):
    """Read a kept, no longer fresh answer again in the background (one at a time per activation and key, lowest
    priority). It belongs to the activation that started it: withdrawn, it stops and keeps nothing."""
    with _inflight_lock:
        if (kept.id, key) in _refreshing:
            return
        _refreshing.add((kept.id, key))

    def run():
        limit, budget, _ = CALLS[kind]
        try:
            client = Client(fetch, limit, budget, REFRESH, interval, kept=kept)
            value = _coalesce(kept, key, lambda: live(client), _mono() + budget)
            if kept.alive:
                _keep(kept, key, value)
                kept.flush()
                STATS['refreshed'] += 1
        except Exception:
            pass                             # the kept answer stays; the next look asks again
        finally:
            with _inflight_lock:
                _refreshing.discard((kept.id, key))
    threading.Thread(target=run, name='franchises-refresh', daemon=True).start()


def _answer(key, kind, live, fetch, settings, cancelled=None):
    kept = current()                         # this call's activation, fixed for all its work
    interval = INTERVALS.get((settings or {}).get('pace'), INTERVALS['standard'])
    held = kept.answer(key)
    if held is not None and held[1] < FRESH[kind]:
        STATS['answer_hits'] += 1
        return dict(held[0], cache_seconds=max(1, int(FRESH[kind] - held[1])))
    if held is not None:
        STATS['stale'] += 1
        _refresh(kept, key, kind, live, fetch, interval)
        out = dict(held[0], cache_seconds=STALE_SECONDS)
        if kind == 'group':
            out.update(stale=True, as_of=int(_wall() - held[1]))
        return out
    limit, budget, priority = CALLS[kind]
    client = Client(fetch, limit, budget, priority, interval, cancelled, kept=kept)
    try:
        value = _coalesce(kept, key, lambda: live(client), _mono() + budget)
    finally:
        kept.flush()
    if not kept.alive:
        raise Withdrawn()                    # turned off or removed meanwhile: nothing answered from it
    if value is None:
        return None
    _keep(kept, key, value)
    kept.flush()
    return dict(value, cache_seconds=60 if value.get('partial') else FRESH[kind])

def groups(query, settings, fetch):
    text = query.get('text') if isinstance(query, dict) else None
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 100:
        return None
    text = ' '.join(text.split())
    key = 'groups:' + text.casefold()
    _state['latest_search'] = key            # a newer, different search makes this one stop waiting
    try:
        out = _answer(key, 'groups', lambda client: _groups_live(text, client), fetch, settings,
                      cancelled=lambda: _state['latest_search'] != key)
    except Superseded:
        return dict(groups=[], partial=True, cache_seconds=0)   # replaced: nothing said, nothing kept
    out.pop('absent', None)
    return out


def group(request, settings, fetch):
    ref = request.get('ref') if isinstance(request, dict) else None
    if not (isinstance(ref, dict) and ref.get('kind') == 'franchise' and KEY.fullmatch(str(ref.get('key')))):
        return None
    return _answer('group:' + ref['key'], 'group', lambda client: _group_live(ref, client), fetch, settings)


def lookup(identity, settings, fetch):
    return None       # a groups-only plugin: no title facts
