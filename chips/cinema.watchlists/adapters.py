"""Watchlists service adapters: what each list/tracking service suits, which ids it needs, how a media reference maps to
it, and (where Accounts offers it) reading a list. Connections and secrets stay in Accounts & Services: an adapter
reads only through cinema.accounts/1 (submit/result/cancel), and none writes (no write capability exists).

No list service is pinned here (2026-11-18, audit R4): a list service is any Accounts provider that DECLARES its list in
cinema.accounts/1 (describe()["watchlist"]: the id namespace of its media ids, the kinds it lists, where its covers are),
and Watchlists reads it through the same contract. AniList is such a provider (Accounts' own); a second one needs no
Watchlists code. Nothing is written to know them (reading writes nothing): the user's bindings and rules keep the
service's id whatever is declared at the moment, and a choice of one not declared now gives no intent, honestly
("accounts_off" or "not_available"), and is never dropped.
Trakt stays DECLARED only (no Accounts provider offers it): always "not available", so policy can name it, refuse it and
explain why (an anime bound to AniList is never routed to Trakt), until a provider declares a list under that name."""
import re
import time

ACCOUNTS = ('cinema.accounts', 1, 'cinema.accounts')
STATUSES = ('current', 'planning', 'completed', 'dropped', 'paused', 'repeating')
USABLE = ('connected', 'unknown')   # connections a read may be tried with (unknown: stored, not verified since start)


class Adapter(object):
    id = label = None
    namespaces = ()          # ids it can map (an episode needs its installment's id in one of these)
    suits = ()               # classifications it is built for (a recommendation reason, never an authorization)
    kinds = ()               # media kinds it lists
    accounts_provider = None  # the Accounts provider holding its connection, or None (no connection exists)

    def info(self):
        return dict(id=self.id, label=self.label, namespaces=list(self.namespaces), suits=list(self.suits), kinds=list(self.kinds),
                    reads=self.accounts_provider is not None, writes=False)

    def map(self, ref):
        """(mapping, None) or (None, reason): the service-specific identity a progress fact would need."""
        raise NotImplementedError


TOKEN = re.compile(r'[a-z][a-z0-9_]{0,23}')
PROVIDER = re.compile(r'[a-z][a-z0-9_.-]{0,39}')
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._&()+-]{0,59}')
MAX_SERVICES = 8


def declaration(raw):
    """A provider's declared list as Watchlists uses it ({namespace, media_types}), or None (Accounts already validated
    it against the contract; this keeps only what is used, and only in that shape)."""
    if not isinstance(raw, dict):
        return None
    ns, kinds = raw.get('namespace'), raw.get('media_types')
    if not (isinstance(ns, str) and TOKEN.fullmatch(ns)) or not isinstance(kinds, list) or not 0 < len(kinds) <= 8 \
            or not all(isinstance(k, str) and TOKEN.fullmatch(k) for k in kinds):
        return None
    return dict(namespace=ns, media_types=list(kinds))


class Declared(Adapter):
    """A list service an Accounts provider declared: its id is the provider's, its media ids are in its declared namespace,
    and the kinds it lists are what it suits (a recommendation reason, never an authorization)."""

    def __init__(self, provider, label, scope):
        self.id, self.label, self.accounts_provider = provider, label, provider
        self.namespaces = (scope['namespace'],)
        self.kinds = self.suits = tuple(scope['media_types'])

    def map(self, ref):
        ns = self.namespaces[0]
        inst = (ref.get('installment') or {}).get('ids') or {}
        own = ref.get('ids') or {}
        media = inst.get(ns) or (own.get(ns) if ref['kind'] != 'episode' else None)
        if media is None:
            if (ref.get('work') or {}).get('ids'):
                return None, 'mapping_ambiguous'    # only the series is known: which AniList entry is not
            return None, 'mapping_missing'
        if ref['kind'] == 'episode':
            if not ref.get('episode') or ref['episode']['number'] is None:
                return None, 'mapping_missing'
            return dict(media_id=media, episode=ref['episode']['number']), None
        return dict(media_id=media, episode=None), None


class Trakt(Adapter):
    id, label = 'trakt', 'Trakt'
    namespaces = ('trakt', 'tmdb', 'imdb', 'tvdb')
    suits = ('movie', 'tv')
    kinds = ('movies', 'shows')
    accounts_provider = None

    def map(self, ref):
        own = ref.get('ids') or {}
        show = dict(((ref.get('work') or {}).get('ids') or {}), **((ref.get('installment') or {}).get('ids') or {}))
        usable = {k: v for k, v in (show if ref['kind'] == 'episode' else own).items() if k in self.namespaces}
        if not usable:
            return None, 'mapping_missing'      # e.g. an AniList-only anime: no Trakt identity is stated
        if ref['kind'] == 'episode' and (not ref.get('episode') or ref['episode'].get('season') is None):
            return None, 'mapping_ambiguous'    # Trakt needs a season number this reference does not state
        return dict(ids=usable, season=(ref.get('episode') or {}).get('season'), episode=(ref.get('episode') or {}).get('number')), None


class Known(object):
    """The list services Watchlists knows NOW: every Accounts provider declaring a list (refreshed at most every REFRESH
    seconds; while Accounts does not answer, the ones it declared earlier in this run), and the declared-only Trakt unless
    a provider declares a list under that name. In memory only: nothing is written. Read like a dict: id -> adapter."""
    REFRESH = 2.0

    def __init__(self, reader, stored=None, store=None, clock=time.monotonic):
        self.reader, self.clock = reader, clock
        self.at, self.table, self.seen = None, {}, {}

    def _now(self):
        if self.at is not None and self.clock() - self.at < self.REFRESH:
            return self.table
        self.at = self.clock()
        declared = self.reader.declared()
        if declared is not None:   # Accounts answered: what it declares now is what exists (a provider gone is forgotten)
            self.seen = {p: dict(scope, label=label) for p, label, scope in declared[:MAX_SERVICES]}
        table = {p: Declared(p, info['label'], info) for p, info in self.seen.items()}
        if 'trakt' not in table:
            table['trakt'] = Trakt()
        self.table = table
        return table

    def get(self, key, default=None):
        return self._now().get(key, default)

    def __contains__(self, key):
        return key in self._now()

    def __getitem__(self, key):
        return self._now()[key]

    def values(self):
        return list(self._now().values())

    def __iter__(self):
        return iter(list(self._now()))


class AccountsReader(object):
    """Reads through Accounts & Services only (the shipped cinema.accounts/1). Never sees a token."""

    def __init__(self, services, clock=time.monotonic, sleep=time.sleep):
        self.services, self.clock, self.sleep = services, clock, sleep

    def _accounts(self):
        try:
            if hasattr(self.services, 'get_one'):            # by contract (Cinema 2026-11-17+, D5): any one implementation
                return self.services.get_one(*ACCOUNTS[:2])
            return self.services.get(*ACCOUNTS)
        except Exception:
            return None

    def declared(self):
        """[(provider, label, {namespace, media_types})] for every Accounts provider declaring a list it reads, or None when
        Accounts is off or does not answer (then nothing is concluded about what exists)."""
        accounts = self._accounts()
        if accounts is None:
            return None
        try:
            status = accounts.status()
        except Exception:
            return None
        out = []
        for entry in status.get('providers', []) if isinstance(status, dict) else []:
            if not isinstance(entry, dict) or entry.get('state', 'ok') != 'ok' or 'watchlist.read' not in (entry.get('operations') or ()):
                continue
            provider, label, scope = entry.get('provider'), entry.get('label'), declaration(entry.get('watchlist'))
            if isinstance(provider, str) and PROVIDER.fullmatch(provider) and isinstance(label, str) and LABEL.fullmatch(label) and scope:
                out.append((provider, label, scope))
        return out

    def connection(self, adapter):
        """connected | unknown | not_connected | needs_attention | accounts_off | not_available (no such connection exists).

        "unknown" (2026-11-16, restart fix) is Accounts' own word for a stored connection not verified since Cinema
        started: it is NOT "not_connected". It stays usable: a read is tried and Accounts' answer decides."""
        if adapter.accounts_provider is None:
            return 'not_available'
        accounts = self._accounts()
        if accounts is None:
            return 'accounts_off'
        try:
            status = accounts.status()
        except Exception:
            return 'accounts_off'
        for entry in status.get('providers', []) if isinstance(status, dict) else []:
            if isinstance(entry, dict) and entry.get('provider') == adapter.accounts_provider:
                if entry.get('state', 'ok') != 'ok':
                    return 'unknown'          # the provider did not answer describe() just now: not known, not absent
                return entry.get('connection') if entry.get('connection') in USABLE + ('not_connected', 'needs_attention') else 'unknown'
        return 'not_available'

    def read_list(self, adapter, media_type, status, page, cancelled=lambda: False, limit=15.0):
        """('ok', page) | ('refused', code). The page is Accounts' validated watchlist.read answer."""
        accounts = self._accounts()
        if adapter.accounts_provider is None or accounts is None:
            return 'refused', 'not_available' if adapter.accounts_provider is None else 'accounts_off'
        try:
            answer = accounts.submit('watchlist.read', adapter.accounts_provider, dict(media_type=media_type, status=status, page=page))
        except Exception:
            return 'refused', 'accounts_off'
        if not answer.get('ok'):
            return 'refused', answer.get('code') or 'refused'
        until = self.clock() + limit
        while True:
            # Accounts may be turned off (or this module may end) mid-read (2026-11-17 modularity audit): that is an honest
            # refusal from THIS module ("accounts_off"), never an exception that the core would report as Watchlists off.
            try:
                got = accounts.result(answer['request'])
            except Exception:
                return 'refused', 'accounts_off'
            if not isinstance(got, dict):
                return 'refused', 'refused'
            if got.get('state') == 'done':
                return 'ok', got['value']
            if got.get('state') in ('refused', 'cancelled'):
                return 'refused', got.get('code') or 'refused'
            if cancelled() or self.clock() >= until:
                try:
                    accounts.cancel(answer['request'])
                except Exception:
                    pass   # already gone: nothing left to withdraw
                return 'refused', 'cancelled' if cancelled() else 'timed_out'
            self.sleep(0.05)
