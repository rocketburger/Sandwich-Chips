"""Advanced Media, first increment: publishes partial curated manifests (cinema_curated_manifests).

Inputs, each reported in describe() as available, unavailable or absent, never filled in:
- library: records supplied in this module's own storage, key "library":
      [{key?, media_type, title, year?, ids?: {scheme: value}, attributes?: {name: value},
        edition?: "label", reference?: "<one of this module's saved references>"}]
- kept: titles the user explicitly kept from playback through this module's page part
  (ui/main.mjs), one storage key each ("kept.<record key>", schema cinema.kept-title/1): the
  stated title information only, never a playable, request, generation or token. The user may
  give one a display label ("display": {"title", "supplied": "user"}) or remove it in the page's
  Library view: the label becomes the title fact (provenance field "display_title"), with the
  stated title kept as the attribute "stated_title"; a removed record is simply gone.
- references: this module's own saved playable references (ctx.playables.list()).
- providers: Media Provider Service availability, watched through its contract. Never manifest
  data: its only use is the user's own "Find sources" in Library (Discovery, below), whose
  candidates are shown, never stored or merged.

Rules:
- Title identity: records join one title only on the same media_type and the same value for the
  same id scheme; episodes also need the same usable stated season and episode numbers
  (contract.episode_position), and one without a usable position joins nothing. The scheme
  is the record's first in ORDER, then any other by name. There is no
  title-string or fuzzy matching. A record without ids is its own title. Within a title, the first
  record states title, year and attributes. An id scheme whose values conflict is left out.
- Playables: opaque playback-contract references, with their provider id. A record's "edition"
  label is stated on its own playable. The title's edition stays unknown: no classification.
- Sources, timeline and chapters: unavailable or unverified. Nothing is inferred from paths,
  runtimes or observation generations.
- Values that look like URLs are dropped. A reference that is not one of this module's saved
  references is dropped.
- Associations: a kept title's valid user-confirmed link (Library's Link…, "link.<key>") is published
  as the manifest's optional association: the candidate's stated facts and provider attribution, with
  edition, release and timeline unknown. It is not an input, an id, a grouping key or a playable. A link
  whose kept title changed (invalid), was removed, or is gone is not published; the next build drops it.
- Session-only: manifests are rebuilt from the inputs at each start. Each build is numbered and
  commits only if it is newer than the published one and this instance is still current. After the
  module is turned off, an old instance's build never becomes visible.
"""
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter

import cinema_curated_manifests as contract

NAME = 'advanced-media'
PROVIDERS = 'cinema.media-provider'
ORDER = ('tmdb', 'imdb', 'tvdb', 'musicbrainz', 'isbn', 'asin')
POLL = 1.0
REFERENCES = 15.0
MAX_RECORDS = 1000
KEY = re.compile(r'[a-z0-9][a-z0-9_.-]{0,31}')
MEDIA = re.compile(r'[a-z][a-z0-9_.-]{0,31}')
URLISH = contract.URLISH


def _text(value, limit=200):
    if not isinstance(value, str) or URLISH.search(value):
        return None
    value = ' '.join(value.split())
    return value[:limit] or None


def _stated(value, source, field):
    return None if value is None else dict(value=value, provenance=dict(input=source, field=field), confidence='stated')


def _ids(raw):
    out = {}
    for scheme, value in (raw.items() if isinstance(raw, dict) else ()):
        scheme, value = str(scheme).lower(), _text(value if isinstance(value, str) else str(value) if type(value) is int else None, 64)
        if KEY.fullmatch(scheme) and value and value.lower() not in ('none', 'null', '0'):
            out[scheme] = value
    return dict(list(out.items())[:contract.MAX_IDS])


def _attributes(raw):
    out = {}
    for name, value in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(value, bool) or (type(value) is int and abs(value) < 10 ** 12):
            kept = value
        else:
            kept = _text(value)
        if isinstance(name, str) and KEY.fullmatch(name) and kept is not None and len(out) < contract.MAX_ATTRIBUTES:
            out[name] = kept
    return out


KEPT_PREFIX = 'kept.'
KEPT_SCHEMA = 'cinema.kept-title/1'


def kept_records(values):
    """Titles the user kept from playback (this module's page part): one storage key each,
    "kept.<record key>". (records, dropped reasons). A kept record never carries a playable
    reference, request, generation or token: anything but the stated title information is ignored."""
    out, dropped = [], []
    for name in sorted(k for k in (values or {}) if isinstance(k, str) and k.startswith(KEPT_PREFIX))[:MAX_RECORDS]:
        raw, key = values[name], name[len(KEPT_PREFIX):]
        title = _text(raw.get('title')) if isinstance(raw, dict) else None
        if not isinstance(raw, dict) or raw.get('schema') != KEPT_SCHEMA or raw.get('key') != key or not KEY.fullmatch(key):
            dropped.append('kept_malformed')
            continue
        if title is None:
            dropped.append('title_missing')
            continue
        media = raw.get('media_type') if isinstance(raw.get('media_type'), str) and MEDIA.fullmatch(raw['media_type']) else 'unknown'
        record = dict(input='kept', key=key, media_type=media, title=title,
                      year=raw.get('year') if type(raw.get('year')) is int and 0 < raw['year'] < 10000 else None,
                      ids=_ids(raw.get('ids')), attributes=_attributes(raw.get('attributes')), edition=None,
                      reference=None, provider=None, available=None, display=display_label(raw),
                      # The attributed parent fact kept with the title (the core's generic shape; malformed stays
                      # {"state": "malformed"}): query evidence only, never ids, a key or a grouping.
                      parent=contract.asserted_parent(raw.get('parent')),
                      # The listing's descriptive snapshot kept with the title, validated again (the core's shape), or
                      # None (absent in older records, or unusable). Never ids, attributes, a key or a grouping.
                      description=contract.descriptive_snapshot(raw.get('description')))
        # The user's confirmed link, validated as Library reads it. Only a valid one is published, as an
        # association apart from the title's ids (never a grouping or matching key; see build()).
        link = read_link(values.get(LINK_PREFIX + key), key, record)
        record['link'] = link if link and link['state'] == 'valid' else None
        out.append(record)
    return out, dropped


def playback_record(current):
    """The title playing now, as its owned playback states it (alpha feedback item 6), or None: the stated identity of the
    current request (title, ids, year, series position, parent fact) and its listing's descriptive snapshot. It lets the
    playing title get Information (and enrichment by enabled Cinema plugins) without the user keeping it. Never a
    request, token, reference or locator; a title without a title or any stated id gives none (nothing to match)."""
    identity = current.get('identity') if isinstance(current, dict) and isinstance(current.get('identity'), dict) else None
    if identity is None:
        return None
    title, ids = _text(identity.get('title')), _ids(identity.get('ids'))
    if title is None or not ids:
        return None
    attributes = _attributes(identity.get('attributes'))
    for name in ('showtitle', 'season', 'episode'):
        if name in identity and len(attributes) < contract.MAX_ATTRIBUTES:
            attributes.update(_attributes({name: identity[name]}))
    media = identity.get('media_type') if isinstance(identity.get('media_type'), str) and MEDIA.fullmatch(identity['media_type']) else 'unknown'
    # Stable per stated identity (never the request or token): the same title playing again is the same record.
    key = 'playing-' + hashlib.sha256(json.dumps([media, sorted(ids.items()), attributes.get('season'), attributes.get('episode')]).encode('utf-8')).hexdigest()[:16]
    return dict(input='playback', key=key, media_type=media, title=title,
                year=identity.get('year') if type(identity.get('year')) is int and 0 < identity['year'] < 10000 else None,
                ids=ids, attributes=attributes, edition=None, reference=None, provider=None, available=None, display=None,
                parent=contract.asserted_parent(identity.get('parent')),
                description=contract.descriptive_snapshot(current.get('description')), link=None)


# Inputs whose title-level facts (enrichment, markers, candidates, listing description) are looked up and attached:
# the titles the user kept, then the title playing now. Kept first, so a kept title's own facts stand.
LOOKED_UP = ('kept', 'playback')


def display_label(raw):
    """The user's display label of a kept record ({"display": {"title", "supplied": "user"}}), or None.
    A malformed one is ignored: the record still states its own title."""
    display = raw.get('display') if isinstance(raw, dict) else None
    if not isinstance(display, dict) or display.get('supplied') != 'user':
        return None
    return _text(display.get('title'))


def records(library, references, kept=None):
    """Clean input records: (records, dropped reasons). library: the supplied list; references:
    ctx.playables.list() rows; kept: the module's storage values (kept_records)."""
    known = {r['ref']: r for r in references if isinstance(r, dict) and isinstance(r.get('ref'), str)}
    kept_out, dropped = kept_records(kept)
    out = []
    for index, raw in enumerate(library[:MAX_RECORDS] if isinstance(library, list) else []):
        if not isinstance(raw, dict):
            dropped.append('not_a_record')
            continue
        title = _text(raw.get('title'))
        if title is None:
            dropped.append('title_missing')
            continue
        reference = raw.get('reference')
        if reference is not None and reference not in known:
            dropped.append('reference_unknown')
            reference = None
        media = raw.get('media_type') if isinstance(raw.get('media_type'), str) and MEDIA.fullmatch(raw['media_type']) else 'unknown'
        key = raw.get('key') if isinstance(raw.get('key'), str) and KEY.fullmatch(raw['key'].lower()) else 'position-%d' % index
        out.append(dict(input='library', key=key, media_type=media, title=title, year=raw.get('year') if type(raw.get('year')) is int and 0 < raw['year'] < 10000 else None,
                        ids=_ids(raw.get('ids')), attributes=_attributes(raw.get('attributes')), edition=_text(raw.get('edition'), 80),
                        reference=reference, provider=(known.get(reference) or {}).get('provider', {}).get('id') if reference else None,
                        available=(known.get(reference) or {}).get('available') if reference else None))
    out.extend(kept_out)  # after the supplied library: an existing entry keeps stating its title
    for row in known.values():
        identity = row.get('identity') if isinstance(row.get('identity'), dict) else {}
        title = _text(identity.get('title')) or _text(row.get('label'))
        if title is None:
            dropped.append('reference_without_title')
            continue
        attributes = _attributes(identity.get('attributes'))
        for name in ('showtitle', 'season', 'episode'):
            if name in identity and len(attributes) < contract.MAX_ATTRIBUTES:
                attributes.update(_attributes({name: identity[name]}))
        media = identity.get('media_type') if isinstance(identity.get('media_type'), str) and MEDIA.fullmatch(identity['media_type']) else 'unknown'
        out.append(dict(input='reference', key='reference-' + row['ref'], media_type=media, title=title,
                        year=identity.get('year') if type(identity.get('year')) is int and 0 < identity['year'] < 10000 else None,
                        ids=_ids(identity.get('ids')), attributes=attributes, edition=None, reference=row['ref'],
                        provider=(row.get('provider') or {}).get('id'), available=row.get('available') if isinstance(row.get('available'), bool) else None))
    return out, dropped


# Automatic metadata enrichment (cinema.metadata-enrichment, provided by the core over its existing metadata
# registry). Advanced Media decides what to ask and what to keep: a kept title with an explicit supported id
# (tmdb, imdb or tvdb) of a supported kind (movie, tvshow) is looked up by that id alone, one lookup at a time on
# this module's own worker. A kept episode is looked up only by its series-scoped ids (tvshow.tmdb/tvdb/imdb)
# and its stated season and episode numbers; a bare id on an episode is never sent. Answers are descriptive attributes only (input "enrichment"), attributed to their
# provider: never the title, display label, year, ids, grouping, edition, source, timeline or seek evidence, and
# never replacing what a record states. An answer is kept for its (record, kind, ids) and the service's revision;
# one that arrives after the title was removed or changed, the providers were reconfigured or this module stopped
# is discarded. No provider, a disabled or unconfigured one, or an unsupported kind gives a partial manifest that
# says so.
ENRICHMENT = 'cinema.metadata-enrichment'


class Enrichment(object):
    def __init__(self, ctx):
        import cinema_metadata_enrichment as contract_
        self.ctx, self.contract = ctx, contract_
        self.client = contract_.Client(ctx.services)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.wanted = {}    # record key -> (asked, revision): what the current kept titles need
        self.results = {}   # record key -> (asked, revision, answer)
        self.running = None
        self.discarded = 0  # late answers thrown away (removed, changed, reconfigured, stopped)

    # An unavailable answer (the provider failed, or was reconfigured mid-lookup) is asked again only after this
    # long: the registry remembers a failure for the same 60 s, so this never becomes a tight retry loop. Found and
    # no_match answers are kept for their revision.
    RETRY_UNAVAILABLE = 60.0
    REASONS = {'provider_failed': 'The metadata provider could not answer; it is asked again later.',
               'reconfigured': 'Looking up details.', 'publisher_unavailable': 'No metadata service.',
               'publisher_failed': 'Details are not available right now.'}
    NO_SERVICE, NO_MATCH, NOT_NOW, PENDING = 'No metadata service.', 'No details found for this id.', 'Details are not available right now.', 'Looking up details.'
    NUMBERING_GATE = True  # descriptive facts: only the numbering the enrichment query's season/episode mean

    def _blank(self, state, reason, code=None):
        # code: the contract's coded reason (cinema_curated_manifests.ENRICHMENT_CODES, audit R11); reason: its words.
        return dict(state=state, reason=reason, provider_label=None, facts={}, code=code)

    def _found(self, answer):
        out = dict(state='available', reason=None, provider_label=answer['provider_label'], facts=dict(answer['facts']))
        if answer.get('retrieved_at'):
            out['retrieved_at'] = answer['retrieved_at']   # when the provider's answer was retrieved (not its freshness)
        if answer.get('presentation'):  # optional display fields (artwork handles, cast, genres, runtime, tagline)
            out['presentation'] = dict(answer['presentation'])
        return out

    def _stale(self, held):
        return held[2]['state'] == 'unavailable' and time.monotonic() - held[3] >= self.RETRY_UNAVAILABLE

    def _service(self):
        publishers = self.client.publishers()
        if not publishers:
            return None, None
        name = publishers[0]['name']
        return name, self.client.describe(name)

    def statuses(self, kept):
        """{record key: {state, reason, provider_label, facts}} for these kept records, as known now; asks the worker
        for what is missing. state: available | pending | unavailable | absent."""
        name, described = self._service()
        out, wanted = {}, {}
        for record in kept:
            asked, basis, refusal = self._ask(record)
            if asked is None:
                out[record['key']] = self._blank(refusal[0], refusal[1], 'not_usable' if refusal[0] == 'unavailable' else None)
                continue
            if described is None:
                out[record['key']] = self._blank('unavailable', self.NO_SERVICE, 'no_service')
                continue
            if described['state'] != 'ready':
                out[record['key']] = self._blank('unavailable', described['reason'] or described['state'], 'not_ready')
                continue
            wanted[record['key']] = (asked, described['revision'], basis)
            with self.lock:
                held = self.results.get(record['key'])
            if held and (held[0], held[1], held[4]) == wanted[record['key']] and not self._stale(held):
                answer = held[2]
                if answer['state'] == 'found':
                    out[record['key']] = self._found(answer)
                    if record['media_type'] == 'episode' and asked['media_type'] == 'tvshow':
                        out[record['key']]['scope'] = 'installment'   # the series entry's facts, never the episode's
                elif answer['state'] == 'no_match':
                    out[record['key']] = self._blank('unavailable', self.NO_MATCH, 'no_match')
                else:
                    out[record['key']] = self._blank('unavailable', self.REASONS.get(answer['reason'], self.NOT_NOW),
                                                     'no_service' if answer['reason'] == 'publisher_unavailable' else 'pending' if answer['reason'] == 'reconfigured' else 'not_now')
            else:
                out[record['key']] = self._blank('pending', self.PENDING, 'pending')
        with self.lock:
            self.wanted = wanted
            self.results = {k: v for k, v in self.results.items() if k in wanted}  # removed titles' answers go
            self.name = name
            self.described = described  # what the service said for this build (TEST trace only reads it)
        if any(s['state'] == 'pending' for s in out.values()):
            self.wake.set()
        return out

    def _ask(self, record):
        """(query, basis, None) for a kept record, or (None, None, (state, reason)). basis: the attributed parent
        assertion the query's series ids were derived from (None when the record states them itself); it is
        part of what an answer is kept for, so a late answer never fills a changed assertion."""
        season, episode = record['attributes'].get('season'), record['attributes'].get('episode')
        media = record['media_type']
        ids, basis, numbering, refusal = derive_ids(record, self.contract.EPISODE_NUMBERING if self.NUMBERING_GATE else None)
        parent = record.get('parent') or {}
        if refusal is not None and media == 'episode' and parent.get('state') == 'asserted' and parent.get('numbering') == 'installment':
            # An episode numbered in its INSTALLMENT's own numbering (a video extension's play, 2026-11-17): its season and
            # episode are not a TV database's, so it is never looked up as that episode. A MyAnimeList entry is one
            # installment: it is described at the installment level only (as _query does for a stated tvshow.mal).
            mal = record['ids'].get('tvshow.mal') or parent.get('ids', {}).get('mal')
            asked = self.contract.query('tvshow', {'mal': mal}) if isinstance(mal, str) else None
            if asked is not None:
                return asked, (parent['producer'], parent['mapping'], parent['numbering'], tuple(sorted(parent['ids'].items()))), None
        if refusal is not None:
            return None, None, refusal
        asked = self._query(media, ids, season, episode, numbering)
        if asked is not None:
            return asked, basis, None
        if media not in self.contract.MEDIA:
            return None, None, ('absent', 'This kind of title is not looked up.')
        if media == 'episode' and not self.contract.coordinates(season, episode):
            return None, None, ('absent', 'No usable season and episode numbers are stated.')
        if media == 'episode':
            return None, None, ('absent', 'No series id is stated for this episode.')  # a bare id is not taken as the series' or the episode's
        return None, None, ('absent', 'No supported id is stated.')

    def _query(self, media, ids, season, episode, numbering):
        asked = self.contract.query(media, ids, season, episode)
        if asked is None and media == 'episode' and isinstance(ids.get('tvshow.mal'), str):
            # A MyAnimeList entry is one installment (2026-11-15): a kept episode stating its installment's MAL id is
            # described at the installment level only (scope "installment"), never as the episode; nothing converts or
            # guesses its numbering. Other episodes are unchanged.
            asked = self.contract.query('tvshow', {'mal': ids['tvshow.mal']})
        return asked

    def _next(self):
        with self.lock:
            for key, (asked, revision, basis) in self.wanted.items():
                held = self.results.get(key)
                if not (held and (held[0], held[1], held[4]) == (asked, revision, basis) and not self._stale(held)):
                    return key, asked, revision, basis, getattr(self, 'name', None)
        return None

    def run(self):
        """This module's own worker: one lookup at a time (it blocks only this thread; the registry's cache and
        transport do the work, shared with the page's details)."""
        while not self.ctx.stopping.is_set():
            job = self._next()
            if job is None or job[4] is None:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            key, asked, revision, basis, name = job
            try:
                answer = self.client.lookup(name, asked)
            except Exception as exc:  # contained: this module's consumer ended, or anything else
                self.ctx.log('enrichment: %s' % type(exc).__name__)
                return
            if answer['state'] == 'unavailable' and answer['revision'] == -1:
                # A consumer-side failure (the publisher's answer was malformed, it failed, or it went away) has no
                # publisher revision: it is held for the revision it was asked under, so the retry window applies
                # (never an immediate re-ask, which would loop against a persistently broken publisher).
                answer = dict(answer, revision=revision)
            with self.lock:
                current = self.wanted.get(key)
                if self.ctx.stopping.is_set() or current != (asked, revision, basis) or answer['revision'] != revision:
                    self.discarded += 1  # removed, changed (ids, position or assertion), reconfigured or stopped meanwhile
                    continue
                self.results[key] = (asked, revision, answer, time.monotonic(), basis)


def derive_ids(record, numbering_gate=None):
    """(ids, basis, numbering, refusal) for a kept record's lookup: its stated ids, plus for an episode with an
    attributed parent the series-scoped ids derived ONLY from that verified assertion. A stated scoped id that
    disagrees refuses (neither is chosen); a malformed assertion is unusable (no bare-id or title fallback).
    numbering_gate: the numberings this lookup accepts (None: carry the numbering, the publisher decides).
    basis: the assertion (producer, mapping, numbering, ids), part of every stale comparison."""
    import cinema_metadata_enrichment as enrichment_contract
    ids, basis, numbering = dict(record['ids']), None, None
    parent = record.get('parent')
    if record['media_type'] == 'episode' and parent is not None:
        if parent['state'] != 'asserted':
            return None, None, None, ('unavailable', 'The series id assertion for this episode is not usable.')
        if numbering_gate is not None and parent['numbering'] not in numbering_gate:
            return None, None, None, ('absent', "This episode's numbering is not the looked-up season and episode numbering.")
        for scheme, value in parent['ids'].items():
            scoped = 'tvshow.' + scheme
            if scoped not in enrichment_contract.SERIES_SCHEMES:
                continue
            if scoped in ids and ids[scoped] != value:
                return None, None, None, ('unavailable', 'The stated and the asserted series ids disagree.')
            ids[scoped] = value
        basis = (parent['producer'], parent['mapping'], parent['numbering'], tuple(sorted(parent['ids'].items())))
        numbering = parent['numbering']
    return ids, basis, numbering, None


class Markers(Enrichment):
    """Title-level intro/recap/credits/preview candidates (cinema.media-markers), curated like enrichment: the same
    id-only query rules, parent derivation, conflicts, stale basis, retry window and lifecycle, on this module's
    own second worker (one lookup at a time). Published as the manifest's "markers" (binding "title", timeline
    "unverified"): never bound to a playing source, edition or timeline, never a seek, skip or timeline mark."""
    NUMBERING_GATE = False  # the episode's numbering is carried; the marker publisher refuses what it cannot place
    REASONS = {'provider_failed': 'The marker provider could not answer; it is asked again later.',
               'reconfigured': 'Looking up markers.', 'publisher_unavailable': 'No marker service.',
               'publisher_failed': 'Markers are not available right now.',
               'unsupported_numbering': "The marker provider cannot place this episode's numbering."}
    NO_SERVICE, NO_MATCH, NOT_NOW, PENDING = 'No marker service.', 'No markers found for this id.', 'Markers are not available right now.', 'Looking up markers.'

    def __init__(self, ctx):
        super().__init__(ctx)
        import cinema_media_markers as markers_contract
        self.contract = markers_contract
        self.client = markers_contract.Client(ctx.services)

    def _blank(self, state, reason, code=None):   # code: Enrichment's coded reason (R11); not carried by this contract
        return dict(state=state, reason=reason, provider_label=None, measured_ms=None, items=[])

    def _found(self, answer):
        return dict(state='available', reason=None, provider_label=answer['provider_label'], measured_ms=answer['measured_ms'],
                    items=[dict(m) for m in answer['markers']])

    def _query(self, media, ids, season, episode, numbering):
        return self.contract.query(media, ids, season, episode, numbering)


class ChapterCandidates(Markers):
    """Title-level conventional chapter SET candidates (cinema.chapter-candidates), curated with the same machinery:
    id-only queries (a movie's IMDb id; an episode only by an id explicitly scoped to the episode), stale basis,
    retry window, discard and lifecycle, on this module's own third worker. Each provider set is kept whole and
    apart, with its stable provider ref, note and votes as the provider's provenance; published as the manifest's
    "chapter_candidates" (binding "title", timeline "unverified"): never this source's chapters, an edition, a
    timeline, or a seek, skip or mark."""
    REASONS = {'provider_failed': 'The chapter provider could not answer; it is asked again later.',
               'reconfigured': 'Looking up chapters.', 'publisher_unavailable': 'No chapter service.',
               'publisher_failed': 'Chapter lists are not available right now.',
               'unusable_sets': "The provider's chapter lists for this title are not usable."}
    NO_SERVICE, NO_MATCH, NOT_NOW, PENDING = 'No chapter service.', 'No chapter lists found for this id.', 'Chapter lists are not available right now.', 'Looking up chapters.'

    def __init__(self, ctx):
        Enrichment.__init__(self, ctx)
        import cinema_chapter_candidates as candidates_contract
        self.contract = candidates_contract
        self.client = candidates_contract.Client(ctx.services)

    def _ask(self, record):
        """(query, None, None) or (None, None, (state, reason)). Only the record's own stated ids: a series parent
        assertion is not a chapter query fact (and a bare id is never taken as the episode's)."""
        media = record['media_type']
        asked = self.contract.query(media, record['ids'])
        if asked is not None:
            return asked, None, None
        if media not in self.contract.MEDIA:
            return None, None, ('absent', 'This kind of title is not looked up.')
        if media == 'episode':
            return None, None, ('absent', 'No episode-scoped TVDB id is stated, so chapters are not looked up.')
        return None, None, ('absent', 'No IMDb id is stated, so chapters are not looked up.')

    def _blank(self, state, reason, code=None):   # code: Enrichment's coded reason (R11); not carried by this contract
        return dict(state=state, reason=reason, provider_label=None, sets=[])

    def _found(self, answer):
        return dict(state='available', reason=None, provider_label=answer['provider_label'],
                    sets=[dict(s, chapters=[dict(c) for c in s['chapters']]) for s in answer['sets']])

    def _query(self, media, ids, season, episode, numbering):
        return self.contract.query(media, ids, season, episode, numbering)


class EditionCandidates(ChapterCandidates):
    """Title-level EDITION candidates and their timed points (cinema.title-editions), curated with the same machinery
    (id-only query: a movie's exact IMDb id; stale basis, retry window, discard, lifecycle) on this module's own
    worker. Published as the manifest's "edition_candidates" (binding "title", timeline "unverified"), WITHOUT
    fingerprint values (schemes only; the values stay in this backend's held answer). It never sets the title's
    edition (still "unknown"), a playable's source or a timeline, and never pairs a chapter-candidate set with an
    edition because both name the same title."""
    REASONS = {'provider_failed': 'The edition provider could not answer; it is asked again later.',
               'reconfigured': 'Looking up editions.', 'publisher_unavailable': 'No edition service.',
               'publisher_failed': 'Editions are not available right now.',
               'ambiguous_id': 'The provider has more than one record for this id.',
               'not_this_record': "The provider's records do not establish this title.",
               'wrong_back_reference': "The provider's records do not establish this title.",
               'cycle': "The provider's records do not establish this title.",
               'malformed_ref': "The provider's records are not usable.", 'malformed_container': "The provider's records are not usable.",
               'budget': 'The provider has more records than are read.'}
    NO_SERVICE, NO_MATCH, NOT_NOW, PENDING = 'No edition service.', 'No editions found for this id.', 'Editions are not available right now.', 'Looking up editions.'

    def __init__(self, ctx):
        Enrichment.__init__(self, ctx)
        import cinema_title_editions as editions_contract
        self.contract = editions_contract
        self.client = editions_contract.Client(ctx.services)

    def _ask(self, record):
        asked = self.contract.query(record['media_type'], record['ids'])
        if asked is not None:
            return asked, None, None
        if record['media_type'] != 'movie':
            return None, None, ('absent', 'Editions are looked up for films only.')
        return None, None, ('absent', 'No IMDb id is stated, so editions are not looked up.')

    def _blank(self, state, reason, code=None):   # code: Enrichment's coded reason (R11); not carried by this contract
        return dict(state=state, reason=reason, provider_label=None, complete=None, editions=[])

    def _found(self, answer):
        editions = []
        for e in answer['editions']:
            editions.append(dict(edition_ref=e['edition_ref'], version_label=e['version_label'], edition_label=e['edition_label'],
                                 duration_ms=e['duration_ms'], frame_rate=e['frame_rate'],
                                 files=[dict(schemes=[k['scheme'] for k in f['keys']], duration_ms=f['duration_ms'], frame_rate=f['frame_rate'],
                                             provider_confidence=f['provider_confidence']) for f in e['files']],
                                 points=[dict(at_ms=p['at_ms'], concept_label=p['concept_label']) for p in e['points']],
                                 points_complete=e['points_complete'], unlabelled_points=e['unlabelled_points']))
        return dict(state='available', reason=None, provider_label=answer['provider_label'], complete=answer['complete'], editions=editions)


def first_scheme(ids):
    return next((s for s in ORDER if s in ids), None) or (sorted(ids)[0] if ids else None)


def group_key(record):
    ids = record['ids']
    scheme = first_scheme(ids)
    if record['media_type'] == 'episode':
        # An episode's id may be its series' (stated bare or scoped): its stated position is part of what it is,
        # so two episodes of one series are never one title (nor share its enrichment). Without a stated
        # position it cannot be told from its series' other episodes: it joins nothing.
        at = contract.episode_position(record['attributes'].get('season'), record['attributes'].get('episode'))
        parent = record.get('parent')
        if at is None or scheme is None or (parent is not None and parent.get('state') != 'asserted'):
            return ('record', record['input'], record['key'])  # no position, no id, or an unusable parent assertion: joins nothing
        # The raw ids and position stay the identity. A position means something only in its numbering, so the
        # numbering scope is added as a SEPARATOR (never a replacement id): no assertion (as before), or the asserted
        # numbering and the parent's ids (e.g. "mal-relative" under a MAL id, "tmdb-aired" under a TMDB series).
        # Two numberings sharing a bare series id (an anime mapping row's franchise tmdb) never join, nor two MAL
        # entries of one franchise at the same stated numbers.
        scope = ('unstated',) if parent is None else ('numbering', parent['numbering']) + tuple(sorted(parent['ids'].items()))
        return ('id', record['media_type'], scheme, ids[scheme]) + at + scope
    if scheme is None:
        return ('record', record['input'], record['key'])
    return ('id', record['media_type'], scheme, ids[scheme])


# Descriptive field names as each source spells them, normalized to the snapshot's (contract.DESCRIPTION_FACTS):
# enrichment states "overview"; a listing's plot becomes "overview" at the bridge. A field any attribute already
# states (stated, user-supplied or enrichment) is never filled from a listing, under any of its names.
DESCRIPTIVE_NAMES = dict(overview=('overview', 'plot', 'synopsis', 'description'), genre=('genre', 'genres'), runtime=('runtime', 'duration'))


def listing_description(members, attributes):
    """The manifest's listing description, or None. Deterministic source precedence, not a judgement of any provider:
    a field is published from a kept listing snapshot only when no attribute of the title (stated, user-supplied or
    enrichment, already merged into attributes) states it. One snapshot: the first kept member's that has one."""
    holder = next((r for r in members if r['input'] in LOOKED_UP and r.get('description')), None)  # kept first, then playing
    if holder is None:
        return None
    snapshot = holder['description']
    taken = {name for name, spellings in DESCRIPTIVE_NAMES.items() if any(s in attributes for s in spellings)}
    facts = {name: value for name, value in snapshot['facts'].items() if name not in taken}
    if not facts:
        return None
    return dict(snapshot, facts=facts, truncated=[n for n in snapshot['truncated'] if n in facts],
                provenance=dict(input=holder['input'], field='description'))


def title_id(key):
    return hashlib.sha256(json.dumps(key).encode('utf-8')).hexdigest()[:24]


def build(clean, revision, enrichment=None, markers=None, chapter_candidates=None, edition_candidates=None):
    """{title_id: manifest} from clean records. Deterministic: the same inputs give the same ids. enrichment:
    {kept record key: status} (Enrichment.statuses), or None (no enrichment input at all)."""
    groups = {}
    for record in clean:
        groups.setdefault(title_id(group_key(record)), []).append(record)
    out = {}
    for ident, members in groups.items():
        first = members[0]
        ids, conflicts = {}, set()
        for record in members:
            for scheme, value in record['ids'].items():
                if scheme in ids and ids[scheme][0] != value:
                    conflicts.add(scheme)
                ids.setdefault(scheme, (value, record['input']))
        playables, seen = [], set()
        for record in members:
            if record['reference'] and record['reference'] not in seen and record['provider'] and KEY.fullmatch(record['provider']):
                seen.add(record['reference'])
                label = _stated(record['edition'], record['input'], 'edition')
                playables.append(dict(reference=record['reference'], provider=record['provider'], available=record['available'],
                                      edition=dict(state='stated', label=label) if label else dict(state='unknown'),
                                      source=dict(state='unknown', reason='No source observation input yet.')))
        # A user's display label (a kept record's own): the title fact says it is that field, not a
        # provider's; the stated title stays beside it. Nothing else of the title changes.
        label = first.get('display')
        attributes = {'stated_title': _stated(first['title'], first['input'], 'title')} if label else {}  # first: never the one cut at the limit
        attributes.update({k: _stated(v, first['input'], 'attributes') for k, v in first['attributes'].items() if k != 'stated_title'})
        associations = []
        for record in members:
            value = association(record.get('link'))
            if value is not None and value not in associations and len(associations) < contract.MAX_ASSOCIATIONS:
                associations.append(value)
        out[ident] = dict(
            schema=contract.SCHEMA, title_id=ident, revision=revision, media_type=first['media_type'],
            title=_stated(label, first['input'], 'display_title') if label else _stated(first['title'], first['input'], 'title'),
            year=_stated(first['year'], first['input'], 'year'),
            ids=[dict(scheme=s, fact=_stated(v, source, 'ids')) for s, (v, source) in ids.items() if s not in conflicts],
            attributes=attributes,
            edition=dict(state='unknown'), playables=playables,
            sources=dict(state='unavailable', reason='No source observation input yet.'),
            timeline=dict(state='unverified', reason='No verified timeline input yet.'),
            chapters=dict(state='unavailable', reason='No chapter input yet.'),
            completeness='partial', inputs=sorted({r['input'] for r in members}))
        if associations:  # optional: a title without a link publishes exactly as before
            out[ident]['associations'] = associations
        holder = next((r for r in members if r['input'] in LOOKED_UP and r.get('parent')), None)
        if holder is not None:  # optional: who asserted the series' ids, as kept or playing (never the title's ids)
            out[ident]['parent'] = dict(holder['parent'], provenance=dict(input=holder['input'], field='parent'))
        status = next((enrichment[r['key']] for r in members if r['input'] in LOOKED_UP and enrichment and r['key'] in enrichment), None)
        if status is not None:  # optional: what enrichment says for this title, and its attributed facts
            for name, value in status['facts'].items():
                if name not in attributes and len(attributes) < contract.MAX_ATTRIBUTES:  # never replaces a stated attribute
                    attributes[name] = _stated(value, 'enrichment', name)
            out[ident]['enrichment'] = dict(state=status['state'], reason=status['reason'], provider_label=status['provider_label'])
            if status.get('code'):   # the coded reason (R11): consumers act on it, never on the words
                out[ident]['enrichment']['code'] = status['code']
            out[ident]['enrichment'].update({k: status[k] for k in ('scope', 'retrieved_at') if status.get(k)})
            if status.get('presentation') and status['provider_label']:  # optional: how the title can be shown, attributed
                out[ident]['presentation'] = dict(status['presentation'], provenance=dict(input='enrichment', provider_label=status['provider_label']))
            if status['facts']:
                out[ident]['inputs'] = sorted(set(out[ident]['inputs']) | {'enrichment'})
        described = listing_description(members, attributes)
        if described is not None:  # optional: a listing's descriptive facts, only where nothing above states that field
            out[ident]['description'] = described
        found = next((markers[r['key']] for r in members if r['input'] in LOOKED_UP and markers and r['key'] in markers), None)
        if found is not None:  # optional: title-level marker candidates, attributed; never source-bound or actionable
            out[ident]['markers'] = dict(found, binding='title', timeline='unverified')
        candidates = next((chapter_candidates[r['key']] for r in members if r['input'] in LOOKED_UP and chapter_candidates and r['key'] in chapter_candidates), None)
        if candidates is not None:  # optional: title-level chapter set candidates, each set whole; never this source's chapters
            out[ident]['chapter_candidates'] = dict(candidates, binding='title', timeline='unverified')
        editions = next((edition_candidates[r['key']] for r in members if r['input'] in LOOKED_UP and edition_candidates and r['key'] in edition_candidates), None)
        if editions is not None:  # optional: title-level edition candidates; the title's own edition stays as it is
            out[ident]['edition_candidates'] = dict(editions, binding='title', timeline='unverified')
    return out


def association(link):
    """A valid Library link (read_link) as the contract's association: what the user confirmed, with its
    provider attribution and explicit unknowns. Not the query, the addon id or anything playable."""
    if not isinstance(link, dict) or link.get('state') != 'valid':
        return None
    found, origin = link['candidate'], link['provenance']
    return dict(kind='discovery_link', confirmed='user',
                candidate=dict(title=found['title'], media_type=found['media_type'], year=found['year'], season=found['season'],
                               episode=found['episode'], show=found['show'], ids=[dict(scheme=s, value=v) for s, v in sorted(found['ids'].items())]),
                provenance=dict(input='discovery', provider=origin['provider'], provider_label=origin['provider_label'], source_label=origin['source_label']),
                edition=dict(UNKNOWN), release=dict(UNKNOWN), timeline=dict(UNKNOWN))


class Publisher(object):
    """What this instance publishes under the contract. Reads are in-memory."""

    def __init__(self, alive):
        self.alive = alive
        self.lock = threading.Lock()
        self.built = 0
        self.revision = 0
        self.lifecycle = 'starting'
        self.manifests = {}
        self.inputs = dict(library=dict(state='absent', reason='Not read yet.'), references=dict(state='absent', reason='Not read yet.'),
                           providers=dict(state='absent', reason='Not watched yet.'),
                           sources=dict(state='absent', reason='No source observation input in this version.'),
                           chapters=dict(state='absent', reason='No chapter input in this version.'))

    def publish(self, number, manifests, inputs):
        """Commits a build only if it is newer than the published one and this instance is still current."""
        with self.lock:
            if not self.alive() or number <= self.built:
                return False
            self.built = number
            if manifests != self._without_revision(self.manifests) or inputs != self.inputs or self.lifecycle != 'ready':
                self.revision += 1
                self.manifests = {k: dict(v, revision=self.revision) for k, v in manifests.items()}
                self.inputs, self.lifecycle = inputs, 'ready'
            return True

    @staticmethod
    def _without_revision(manifests):
        return {k: dict(v, revision=0) for k, v in manifests.items()}

    def stopping(self):
        with self.lock:
            self.lifecycle = 'stopping'

    # The contract ----------------------------------------------------------------------
    def describe(self):
        with self.lock:
            return dict(label='Advanced Media', schema=contract.SCHEMA, revision=self.revision, lifecycle=self.lifecycle,
                        inputs=json.loads(json.dumps(self.inputs)))

    def titles(self):
        with self.lock:
            return dict(revision=self.revision, titles=[dict(title_id=m['title_id'], media_type=m['media_type'], title=m['title']['value'],
                                                             year=m['year']['value'] if m['year'] else None, playables=len(m['playables']))
                                                        for m in self.manifests.values()])

    def manifest(self, ident):
        with self.lock:
            value = self.manifests.get(ident)
            return json.loads(json.dumps(value)) if value is not None else None


class Instance(object):
    """One start of the module: reads its inputs, builds and publishes, until it is turned off."""

    def __init__(self, ctx, clock=time.monotonic):
        self.ctx, self.clock, self.cached = ctx, clock, None
        self.publisher = Publisher(alive=lambda: not ctx.stopping.is_set())
        self.number = 0
        self.lock = threading.Lock()
        self.providers = set()
        self.watched = None  # None until the provider contract could be watched
        self.enrichment = None  # Enrichment, when this module may use services
        self.markers = None     # Markers (title-level marker candidates), likewise
        self.chapter_candidates = None  # ChapterCandidates (title-level chapter set candidates), likewise
        self.edition_candidates = None  # EditionCandidates (title-level edition candidates), likewise
        self.trace = [] if os.environ.get('CINEMA_TEST_TRACE') == '1' else None  # TEST diagnostics only (_trace_markers)

    def _provider_changed(self, event):
        with self.lock:
            (self.providers.add if event.get('type') == 'available' else self.providers.discard)(event.get('name'))

    def watch(self):
        try:
            self.ctx.services.watch(PROVIDERS, self._provider_changed)
            self.watched = True
        except Exception as exc:  # the registry refused (a limit): report it, never fail the module
            self.watched = False
            self.ctx.log('provider availability is not watched: %s' % type(exc).__name__)

    def _references(self):
        """Listing saved items asks their provider for availability (a platform call), so it is
        read at most every REFERENCES seconds; storage is read every poll."""
        now = self.clock()
        if self.cached is None or now - self.cached[0] >= REFERENCES:
            self.cached = (now, self.ctx.playables.list())
        return self.cached[1]

    def inputs(self):
        """Reads every input: (clean records, inputs report)."""
        report, values = {}, {}
        try:
            values = self.ctx.storage.get()
            library = values.get('library')
        except Exception as exc:
            library = None
            report['library'] = report['kept'] = dict(state='unavailable', reason='Storage could not be read (%s).' % type(exc).__name__)
        try:
            references = self._references()
            report['references'] = dict(state='available' if references else 'absent', reason='%d saved.' % len(references) if references else 'None saved.')
        except Exception as exc:
            references, report['references'] = [], dict(state='unavailable', reason='Saved items could not be read (%s).' % type(exc).__name__)
        clean, dropped = records(library if isinstance(library, list) else [], references, values)
        kept, kept_dropped = kept_records(values)
        dropped = list((Counter(dropped) - Counter(kept_dropped)).elements())  # the library's own
        if 'kept' not in report:
            report['kept'] = dict(state='available', reason='%d kept%s.' % (len(kept), ', %d dropped (%s)' % (len(kept_dropped), ', '.join(sorted(set(kept_dropped)))) if kept_dropped else '')) \
                if kept else dict(state='absent', reason='No titles kept from playback.' if not kept_dropped else '%d dropped (%s).' % (len(kept_dropped), ', '.join(sorted(set(kept_dropped)))))
        if 'library' not in report:
            if not isinstance(library, list) or not library:
                report['library'] = dict(state='absent', reason='No library records supplied.')
            else:
                report['library'] = dict(state='available', reason='%d records%s.' % (len(library), ', %d dropped (%s)' % (len(dropped), ', '.join(sorted(set(dropped)))) if dropped else ''))
        with self.lock:
            names = sorted(self.providers)
        report['providers'] = dict(state='unavailable', reason='Not watched.') if self.watched is False else \
            dict(state='available' if names else 'absent', reason=('%s; used only for your own Find sources, never for manifests.' % ', '.join(names)) if names else 'No media provider is registered.')
        # The title playing now (its owned playback's stated identity): Information for it without keeping it.
        try:
            playing = playback_record(self.ctx.playback.current()) if getattr(self.ctx, 'playback', None) is not None else None
            report['playback'] = dict(state='available', reason='The title playing now.') if playing else \
                dict(state='absent', reason='Nothing is playing, or what plays states no title and id.')
        except Exception as exc:
            playing, report['playback'] = None, dict(state='unavailable', reason='Playback could not be read (%s).' % type(exc).__name__)
        if playing is not None:
            clean.append(playing)  # last: a kept, library or saved record of the same title keeps stating it
        report['sources'] = dict(state='absent', reason='No source observation input in this version.')
        report['chapters'] = dict(state='absent', reason='No chapter input in this version.')
        return clean, report

    def library(self, args=None):
        """The read "library" (this module's page part): its kept titles as storage holds them now, each
        with the title it joins and what else states that title. Display only; it writes nothing."""
        clean, _report = self.inputs()
        try:
            values = self.ctx.storage.get()
        except Exception:
            values = {}
        stored = {r['key']: r for r in kept_records(values)[0]}
        groups = {}
        for record in clean:
            groups.setdefault(title_id(group_key(record)), []).append(record)
        rows = []
        for record in clean:
            if record['input'] != 'kept':
                continue
            ident = title_id(group_key(record))
            members = groups[ident]
            others = Counter('kept' if r['input'] == 'kept' else r['input'] for r in members if r is not record)
            first = members[0]
            rows.append(dict(key=record['key'], media_type=record['media_type'], title=record['title'], display=record['display'],
                             year=record['year'], ids=record['ids'], title_id=ident,
                             others=dict(sorted(others.items())),  # e.g. {"library": 1, "reference": 2}
                             states_title=first is record,
                             link=read_link(values.get(LINK_PREFIX + record['key']), record['key'], stored.get(record['key']))))
        return dict(state='ready', kept=rows)

    def refresh(self):
        """One build from the inputs as they are now; True if it was published."""
        with self.lock:
            self.number += 1
            number = self.number
        clean, report = self.inputs()
        statuses = None
        if self.enrichment is not None:
            try:
                statuses = self.enrichment.statuses([r for r in clean if r['input'] in LOOKED_UP])
            except Exception as exc:  # contained: manifests publish without enrichment
                self.ctx.log('enrichment status: %s' % type(exc).__name__)
                statuses = None
            counts = Counter(s['state'] for s in (statuses or {}).values())
            report['enrichment'] = dict(state='unavailable', reason='Enrichment could not be read.') if statuses is None else \
                dict(state='available' if counts['available'] else 'absent' if not statuses else 'unavailable',
                     reason=', '.join('%d %s' % (n, s) for s, n in sorted(counts.items())) or 'No kept titles.')
        marker_statuses = None
        if self.markers is not None:
            try:
                marker_statuses = self.markers.statuses([r for r in clean if r['input'] in LOOKED_UP])
            except Exception as exc:  # contained: manifests publish without markers
                self.ctx.log('marker status: %s' % type(exc).__name__)
                marker_statuses = None
        candidate_statuses = None
        if self.chapter_candidates is not None:
            try:
                candidate_statuses = self.chapter_candidates.statuses([r for r in clean if r['input'] in LOOKED_UP])
            except Exception as exc:  # contained: manifests publish without chapter candidates
                self.ctx.log('chapter candidate status: %s' % type(exc).__name__)
                candidate_statuses = None
        edition_statuses = None
        if self.edition_candidates is not None:
            try:
                edition_statuses = self.edition_candidates.statuses([r for r in clean if r['input'] in LOOKED_UP])
            except Exception as exc:  # contained: manifests publish without edition candidates
                self.ctx.log('edition candidate status: %s' % type(exc).__name__)
                edition_statuses = None
        built = build(clean, 0, statuses, marker_statuses, candidate_statuses, edition_statuses)
        published = self.publisher.publish(number, built, report)
        if self.trace is not None:
            self._trace_markers(number, published, built)
        return published

    # TEST diagnostics only (CINEMA_TEST_TRACE=1 in the bridge's environment; otherwise None and never read). Per build
    # that changed something: the publication revision, the marker service's state and revision as this build saw them,
    # and each title's marker status, keyed by an opaque title id prefix. No ids, locators, titles or content. Bounded.
    TRACE_LIMIT = 300

    def _trace_markers(self, number, published, built):
        described = getattr(self.markers, 'described', None) if self.markers is not None else None
        titles = sorted([ident[:10], m['media_type'], (m.get('markers') or {}).get('state'), (m.get('markers') or {}).get('reason')] for ident, m in built.items())
        entry = dict(service=[described.get('state'), described.get('revision')] if described else None, titles=titles)
        with self.lock:
            last = self.trace[-1] if self.trace else None
            if last is not None and {k: last[k] for k in entry} == entry and last['revision'] == self.publisher.revision:
                return
            self.trace.append(dict(entry, t=round(self.clock(), 2), build=number, published=published, revision=self.publisher.revision))
            del self.trace[:-self.TRACE_LIMIT]

    def trace_read(self, args=None):
        with self.lock:
            return dict(state='ready', entries=json.loads(json.dumps(self.trace or [])))

    def run(self):
        while not self.ctx.stopping.is_set():
            try:
                self.refresh()
            except Exception as exc:  # contained: the previous publication stays
                self.ctx.log('build failed: %s' % type(exc).__name__)
            self.ctx.stopping.wait(POLL)


# Supplied source assets ------------------------------------------------------------------------
# Advanced Media owns which images belong to which exact bytes (cinema_source_assets). Sets are files
# packaged with this module under ui/supplied/<set>/set.json (served as its own ui files), with their
# images beside them. There is no import workflow yet: the only sets are those packaged.
#   {"schema": "cinema.source-asset-set/1",
#    "binding": {"algorithm": "sha256", "value": "<64 hex>", "bytes": N, "timeline": "player_clock"},
#    "provenance": {"producer": "...", "method": "...", "note": "..."}, "confidence": "supplied",
#    "chapters": [{"start_ms": 6000, "image": "c2.png"}], "frames": [{"at_ms": 0, "image": "f00.png"}]}
SET_SCHEMA = 'cinema.source-asset-set/1'
SET_NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')
IMAGE_NAME = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}\.(?:png|webp)')
MAX_SETS = 64


def load_sets(folder, module_id):
    """(sets, dropped reasons) from <folder>/<set>/set.json. An entry whose image is missing, of another
    type or outside its own folder is dropped; a set without a valid binding is dropped whole."""
    import cinema_source_assets as assets
    sets, dropped = [], []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return sets, dropped
    for name in names[:MAX_SETS]:
        here = os.path.join(folder, name)
        if not SET_NAME.fullmatch(name) or not os.path.isfile(os.path.join(here, 'set.json')):
            continue
        try:
            with open(os.path.join(here, 'set.json'), 'r', encoding='utf-8') as stream:
                raw = json.load(stream)
        except (OSError, ValueError):
            dropped.append('set_unreadable')
            continue
        binding = raw.get('binding') if isinstance(raw, dict) and isinstance(raw.get('binding'), dict) else {}
        provenance = raw.get('provenance') if isinstance(raw.get('provenance'), dict) else {}
        if raw.get('schema') != SET_SCHEMA or binding.get('algorithm') != 'sha256' or not isinstance(binding.get('value'), str) \
                or not assets.HASH.fullmatch(binding['value']) or type(binding.get('bytes')) is not int or binding.get('timeline') != assets.TIMELINE \
                or raw.get('confidence') != 'supplied' or not _text(provenance.get('producer')):
            dropped.append('set_invalid')
            continue

        def entries(key, time_key):
            out = []
            for row in (raw.get(key) if isinstance(raw.get(key), list) else []):
                image = row.get('image') if isinstance(row, dict) else None
                if not isinstance(image, str) or not IMAGE_NAME.fullmatch(image) or not os.path.isfile(os.path.join(here, image)) \
                        or type(row.get(time_key)) is not int:
                    dropped.append('%s_image_missing' % key[:-1])
                    continue
                out.append({time_key: row[time_key], 'image': '/x/%s/ui/supplied/%s/%s' % (module_id, name, image)})
            return out
        sets.append(dict(name=name, binding=dict(algorithm='sha256', value=binding['value'], bytes=binding['bytes'], timeline=assets.TIMELINE),
                         provenance=dict(producer=_text(provenance.get('producer'), 120), method=_text(provenance.get('method')), note=_text(provenance.get('note'))),
                         chapters=entries('chapters', 'start_ms'), frames=entries('frames', 'at_ms')))
    return sets, dropped


class SuppliedAssets(object):
    """The cinema.source-assets publisher: answers only an exact binding to its own packaged sets."""

    def __init__(self, sets, dropped, frames=None, previews=None):
        self.sets, self.dropped, self.frames, self.previews = sets, dropped, frames, previews

    def describe(self):
        import cinema_source_assets as assets
        return dict(label='Advanced Media', schema=assets.SCHEMA, revision=1, lifecycle='ready', sets=len(self.sets), previews=self.previews is not None)

    def preview(self, query):
        if self.previews is None or not isinstance(query, dict):
            return dict(state='unavailable', reason='unsupported')
        return self.previews.answer(query)

    def assets(self, query):
        identity = query.get('identity') if isinstance(query, dict) else None
        if not isinstance(identity, dict) or identity.get('coverage') != 'full':
            # No full identity: only frames extracted from the asked observation can apply.
            if isinstance(query, dict) and isinstance(query.get('observation'), dict):
                return self.extracted(query)
            return dict(state='unavailable', reason='not_full_identity')
        found = [s for s in self.sets if (s['binding']['algorithm'], s['binding']['value'], s['binding']['bytes'], s['binding']['timeline'])
                 == (identity.get('algorithm'), identity.get('value'), identity.get('bytes'), query.get('timeline'))]
        if not found:
            return self.extracted(query)  # a packaged set for these exact bytes comes first; else this playback's frames
        return self._packaged(query, identity)

    def extracted(self, query):
        seen = query.get('observation') if isinstance(query, dict) else None
        if self.frames is None or not isinstance(seen, dict):
            return dict(state='no_match')
        return self.frames.answer(seen, set(query.get('chapters') or []), query.get('timeline'))

    def _packaged(self, query, identity):
        key = (identity.get('algorithm'), identity.get('value'), identity.get('bytes'), query.get('timeline'))
        found = [s for s in self.sets if (s['binding']['algorithm'], s['binding']['value'], s['binding']['bytes'], s['binding']['timeline']) == key]
        if len(found) > 1:
            return dict(state='unavailable', reason='ambiguous')  # two sets claim the same bytes
        if not found:
            return dict(state='no_match')
        chosen, listed, duration = found[0], set(query.get('chapters') or []), query.get('duration_ms')
        inside = lambda at: type(duration) is int and 0 <= at < duration
        return dict(state='matched', binding=dict(chosen['binding']), provenance=dict(chosen['provenance']), confidence='supplied',
                    chapters=[dict(r) for r in chosen['chapters'] if r['start_ms'] in listed and inside(r['start_ms'])],
                    frames=[dict(r) for r in chosen['frames'] if inside(r['at_ms'])])


# Extracted chapter frames (automatic) ------------------------------------------------------------------
# Advanced Media owns acquiring chapter thumbnails from the video itself: for the current owned local
# observation with embedded chapters, it asks the core (capability playback.frames) for one frame per
# chapter, in order, and holds the answers for that observation only. Nothing is asked of the user and
# nothing is stored. The core does the engine work (one shared worker, its own checks); this module only
# decides what to ask for, keeps what came back, and answers cinema.source-assets for that observation.
# A progressive HTTP stream gets the same, when the core reports its chapters (source_class "progressive_http": the
# player's own input stream, read-only; the observation stays unverified for seek, edition and identity). Its frames
# come from the engine's own reader of that stream and only with matching server validators on every response
# (server evidence, not byte identity); anything less is no thumbnail.
FRAME_PRODUCER = 'Advanced Media, from this video (Kodi chapter thumbnail route)'
STREAM_PRODUCER = 'Advanced Media, from this stream (engine stream reader)'
FRAME_POLL = 0.5
FRAME_LIMIT = 32
FRAME_SCOPES = ('local_file', 'unverified')
# Lazy (2026-11-18, audit R10): frames are taken only while a consumer is asking for this observation's assets (a
# cinema.source-assets query naming it, e.g. Advanced Playlist showing its chapters); none asked within DEMAND_TTL
# seconds: nothing more is taken (what was taken stays held for that observation). No consumer: no frame at all.
DEMAND_TTL = 30.0


class Frames(object):
    def __init__(self, ctx, clock=time.monotonic):
        self.ctx, self.clock = ctx, clock
        self.lock = threading.Lock()
        self.observation = None   # (request, generation, token) being collected
        self.chapters = None      # that observation's embedded chapters (snapshot rows)
        self.got = {}             # chapter number -> {start_ms, image, at_ms, basis}
        self.done = set()         # chapter numbers answered (ready or refused)
        self.basis = None
        self.source_class = None  # 'local_file' or 'progressive_http', from that observation's chapter snapshot
        self.refused = {}         # chapter number -> the core's reason (status only)
        self.wanted, self.wanted_at = None, None   # the observation a consumer last asked about, and when (R10)

    def step(self):
        """One pass: follow the current observation, then ask for what is still missing."""
        current = self.ctx.playback.current() or {}
        live = (current.get('request'), current.get('generation'), current.get('token')) \
            if current.get('token') and current.get('source_state') == 'valid' and current.get('source_scope') in FRAME_SCOPES else None
        with self.lock:
            if live != self.observation:  # another observation (or none): what was held belonged to the last one
                self.observation, self.chapters, self.got, self.done, self.basis = live, None, {}, set(), None
                self.source_class, self.refused = None, {}
        if live is None:
            return
        with self.lock:
            demanded = self.wanted == live and self.wanted_at is not None and self.clock() - self.wanted_at <= DEMAND_TTL
        if not demanded:
            return   # nobody is asking for this observation's frames now: none are taken (R10)
        request, generation, token = live
        if self.chapters is None:
            snap = self.ctx.playback.chapters(request, generation)
            if snap.get('token') == token and snap.get('state') == 'unsupported' and current.get('source_scope') != 'local_file':
                with self.lock:  # a stream the core does not read chapters of (HLS, not seekable...): nothing to ask for it
                    if self.observation == live:
                        self.chapters, self.refused = [], {0: snap.get('reason')}
                return
            if snap.get('state') != 'ready' or snap.get('token') != token:
                return  # loading, or none: asked again next pass (embedded chapters stay the fallback)
            with self.lock:
                if self.observation == live:
                    self.chapters = list(snap.get('chapters') or [])[:FRAME_LIMIT]
                    self.source_class = snap.get('source_class') or 'local_file'
        for number in range(1, len(self.chapters or []) + 1):
            if number in self.done:
                continue
            answer = self.ctx.playback.chapter_frame(request, generation, number)
            state = answer.get('state')
            with self.lock:
                if self.observation != live:
                    return  # it moved on while asking: a late answer applies to nothing
                if state == 'ready':
                    self.got[number] = dict(start_ms=answer['start_ms'], image=answer['image'], at_ms=answer['at_ms'])
                    self.basis = answer.get('basis')
                    self.done.add(number)
                elif state == 'refused':
                    self.done.add(number)
                    self.refused[number] = answer.get('reason')
            if state in ('waiting', 'busy') or (state == 'refused' and answer.get('reason') in ('timed_out', 'not_current', 'module_unloaded')):
                return  # nothing more this pass

    def run(self):
        while not self.ctx.stopping.is_set():
            try:
                self.step()
            except Exception as exc:  # contained: the next pass starts again from the observation
                self.ctx.log('frames: %s' % type(exc).__name__)
            self.ctx.stopping.wait(FRAME_POLL)

    def answer(self, seen, listed, timeline):
        """cinema.source-assets "extracted" for the asked observation, only if it is the one held."""
        with self.lock:
            # A consumer asking about this observation is the demand that lets frames be taken for it (R10).
            asked = (seen.get('request'), seen.get('generation'), seen.get('token'))
            if isinstance(asked[0], str) and type(asked[1]) is int and isinstance(asked[2], str):
                self.wanted, self.wanted_at = asked, self.clock()
            held = self.observation
            if held is None or (seen.get('request'), seen.get('generation'), seen.get('token')) != held or not self.got:
                return dict(state='no_match')
            rows = [dict(start_ms=f['start_ms'], image=f['image']) for f in sorted(self.got.values(), key=lambda f: f['start_ms']) if f['start_ms'] in listed]
            basis, stream = self.basis, self.source_class == 'progressive_http'
        if not rows:
            return dict(state='no_match')
        note = ('Extracted automatically while this stream plays; held for this playback only. The server\'s validators matched '
                '(server evidence, not byte identity); the source is not verified.') if stream else \
            'Extracted automatically while this video plays; held for this playback only.'
        return dict(state='matched', binding=dict(kind='observation', request=held[0], generation=held[1], token=held[2], timeline=timeline),
                    provenance=dict(producer=STREAM_PRODUCER if stream else FRAME_PRODUCER, method=basis, note=note),
                    confidence='extracted', chapters=rows, frames=[])

    def status(self):
        """What this module holds for the current observation (diagnostic; no image data)."""
        with self.lock:
            return dict(observation=bool(self.observation), demanded=self.wanted == self.observation and self.observation is not None,
                        source_class=self.source_class, chapters=len(self.chapters or []),
                        ready=sorted(self.got), refused={str(k): v for k, v in self.refused.items()})


# Time-addressed scrub previews (automatic, progressive HTTP streams) ------------------------------------------
# Advanced Media owns the demand: a hovered time is asked of the engine per whole second (PREVIEW_STEP), so nearby
# hovers reuse one frame, and the core keeps one preview per observation in flight (a newer second replaces an
# older one before it runs). The frames are held in the core's frame worker for this module and this observation
# only (its bounded, least-recently-used preview cache), with the same budgets as chapter thumbnails. An answer
# says which hovered span it is for (covers), which time was asked (target_ms) and the decoded picture's own time
# (at_ms): it is never another time's frame. Chapters are not needed.
PREVIEW_STEP = 1000
PREVIEW_PRODUCER = 'Advanced Media, from this stream (engine stream reader)'
LOCAL_PREVIEW_PRODUCER = 'Advanced Media, from this video (engine local file reader)'


class Previews(object):
    def __init__(self, ctx):
        self.ctx = ctx

    def answer(self, query):
        import cinema_source_assets as assets
        seen, at, duration = query.get('observation') or {}, query.get('at_ms'), query.get('duration_ms')
        request, generation, token = seen.get('request'), seen.get('generation'), seen.get('token')
        binding = dict(kind='observation', request=request, generation=generation, token=token, timeline=assets.TIMELINE)
        current = self.ctx.playback.current() or {}
        if (current.get('request'), current.get('generation'), current.get('token')) != (request, generation, token) or current.get('source_state') != 'valid':
            return dict(state='unavailable', reason='not_current')
        target = at // PREVIEW_STEP * PREVIEW_STEP
        covers = [target, min(target + PREVIEW_STEP, duration)]
        got = self.ctx.playback.frame_at(request, generation, target)
        state = got.get('state')
        if state == 'ready':
            local = (got.get('evidence') or {}).get('coherence') == 'length_only'  # a local file (the core's own evidence class)
            return dict(state='ready', binding=binding, covers=covers, target_ms=target, at_ms=got.get('at_ms'), image=got.get('image'),
                        provenance=dict(producer=LOCAL_PREVIEW_PRODUCER if local else PREVIEW_PRODUCER, method=got.get('basis'),
                                        note=('Extracted automatically while this video plays; held for this playback only. Only the file\'s '
                                              'length was matched; the source is not verified.') if local else
                                        'Extracted automatically while this stream plays; held for this playback only. The source is not verified.'),
                        evidence=got.get('evidence'))
        if state in ('queued', 'running', 'waiting', 'busy') or (state == 'refused' and got.get('reason') == 'superseded'):
            return dict(state='pending', binding=binding, covers=covers, target_ms=target)
        return dict(state='unavailable', reason=got.get('reason') or 'unavailable')


# Discovery (user-initiated) ------------------------------------------------------------------------
# "Find sources" in Library: for one kept title, the user asks one available Media Provider Service
# implementation that advertises search (cinema_media_providers, through this module's own ctx.services
# view). Its hits are discovery candidates only: shown with their provider provenance, edition and timeline
# unknown, kept in memory for that one request, never stored, never published, never merged into a manifest,
# and with no locator or action. A search runs in tracked work (ctx.spawn); the page part starts it and polls
# it through this module's reads (each well inside the read limit). Starting another, cancelling, or the module
# ending withdraws it (Cancel / Client.close), and a late answer is discarded.
MEDIA_PROVIDERS = 'cinema.media-provider'
MAX_CANDIDATES = 20
FIND = re.compile(r'[A-Za-z0-9_-]{12,32}')
# Existing result states: what the page shows for each.
FINAL = ('found', 'empty', 'unavailable', 'unsupported', 'failed', 'cancelled')


# User-supplied chapter sets ------------------------------------------------------------------------------
# A small JSON file the user imports in Library (Import chapters…), checked here and written by the page as
# one storage key, "chapters.<key>" (key from the binding). Published through cinema.chapter-sets for the
# exact bytes it is bound to. The file:
#   {"schema": "cinema.chapter-set/1",
#    "binding": {"algorithm": "sha256", "value": "<64 hex>", "bytes": N}, "timeline": "player_clock",
#    "provenance": {"producer": "...", "method": "...", "note": "..."},
#    "chapters": [{"start_ms": 0, "title": "Opening"}, ...]}
# A set is never tied to a kept title, its ids or an edition: only to the bytes. Supplier "user", confidence
# "supplied", accuracy unverified. Bounded by the module's request and storage limits (4 KiB per write).
SET_FILE_SCHEMA = 'cinema.chapter-set/1'
CHAPTERS_PREFIX = 'chapters.'
STORED_SET_SCHEMA = 'cinema.chapter-set.stored/1'
MAX_CHAPTER_SETS = 8
MAX_IMPORT_TEXT = 3072


def check_chapter_set(raw):
    """(stored set, None) or (None, plain reason). raw: the parsed file. Unknown fields are dropped."""
    import cinema_chapter_sets as cs
    if not isinstance(raw, dict) or raw.get('schema') != SET_FILE_SCHEMA:
        return None, 'This is not a Cinema chapter set (cinema.chapter-set/1).'
    binding = raw.get('binding') if isinstance(raw.get('binding'), dict) else {}
    if binding.get('algorithm') != 'sha256' or not isinstance(binding.get('value'), str) or not cs.HASH.fullmatch(binding['value'].lower()) \
            or type(binding.get('bytes')) is not int or binding['bytes'] <= 0:
        return None, 'The set must name the file by its full SHA-256 and its size in bytes.'
    if raw.get('timeline', cs.TIMELINE) != cs.TIMELINE:
        return None, 'Times must be player-clock milliseconds.'
    provenance = raw.get('provenance') if isinstance(raw.get('provenance'), dict) else {}
    producer = cs.text(provenance.get('producer'), 120)
    if not producer:
        return None, 'The set must say who produced it (provenance.producer).'
    try:
        chapters = cs.chapter_list(raw.get('chapters'))
    except cs.Malformed:
        return None, 'Chapters must be 1 to %d entries with whole-millisecond starts in increasing order.' % cs.MAX_CHAPTERS
    value = binding['value'].lower()
    return dict(schema=STORED_SET_SCHEMA, key='cs' + value[:16], binding=dict(algorithm='sha256', value=value, bytes=binding['bytes'], timeline=cs.TIMELINE),
                provenance=dict(producer=producer, method=cs.text(provenance.get('method'), 200), note=cs.text(provenance.get('note'), 300)),
                supplier='user', confidence='supplied', accuracy='unverified', chapters=chapters), None


def stored_chapter_sets(values):
    """The usable stored sets (each validated again on every read), keyed by storage key."""
    out = {}
    for name in sorted(k for k in (values or {}) if isinstance(k, str) and k.startswith(CHAPTERS_PREFIX))[:MAX_CHAPTER_SETS]:
        raw = values[name]
        if not isinstance(raw, dict) or raw.get('schema') != STORED_SET_SCHEMA or raw.get('supplier') != 'user':
            continue
        again, _ = check_chapter_set(dict(raw, schema=SET_FILE_SCHEMA))
        if again is not None and again['key'] == name[len(CHAPTERS_PREFIX):] == raw.get('key'):
            out[name[len(CHAPTERS_PREFIX):]] = again
    return out


def chapters_check(args, values):
    """Backend read "chapters.check" {set}: the parsed file (the page reads and parses it, at most
    MAX_IMPORT_TEXT characters), checked; it writes nothing (the page writes it)."""
    raw = args.get('set') if isinstance(args, dict) else None
    if raw is None:
        return dict(state='refused', message='Choose a chapter set file.')
    found, reason = check_chapter_set(raw)
    if found is None:
        return dict(state='refused', message=reason)
    sets = stored_chapter_sets(values)
    if found['key'] not in sets and len(sets) >= MAX_CHAPTER_SETS:
        return dict(state='refused', message='At most %d chapter sets can be kept. Remove one first.' % MAX_CHAPTER_SETS)
    return dict(state='ok', key=found['key'], set=found, replaces=found['key'] in sets)


def chapters_list(values):
    """Backend read "chapters.list": what Library shows. The binding is shown as a size and a short hash."""
    return dict(state='ready', sets=[dict(key=k, producer=s['provenance']['producer'], method=s['provenance']['method'], count=len(s['chapters']),
                                          bytes=s['binding']['bytes'], short=s['binding']['value'][:12]) for k, s in stored_chapter_sets(values).items()])


class SuppliedChapters(object):
    """The cinema.chapter-sets publisher: the user's imported sets, read from storage on each query, answered
    only for an exact binding. Two sets for the same bytes cannot be stored (one key per binding)."""

    def __init__(self, ctx):
        self.ctx = ctx

    def describe(self):
        import cinema_chapter_sets as cs
        return dict(label='Advanced Media', schema=cs.SCHEMA, revision=1, lifecycle='stopping' if self.ctx.stopping.is_set() else 'ready')

    def chapters(self, query):
        identity = query.get('identity') if isinstance(query, dict) else None
        if not isinstance(identity, dict) or identity.get('coverage') != 'full':
            return dict(state='unavailable', reason='not_full_identity')
        try:
            values = self.ctx.storage.get()
        except Exception:
            return dict(state='unavailable', reason='storage')
        key = (identity.get('algorithm'), identity.get('value'), identity.get('bytes'), query.get('timeline'))
        found = [s for s in stored_chapter_sets(values).values() if (s['binding']['algorithm'], s['binding']['value'], s['binding']['bytes'], s['binding']['timeline']) == key]
        if len(found) != 1:
            return dict(state='no_match') if not found else dict(state='unavailable', reason='ambiguous')
        chosen = found[0]
        return dict(state='matched', binding=chosen['binding'], provenance=chosen['provenance'], supplier='user', confidence='supplied',
                    accuracy='unverified', edition=dict(state='unknown'), chapters=chosen['chapters'])


def candidate(item, provider):
    """A displayable discovery candidate from a normalized provider item (cinema_media_providers.item), or
    None. Plain facts with provenance; no locator, action, artwork or presentation; edition and timeline
    unknown even if the provider named an edition."""
    title = _text(item.get('title')) if isinstance(item, dict) else None
    if title is None or (item.get('kind') == 'folder' and item.get('media_type') in (None, 'unknown')):
        return None  # a folder that states no kind of media is navigation (e.g. a next page), not a title
    shown = item.get('presentation') if isinstance(item.get('presentation'), dict) else {}
    ids = {}
    for level, values in ((item.get('identity') or {}).get('ids') or {}).items():
        for scheme, value in (values.items() if isinstance(values, dict) else ()):
            name = scheme if level in ('item', 'title') else '%s.%s' % (level, scheme)
            kept = _text(value, 64)
            if KEY.fullmatch(name) and kept and len(ids) < contract.MAX_IDS:
                ids[name] = kept
    number = lambda v: v if type(v) is int and v >= 0 else None
    media = item.get('media_type') if isinstance(item.get('media_type'), str) and MEDIA.fullmatch(item['media_type']) else 'unknown'
    return dict(title=title, media_type=media, kind=item.get('kind') if item.get('kind') in ('item', 'folder') else 'item',
                year=number(item.get('year')), season=number(item.get('season')), episode=number(item.get('episode')),
                show=_text(item.get('show')), detail=_text(item.get('detail'), 300), ids=ids,
                provenance=dict(input='discovery', provider=provider['name'], provider_label=provider['label'],
                                source=_text(item.get('source'), 128), source_label=_text(shown.get('source_label'), 80)),
                edition=dict(state='unknown'), timeline=dict(state='unknown'), confidence='candidate')


# Links (user-confirmed) --------------------------------------------------------------------------------
# One per kept title, storage key "link.<kept key>", schema cinema.discovery-link/1: the user's own
# confirmation that one discovery candidate is this kept title, with the candidate's stated facts and its
# provider provenance. Module-local: the kept record (its key, title and ids) is never changed, and the link
# is never a manifest input, an id of the title, or a grouping or matching rule. A link whose kept title is
# gone or whose stated facts changed is not valid (shown so, removable). No locator, URL, artwork or account.
LINK_PREFIX = 'link.'
LINK_SCHEMA = 'cinema.discovery-link/1'
UNKNOWN = dict(state='unknown')


def stated(record):
    """What a link is bound to: the kept title's stated facts (not its display label)."""
    return dict(title=record.get('title'), media_type=record.get('media_type') or 'unknown', year=record.get('year'), ids=dict(record.get('ids') or {}))


def link_conflict(record, found):
    """A plain reason the candidate clearly is not this title, or None: a different stated kind of media, or a
    different value for the same id scheme. Anything not comparable is not a conflict (and not a match)."""
    mine, theirs = record.get('media_type') or 'unknown', found.get('media_type') or 'unknown'
    if 'unknown' not in (mine, theirs) and mine != theirs:
        return 'Not linked: this result is a %s, but “%s” is a %s.' % (theirs, record['title'], mine)
    for scheme, value in sorted((record.get('ids') or {}).items()):
        other = (found.get('ids') or {}).get(scheme)
        if other is not None and other != value:
            return 'Not linked: their %s ids differ (%s and %s).' % (scheme, value, other)
    return None


def make_link(record, found, job):
    """The minimal sanitized snapshot stored for a confirmed link."""
    number = lambda v: v if type(v) is int and v >= 0 else None
    provenance = found.get('provenance') or {}
    return dict(schema=LINK_SCHEMA, key=record['key'], confirmed='user', kept=stated(record),
                candidate=dict(title=_text(found.get('title')), media_type=found.get('media_type') if isinstance(found.get('media_type'), str) and MEDIA.fullmatch(found['media_type']) else 'unknown',
                               year=number(found.get('year')), season=number(found.get('season')), episode=number(found.get('episode')),
                               show=_text(found.get('show')), ids=_ids(found.get('ids'))),
                provenance=dict(input='discovery', provider=_text(provenance.get('provider'), 64), provider_label=_text(provenance.get('provider_label'), 80),
                                source=_text(provenance.get('source'), 128), source_label=_text(provenance.get('source_label'), 80), query=_text(job.get('query'))),
                edition=dict(UNKNOWN), release=dict(UNKNOWN), timeline=dict(UNKNOWN))


def read_link(raw, key, record):
    """A stored link as Library shows it, or None when there is none or it is unusable (then it is ignored).
    {state: valid|invalid, reason, candidate, provenance}. Fields are cleaned again on every read."""
    if not isinstance(raw, dict) or raw.get('schema') != LINK_SCHEMA or raw.get('key') != key or raw.get('confirmed') != 'user':
        return None
    candidate, provenance = raw.get('candidate') if isinstance(raw.get('candidate'), dict) else {}, raw.get('provenance') if isinstance(raw.get('provenance'), dict) else {}
    title = _text(candidate.get('title'))
    if title is None or provenance.get('input') != 'discovery':
        return None
    number = lambda v: v if type(v) is int and v >= 0 else None
    shown = dict(title=title, media_type=candidate.get('media_type') if isinstance(candidate.get('media_type'), str) and MEDIA.fullmatch(candidate['media_type']) else 'unknown',
                 year=number(candidate.get('year')), season=number(candidate.get('season')), episode=number(candidate.get('episode')),
                 show=_text(candidate.get('show')), ids=_ids(candidate.get('ids')))
    origin = dict(provider=_text(provenance.get('provider'), 64), provider_label=_text(provenance.get('provider_label'), 80),
                  source=_text(provenance.get('source'), 128), source_label=_text(provenance.get('source_label'), 80), query=_text(provenance.get('query')))
    valid = record is not None and raw.get('kept') == stated(record)
    return dict(state='valid' if valid else 'invalid', reason=None if valid else ('removed' if record is None else 'changed'),
                candidate=shown, provenance=origin, edition=dict(UNKNOWN), release=dict(UNKNOWN), timeline=dict(UNKNOWN))


class Discovery(object):
    """At most one search at a time, for one kept title. Thread-safe; nothing persisted."""

    def __init__(self, ctx, client, token=lambda: os.urandom(12).hex()):
        self.ctx, self.client, self.token = ctx, client, token
        self.lock = threading.Lock()
        self.job = None

    def _provider(self, wanted=None):
        """(provider {name, label}, None) or (None, outcome) from what is registered now."""
        try:
            listed = self.client.providers()
        except Exception:
            listed = None
        if listed is None:
            return None, dict(state='unavailable', code='provider_unavailable', message='Searching is not available right now.')
        if not listed:
            return None, dict(state='unavailable', code='provider_unavailable', message='No search service is available. Find sources needs one, such as the Kodi Provider Bridge.')
        searching = [p for p in sorted(listed, key=lambda p: p['name']) if p['descriptor']['operations']['search']['state'] == 'supported'
                     and (wanted is None or p['name'] == wanted)]
        if not searching:
            reasons = [p['descriptor']['operations']['search'].get('reason') for p in listed if p['descriptor']['operations']['search'].get('reason')]
            return None, dict(state='unsupported', code='unsupported_operation', message=reasons[0] if reasons else 'No available service offers search.')
        chosen = searching[0]
        return dict(name=chosen['name'], label=chosen['descriptor'].get('label') or chosen['name']), None

    def start(self, args, kept):
        """args {key, query?}: search for the kept title with that key. kept: {key: record} as stored now.

        Ordering rule: reservations are atomic (token, displacement and install under one lock), and the
        latest reservation is the one search: any earlier one is cancelled as it is displaced. The read
        returns at once; choosing a provider and searching happen in tracked work (_run), so a slow
        provider can never hold this read past its limit and then create a search nobody waits for."""
        key = args.get('key') if isinstance(args, dict) else None
        record = kept.get(key) if isinstance(key, str) else None
        if record is None:
            return dict(state='failed', code='invalid_request', message='This title is no longer kept.')
        query = _text(args.get('query')) if isinstance(args.get('query'), str) else None
        query = query or record['title']  # the stated title, disclosed to the user as the query
        wanted = args.get('provider') if isinstance(args.get('provider'), str) else None
        import cinema_media_providers as providers
        with self.lock:
            if self.ctx.stopping.is_set():
                return dict(state='cancelled', code='cancelled', message='Cancelled.', key=key, query=query)
            job = dict(id=self.token(), key=key, query=query, wanted=wanted, provider=None, state='searching',
                       cancel=providers.Cancel(), outcome=None, running=False, kept=stated(record))
            displaced, self.job = self.job, job
        if displaced is not None:
            displaced['cancel'].cancel()  # another title or a new search: the earlier one is withdrawn
        try:
            self.ctx.spawn(self._run, job)
        except Exception:  # the module is ending
            self._withdraw(job)
            return dict(state='cancelled', code='cancelled', message='Cancelled.', key=key, query=query)
        return self._public(job)

    def _current(self, job):
        return self.job is job and not job['cancel'].cancelled and not self.ctx.stopping.is_set()

    def _withdraw(self, job):
        with self.lock:
            if self.job is job:
                self.job = None
        job['cancel'].cancel()

    def _run(self, job):
        with self.lock:
            if not self._current(job):
                return  # cancelled, displaced or unloaded before its work began: nothing is asked
            job['running'] = True
        try:
            provider, refused = self._provider(job['wanted'])
            with self.lock:
                if not self._current(job):
                    return  # gone during a slow describe: no search is started, nothing is published
                if refused is not None:
                    job.update(state=refused['state'], outcome=refused)
                    return
                job['provider'] = provider
            answer = self.client.search(provider['name'], job['query'], cancel=job['cancel'])
            outcome = self._outcome(answer, provider)
            with self.lock:
                if self._current(job):  # cancelled, displaced or unloaded meanwhile: a late answer is discarded
                    job.update(state=outcome['state'], outcome=outcome)
        finally:
            job['running'] = False

    @staticmethod
    def _outcome(answer, provider):
        if not isinstance(answer, dict) or answer.get('status') != 'ok':
            code = (answer or {}).get('code') if isinstance(answer, dict) else 'provider_failed'
            state = {'provider_unavailable': 'unavailable', 'unsupported_operation': 'unsupported', 'cancelled': 'cancelled'}.get(code, 'failed')
            return dict(state=state, code=code or 'provider_failed', message=_text((answer or {}).get('message') if isinstance(answer, dict) else None, 300)
                        or 'The search could not be completed.')
        found = [c for c in (candidate(i, provider) for i in answer.get('items') or []) if c is not None]
        shown = found[:MAX_CANDIDATES]
        # The provider's own per-source notes (a source that failed or found nothing), as plain text.
        notes = [dict(source_label=_text(n.get('source_label'), 80), code=n.get('code'), message=_text(n.get('message'), 200))
                 for n in (answer.get('notes') or [])[:8] if isinstance(n, dict)]
        if not shown:
            return dict(state='empty', code=None, message='No results from %s.' % provider['label'], notes=notes)
        for index, entry in enumerate(shown):
            entry['cid'] = 'c%02d' % index  # stable within this one result (a result never changes once found)
        return dict(state='found', code=None, message=None, candidates=shown, more=len(found) > len(shown), notes=notes)

    # Linking: the user confirms one candidate of the CURRENT result as a link of one kept title ---------
    def confirm(self, args, kept):
        """args {request, candidate (cid), key}. {state: "ok", link} to be written by the page through its
        serialized edit path, or {state: "refused", code, message}. Writes nothing itself."""
        def no(code, message):
            return dict(state='refused', code=code, message=message)
        request = args.get('request') if isinstance(args, dict) else None
        cid = args.get('candidate') if isinstance(args, dict) else None
        key = args.get('key') if isinstance(args, dict) else None
        with self.lock:
            job = self.job
            if job is None or not self._current(job) or request != job['id'] or job['state'] != 'found':
                return no('stale', 'This search is no longer current. Find sources again.')
            if key != job['key']:
                return no('stale', 'That result belongs to another title.')
            found = next((c for c in job['outcome']['candidates'] if c['cid'] == cid), None)
            searched = job['kept']
        if found is None:
            return no('stale', 'That result is no longer listed. Find sources again.')
        record = kept.get(key)
        if record is None:
            return no('removed', 'This title is no longer kept.')
        if stated(record) != searched:
            return no('changed', 'This title changed since you searched. Find sources again.')
        conflict = link_conflict(record, found)
        if conflict:
            return no('conflict', conflict)
        return dict(state='ok', link=make_link(record, found, job))

    def _public(self, job):
        out = dict(state=job['state'], request=job['id'], key=job['key'], query=job['query'],
                   provider=dict(job['provider']) if job['provider'] else None)
        if job['outcome']:
            out.update({k: v for k, v in job['outcome'].items() if k != 'state'})
        return out

    def poll(self, args):
        """args {request}: that search's state now; another title's or an ended search is "stale"."""
        request = args.get('request') if isinstance(args, dict) else None
        with self.lock:
            job = self.job
            if job is None or not isinstance(request, str) or request != job['id']:
                return dict(state='stale', request=request if isinstance(request, str) and FIND.fullmatch(request) else None)
            return json.loads(json.dumps(self._public(job)))

    def cancel(self, args):
        request = args.get('request') if isinstance(args, dict) else None
        with self.lock:
            job = self.job
            if job is None or request != job['id']:
                return dict(state='stale', request=None)
            self.job = None
            job['cancel'].cancel()
            settling = job['running']  # its provider call is under way: the provider stops it when it can
        return dict(state='cancelled', request=job['id'], key=job['key'], code='cancelled', message='Cancelled.', settling=settling)

    def close(self):
        with self.lock:
            job, self.job = self.job, None
        if job is not None:
            job['cancel'].cancel()


class Trailers(object):
    """Trailers for the title under the user's focus or in the open card (page part: ui/main.mjs). The page gives an
    item's opaque information token; its stated identity comes from the core (capability items.identity); the lookup
    is id-only through the core's trailer service (cinema.trailers; a "trailers" plugin such as KinoCheck answers).

    The read never blocks on the network: it answers from the cache or says "pending" and leaves the newest wanted
    query for this module's worker, which asks one at a time. A newer focus replaces an older wanted query before it
    is asked (rapid navigation asks only for where the user stops). Found and no-match answers are kept for the
    session (bounded); an unavailable one is asked again after RETRY seconds. An episode is asked only by its show's
    scoped ids (tvshow.*) with its season (a provider listing season videos may answer that season's trailer); its answer
    is labelled the show's, never an episode's own trailer."""
    CACHE = 300
    RETRY = 60.0
    REASONS = {'absent': 'No trailer provider is installed.', 'disabled': 'No trailer provider is turned on.',
               'unconfigured': 'The trailer provider is on but not set up.',
               'provider_failed': 'The trailer provider could not answer; it is asked again later.'}

    def __init__(self, ctx, clock=time.monotonic):
        import cinema_trailers
        from collections import OrderedDict
        self.ctx, self.tc, self.clock = ctx, cinema_trailers, clock
        self.client = cinema_trailers.Client(ctx.services)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.cache = OrderedDict()   # query key -> (answer, at)
        self.wanted = None           # (key, query): the newest the page asked for and the cache did not hold
        self.asked = self.superseded = 0
        self.issued = []             # media session paths this module opened and has not released (bounded)

    def _query(self, identity):
        media, ids = identity.get('media_type'), identity.get('ids') or {}
        if media == 'episode':
            show = {k[len('tvshow.'):]: v for k, v in ids.items() if k.startswith('tvshow.')}
            # The episode's season too (2026-11-14): a provider listing season videos (TMDB) prefers that season's
            # trailer; others are asked for the show's, as before. Still labelled the show's (scope "show").
            season = identity.get('season')
            try:
                return self.tc.query('tvshow', show, season), 'show'
            except TypeError:  # an older core without the season argument
                return self.tc.query('tvshow', show), 'show'
        asked = self.tc.query(media, {k: v for k, v in ids.items() if '.' not in k})
        if asked is not None and set(asked['ids']) == {'mal'}:
            return asked, 'title'   # a MyAnimeList entry is one installment: its trailer is that entry's, said plainly
        return asked, 'show' if media == 'tvshow' else 'title'

    PREFER_DIRECT = 'trailers.prefer_direct'  # this module's storage key (true: a direct trailer first); default false

    def _prefer_direct(self):
        try:
            return (self.ctx.storage.get() or {}).get(self.PREFER_DIRECT) is True
        except Exception:
            return False

    SEASON_NAME = re.compile(r'\b(?:season|series|s)\s*0*(\d{1,3})\b', re.I)

    def _choose(self, answer, season=None):
        """This module's choice among the attributed trailers (the core does not choose): a direct trailer only when the
        user prefers direct trailers (storage "trailers.prefer_direct", off by default); otherwise a YouTube trailer, in
        the providers' order (KinoCheck first). For an episode (season known, 2026-11-14): first a trailer whose title names
        that season, then one that names no other season, then the first; so another season's trailer gives way. Nothing
        chosen when none applies."""
        listed = answer['trailers']
        if self._prefer_direct():
            direct = [t for t in listed if t['transport'] == 'direct' and t.get('provider')]
            if direct:
                return direct[0]
        youtube = [t for t in listed if t['transport'] == 'youtube']
        if not youtube:
            return None
        if type(season) is int:
            named = lambda t: [int(n) for n in self.SEASON_NAME.findall(t.get('title') or '')]
            same = [t for t in youtube if season in named(t)]
            if same:
                return same[0]
            neutral = [t for t in youtube if not named(t)]
            if neutral:
                return neutral[0]
        return youtube[0]

    def _public(self, answer, scope, season=None):
        if answer['state'] == 'found':
            first = self._choose(answer, season)
            if first is None:
                return dict(state='no_match', reason='No trailer this module can play is listed for this title.')
            label = first.get('provider_label') or answer['provider_label']
            if first['transport'] == 'direct':
                # The handle stays here: the page gets a session path from read "trailer.media", never the handle or a URL.
                return dict(state='found', transport='direct', container=first['container'], title=first['title'],
                            language=first['language'], provider_label=label, scope=scope)
            return dict(state='found', transport=first['transport'], video=first['video'], title=first['title'],
                        language=first['language'], provider_label=label, scope=scope)
        if answer['state'] == 'no_match':
            return dict(state='no_match', reason='No trailer is listed for this title.')
        return dict(state='unavailable', reason=self.REASONS.get(answer.get('reason'), 'Trailers are not available right now.'))

    def read(self, args):
        token = args.get('item') if isinstance(args, dict) else None
        if not isinstance(token, str) or not 8 <= len(token) <= 64:
            return dict(state='invalid')
        identity = self.ctx.items.identity(token)
        if identity is None:
            return dict(state='absent', reason='No identity is stated for this title.')
        asked, scope = self._query(identity)
        if asked is None:
            return dict(state='absent', reason='No supported id is stated for this title.')
        key = json.dumps(asked, sort_keys=True)
        with self.lock:
            held = self.cache.get(key)
            if held is not None and not (held[0]['state'] == 'unavailable' and self.clock() - held[1] >= self.RETRY):
                self.cache.move_to_end(key)
                return self._public(held[0], scope, asked.get('season'))
            if self.wanted is None or self.wanted[0] != key:
                if self.wanted is not None:
                    self.superseded += 1
                self.wanted = (key, asked)
        self.wake.set()
        return dict(state='pending')

    def media(self, args):
        """read "trailer.media" {item}: a scoped same-origin session for the chosen direct trailer of that item (capability
        media.sessions): {state: "ready", src, expires_s} or {state: "unavailable", reason}. Only from an answer already
        held (the page asks after read "trailer" said found/direct)."""
        token = args.get('item') if isinstance(args, dict) else None
        if not isinstance(token, str) or not 8 <= len(token) <= 64:
            return dict(state='invalid')
        identity = self.ctx.items.identity(token)
        asked = self._query(identity)[0] if identity else None
        if asked is None:
            return dict(state='unavailable', reason='No identity is stated for this title.')
        with self.lock:
            held = self.cache.get(json.dumps(asked, sort_keys=True))
        chosen = self._choose(held[0], asked.get('season')) if held and held[0]['state'] == 'found' else None
        if not chosen or chosen['transport'] != 'direct':
            return dict(state='unavailable', reason='No direct trailer is chosen for this title.')
        import cinema_media_sessions
        try:
            opened = self.ctx.media.open(chosen['provider'], chosen['handle'])
        except cinema_media_sessions.Refused as refused:
            return dict(state='unavailable', reason=refused.code)
        with self.lock:
            self.issued.append(opened['path'])
            del self.issued[:-16]
        return dict(state='ready', src=opened['path'], expires_s=opened['expires_s'])

    def release(self, args):
        """read "trailer.release" {src}: end a session this module opened (the player went)."""
        src = args.get('src') if isinstance(args, dict) else None
        with self.lock:
            mine = isinstance(src, str) and src in self.issued
            if mine:
                self.issued.remove(src)
        return dict(released=bool(mine and self.ctx.media.revoke(src)))

    def step(self):
        """Ask for the newest wanted query (blocking this module's worker only). False when there was nothing."""
        with self.lock:
            job = self.wanted
        if job is None:
            return False
        key, asked = job
        publishers = self.client.publishers()
        answer = self.client.lookup(publishers[0]['name'], asked) if publishers else self.tc.unavailable('absent')
        with self.lock:
            self.asked += 1
            self.cache[key] = (answer, self.clock())
            self.cache.move_to_end(key)
            while len(self.cache) > self.CACHE:
                self.cache.popitem(last=False)
            if self.wanted == job:
                self.wanted = None
        return True

    def run(self):
        while not self.ctx.stopping.is_set():
            try:
                if not self.step():
                    self.wake.wait(1.0)
                    self.wake.clear()
            except Exception as exc:  # contained: this module's consumer ended, or anything else
                self.ctx.log('trailers: %s' % type(exc).__name__)
                return

    def trace(self, args=None):
        with self.lock:
            return dict(asked=self.asked, superseded=self.superseded, cached=len(self.cache), wanted=self.wanted is not None)


instance = None
discovery = None
frames = None
trailers = None


def start(ctx):
    global instance
    instance = Instance(ctx)
    instance.enrichment = Enrichment(ctx)
    instance.markers = Markers(ctx)
    instance.chapter_candidates = ChapterCandidates(ctx)
    instance.edition_candidates = EditionCandidates(ctx)
    ctx.services.provide(contract.CONTRACT, contract.MAJOR, NAME, instance.publisher)
    import cinema_source_assets as assets
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui', 'supplied')
    sets, dropped = load_sets(folder, ctx.id) if os.path.isdir(folder) else ([], [])
    global frames
    # The context's public capability check (2026-11-17 modularity audit; was its private set). An older host without
    # has(): assume declared, as before. Every capability tested below must be asked here: bf asked only
    # playback.frames, so on a real host "trailer" was never offered and no trailer showed (found natively, bj).
    CHECKED = ('playback.frames', 'items.identity', 'media.sessions')
    caps = None if not callable(getattr(ctx, 'has', None)) else {c for c in CHECKED if ctx.has(c)}
    frames = Frames(ctx) if (caps is None or 'playback.frames' in caps) and callable(getattr(getattr(ctx, 'playback', None), 'chapter_frame', None)) else None
    previews = Previews(ctx) if (caps is None or 'playback.frames' in caps) and callable(getattr(getattr(ctx, 'playback', None), 'frame_at', None)) else None
    if sets or frames is not None or previews is not None:
        ctx.services.provide(assets.CONTRACT, assets.MAJOR, NAME, SuppliedAssets(sets, dropped, frames, previews))
    if dropped:
        ctx.log('supplied assets: %d entries dropped' % len(dropped))
    import cinema_chapter_sets as chapter_sets
    ctx.services.provide(chapter_sets.CONTRACT, chapter_sets.MAJOR, NAME, SuppliedChapters(ctx))
    instance.watch()
    global discovery
    import cinema_media_providers as providers
    client = providers.Client(ctx.services)
    discovery = Discovery(ctx, client)
    if getattr(ctx, 'reads', None) is not None:
        ctx.reads.provide('library', instance.library)
        ctx.reads.provide('find', lambda args: discovery.start(args, {r['key']: r for r in kept_records(ctx.storage.get())[0]}))
        ctx.reads.provide('find.poll', discovery.poll)
        ctx.reads.provide('find.cancel', discovery.cancel)
        ctx.reads.provide('find.confirm', lambda args: discovery.confirm(args, {r['key']: r for r in kept_records(ctx.storage.get())[0]}))
        ctx.reads.provide('chapters.check', lambda args: chapters_check(args, ctx.storage.get()))
        ctx.reads.provide('chapters.list', lambda args: chapters_list(ctx.storage.get()))
        if caps is None or 'items.identity' in caps:
            global trailers
            trailers = Trailers(ctx)
            ctx.reads.provide('trailer', trailers.read)
            if caps is None or 'media.sessions' in caps:
                ctx.reads.provide('trailer.media', trailers.media)
                ctx.reads.provide('trailer.release', trailers.release)
            ctx.spawn(trailers.run)
        if instance.trace is not None:  # TEST diagnostics only: absent unless CINEMA_TEST_TRACE=1
            ctx.reads.provide('trace.markers', instance.trace_read)
            if trailers is not None:
                ctx.reads.provide('trace.trailers', trailers.trace)
    ctx.spawn(instance.run)
    ctx.spawn(instance.enrichment.run)
    ctx.spawn(instance.markers.run)
    ctx.spawn(instance.chapter_candidates.run)
    ctx.spawn(instance.edition_candidates.run)
    if frames is not None:
        ctx.spawn(frames.run)


def stop():
    if instance is not None:
        instance.publisher.stopping()
    if discovery is not None:
        discovery.close()
        discovery.client.close()  # its calls in flight are withdrawn; nobody else's
