"""Advanced Playlist's optional backend: curated information for the current playback.

It reads published curated manifests (cinema.curated-manifest) through the core consumer Client
over its own ctx.services, and answers one named read for its own page part ("information").
It imports no other module's code and nothing platform-specific.

Matching is by stated identity only. The playing request's identity comes from this backend's own
ctx.playback.current(), never from the page. A manifest is a candidate only when:
- its media_type equals the stated one (neither unknown);
- they share at least one (scheme, value) id;
- no scheme present on both has different values;
- for an episode, both state a usable season and episode (manifests.episode_position: season
  0 allowed, episode from 1, no booleans) and they are the same: the manifest's stated attributes,
  never enrichment facts. Without one there is no match: a series id alone is not an episode.
Exactly one candidate across every publisher is a match; more is ambiguous, so no curated
information. A scan that did not cover every title of every publisher (the SCAN bound, a failed
listing) proves no uniqueness, so it answers "unavailable" even with one candidate. The final
manifest is checked again (same revision as indexed, type and ids still agreeing) rather than
trusting the index. Title text, runtime, file names, observation generations, tokens and editions are
never used. A match says which title this is, not which release or timeline plays: nothing here
is used to seek. A manifest's optional associations (a user-confirmed link to another description of
the title) are shown as attributed information only; their ids are never matched on.

Per publisher, a small index (title id -> media type and ids) is rebuilt only when that
publisher's revision changes, or when the contract's availability changes (watched). At most
SCAN titles are read; past that the answer is "unavailable" rather than a guess.
"""
import threading
import time

import cinema_curated_manifests as manifests

SCAN = 500
ATTRIBUTES = 6
ASSOCIATIONS = 2


def stated(identity):
    """(media type, {scheme: value}) of a playback's stated identity; (None, {}) when unusable."""
    if not isinstance(identity, dict):
        return None, {}
    media = identity.get('media_type')
    media = media if isinstance(media, str) and manifests.MEDIA.fullmatch(media) and media != 'unknown' else None
    ids = {}
    for scheme, value in (identity.get('ids') or {}).items() if isinstance(identity.get('ids'), dict) else ():
        scheme = str(scheme).lower()
        value = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ''
        if manifests.KEY.fullmatch(scheme) and value and value.lower() not in ('none', 'null', '0'):
            ids[scheme] = value
    return media, ids


def position(identity):
    """An episode playback's usable stated (season, episode) (manifests.episode_position), or None."""
    identity = identity if isinstance(identity, dict) else {}
    return manifests.episode_position(identity.get('season'), identity.get('episode'))


def manifest_position(manifest):
    """A manifest's usable stated (season, episode) attributes, or None. Enrichment facts are never identity."""
    def stated(name):
        fact = manifest['attributes'].get(name)
        return fact['value'] if isinstance(fact, dict) and fact['provenance']['input'] != 'enrichment' else None
    return manifests.episode_position(stated('season'), stated('episode'))


MALFORMED_SCOPE = ('malformed',)


def numbering_scope(parent):
    """What an episode's position is relative to: None when no parent fact is stated (numbering unstated), the
    attributed parent's (numbering, {scheme: value}) when asserted, MALFORMED_SCOPE when one is stated but unusable.
    Accepts a playback identity's parent (asserted_parent) or a manifest's (manifest_parent, with provenance)."""
    if parent is None:
        return None
    if not isinstance(parent, dict) or parent.get('state') != 'asserted' or not isinstance(parent.get('ids'), dict) or not parent['ids']:
        return MALFORMED_SCOPE
    return (parent.get('numbering'), dict(parent['ids']))


def agrees(media, ids, entry_media, entry_ids, at=None, entry_at=None, scope=None, entry_scope=None):
    """Exact identity agreement: same type, a shared id, and no conflicting scheme. An episode also needs a
    usable stated position on BOTH sides, and the same one: its id may be its series', so a series id alone
    (no position, a partial one or an unusable one) never identifies an episode. And a position means something
    only in its numbering, so the numbering scope is a separator beside the raw ids (never a replacement): both
    sides state none (as before), or both the SAME asserted numbering with the same parent ids. An unusable
    assertion agrees with nothing."""
    if not media or media != entry_media or not ids or not entry_ids:
        return False
    if media == 'episode':
        if at is None or entry_at is None or tuple(at) != tuple(entry_at):
            return False
        if scope == MALFORMED_SCOPE or entry_scope == MALFORMED_SCOPE or scope != entry_scope:
            return False
    shared = [s for s in ids if s in entry_ids]
    return bool(shared) and all(ids[s] == entry_ids[s] for s in shared)


def present(manifest, label):
    """What the page shows: plain facts, already normalized by the contract."""
    value = lambda fact: fact['value'] if isinstance(fact, dict) else None
    attributes = [[name, str(value(fact))] for name, fact in list(manifest['attributes'].items())[:ATTRIBUTES]]
    edition = manifest['edition']
    return dict(title=value(manifest['title']), year=value(manifest['year']), media_type=manifest['media_type'], attributes=attributes,
                edition=dict(state=edition['state'], label=value(edition.get('label')) if edition['state'] == 'stated' else None),
                sources=manifest['sources']['state'], timeline=manifest['timeline']['state'], chapters=manifest['chapters']['state'],
                inputs=list(manifest['inputs']), source_label=label, associations=associations(manifest),
                enrichment=dict(manifest['enrichment']) if manifest.get('enrichment') else None,
                # A listing's descriptive facts, as the curator published them (only fields nothing better states), with
                # who stated them: information only, never matched on (matching above uses ids and position only).
                description=description(manifest.get('description')),
                # How the title can be shown (artwork handles, cast, genres, runtime, tagline), attributed: display only.
                presentation=presentation(manifest.get('presentation')),
                # Title-level marker candidates, as published (binding "title", timeline "unverified"): information only.
                markers=dict(manifest['markers'], items=[dict(m) for m in manifest['markers']['items']]) if manifest.get('markers') else None,
                # Title-level chapter SET candidates, as published, each set whole: information only.
                chapter_candidates=dict(manifest['chapter_candidates'], sets=[dict(s, chapters=[dict(c) for c in s['chapters']])
                                                                             for s in manifest['chapter_candidates']['sets']])
                if manifest.get('chapter_candidates') else None,
                # Title-level EDITION candidates, as published; provider refs and fingerprint schemes stay in the backend.
                edition_candidates=dict({k: v for k, v in manifest['edition_candidates'].items() if k != 'editions'},
                                        editions=[dict(version_label=e['version_label'], edition_label=e['edition_label'], duration_ms=e['duration_ms'],
                                                       frame_rate=e['frame_rate'], points=[dict(p) for p in e['points']],
                                                       points_complete=e.get('points_complete'), unlabelled_points=e.get('unlabelled_points', 0))
                                                  for e in manifest['edition_candidates']['editions']])
                if manifest.get('edition_candidates') else None)


def presentation(value):
    """A manifest's presentation for the page, as the contract normalized it, with its provider label; or None."""
    if not isinstance(value, dict):
        return None
    out = {k: v for k, v in value.items() if k != 'provenance'}
    out['cast'] = [dict(p) for p in value.get('cast', [])]
    out['genres'] = list(value.get('genres', []))
    out['provider_label'] = value['provenance']['provider_label']
    return out


def description(value):
    """A manifest's listing description for the page: {producer_label, version, origin, facts, truncated}, or None."""
    if not isinstance(value, dict) or not value.get('facts'):
        return None
    producer = value['producer']
    return dict(origin=value['origin'], producer_label=producer['name'], version=producer['version'], facts=dict(value['facts']),
                truncated=list(value['truncated']))


def associations(manifest):
    """The manifest's user-confirmed associations as attributed, read-only facts for the page. Information
    only: never matched on (matching uses the title's own ids, above), never used to play, seek or preview.
    A manifest without the optional field has none."""
    out = []
    for entry in (manifest.get('associations') or [])[:ASSOCIATIONS]:
        found, origin = entry['candidate'], entry['provenance']
        out.append(dict(kind=entry['kind'], confirmed=entry['confirmed'], title=found['title'], year=found['year'], media_type=found['media_type'],
                        provider_label=origin['provider_label'], source_label=origin['source_label'],
                        edition=entry['edition']['state'], release=entry['release']['state'], timeline=entry['timeline']['state']))
    return out


class Information(object):
    def __init__(self, ctx):
        self.ctx = ctx
        self.client = manifests.Client(ctx.services)
        self.lock = threading.Lock()
        self.indexes = {}  # (publisher, owner) -> (revision, [(title_id, media, ids)], truncated)

    def forget(self, event=None):
        with self.lock:
            self.indexes.clear()

    def _index(self, publisher):
        key, revision = (publisher['name'], publisher['owner']), publisher['description']['revision']
        with self.lock:
            cached = self.indexes.get(key)
        if cached and cached[0] == revision:
            return cached
        listing = self.client.titles(publisher['name'])
        if listing.get('status') != 'ok':
            return None
        entries, truncated = [], len(listing['titles']) > SCAN
        for title in listing['titles'][:SCAN]:
            answer = self.client.manifest(publisher['name'], title['title_id'])
            if answer.get('status') == 'ok':
                m = answer['manifest']
                entries.append((m['title_id'], m['media_type'], {i['scheme']: i['fact']['value'] for i in m['ids']}, manifest_position(m), numbering_scope(m.get('parent'))))
        built = (revision, entries, truncated)
        with self.lock:
            self.indexes[key] = built
        return built

    def read(self, args):
        request = args.get('request') if isinstance(args, dict) else None
        if not isinstance(request, str) or not request:
            return dict(state='invalid')
        current = self.ctx.playback.current()
        if not current or current.get('request') != request:
            return dict(state='stale', request=request)
        media, ids = stated(current.get('identity'))
        at = position(current.get('identity'))
        scope = numbering_scope((current.get('identity') or {}).get('parent') if isinstance(current.get('identity'), dict) else None)
        if not media or not ids:
            return dict(state='no_identity', request=request)
        publishers = self.client.publishers()
        if not publishers:
            return dict(state='unavailable', request=request)
        found, incomplete = [], False
        for publisher in publishers:
            index = self._index(publisher)
            if index is None:
                incomplete = True
                continue
            incomplete = incomplete or index[2]
            found.extend((publisher, title_id) for title_id, entry_media, entry_ids, entry_at, entry_scope in index[1]
                         if agrees(media, ids, entry_media, entry_ids, at, entry_at, scope, entry_scope))
        if len(found) > 1:
            return dict(state='ambiguous', request=request)
        if incomplete:
            # An unscanned title or publisher could hold another candidate: one found is not unique.
            return dict(state='unavailable', request=request)
        if not found:
            return dict(state='no_match', request=request)
        publisher, title_id = found[0]
        answer = self.client.manifest(publisher['name'], title_id)
        if answer.get('status') != 'ok':
            return dict(state='unavailable', request=request)
        m = answer['manifest']
        # The index may be stale: the manifest itself must still agree, from the revision indexed.
        if m['revision'] != publisher['description']['revision'] or not agrees(media, ids, m['media_type'], {i['scheme']: i['fact']['value'] for i in m['ids']}, at, manifest_position(m),
                                                                                    scope, numbering_scope(m.get('parent'))):
            self.forget()
            return dict(state='unavailable', request=request)
        return dict(state='matched', request=request, info=present(m, publisher['description']['label']))


class Assets(object):
    """Supplied images for the current observation's exact bytes (cinema.source-assets). This module
    binds them to the playback: it asks for the full-file identity (playback.source_identity) once per
    observation, reuses it, never overlaps, and backs off after "unavailable"; it then queries every
    publisher through the core Client and checks the observation again before answering. More than
    one publisher claiming the same bytes is ambiguous: nothing is shown."""
    BACKOFF = 10.0

    def __init__(self, ctx, clock=time.monotonic):
        import cinema_source_assets as assets
        self.ctx, self.clock, self.contract = ctx, clock, assets
        self.client = assets.Client(ctx.services)
        self.lock = threading.Lock()
        self.identities = {}  # (request, generation, token) -> (identity or None, when): this observation only
        self.reading = set()

    def forget(self, event=None):
        with self.lock:
            self.identities.clear()

    def _current(self, request, generation, token, scopes=('local_file',)):
        now = self.ctx.playback.current()
        return bool(now and now.get('request') == request and now.get('generation') == generation and now.get('token') == token
                    and now.get('source_state') == 'valid' and now.get('source_scope') in scopes)

    def _scope(self, request, generation, token):
        now = self.ctx.playback.current() or {}
        same = now.get('request') == request and now.get('generation') == generation and now.get('token') == token
        return now.get('source_scope') if same and now.get('source_state') == 'valid' else None

    def _identity(self, key):
        with self.lock:
            known = self.identities.get(key)
            if known and (known[0] is not None or self.clock() - known[1] < self.BACKOFF):
                return known[0], None
            if key in self.reading:
                return None, 'busy'  # one read per observation at a time
            self.reading.add(key)
        try:
            answer = self.ctx.playback.source_identity(key[0], key[1])
            identity = answer.get('identity') if answer.get('state') == 'available' and (answer.get('identity') or {}).get('coverage') == 'full' else None
            with self.lock:
                self.identities = {k: v for k, v in self.identities.items() if k[0] == key[0]}  # only this request's
                self.identities[key] = (identity, self.clock())
            return identity, None if identity else (answer.get('reason') or 'unavailable')
        finally:
            with self.lock:
                self.reading.discard(key)

    def read(self, args):
        args = args if isinstance(args, dict) else {}
        request, generation, token = args.get('request'), args.get('generation'), args.get('token')
        if not isinstance(request, str) or type(generation) is not int or not isinstance(token, str):
            return dict(state='invalid')
        # An unverified observation (a stream) has no full identity, and none is asked for: only frames extracted from
        # that very observation can apply (its publisher answers for it, or nothing does).
        scope = self._scope(request, generation, token)
        if scope not in ('local_file', 'unverified'):
            return dict(state='stale', request=request)
        scopes = (scope,)
        if scope == 'local_file':
            identity, reason = self._identity((request, generation, token))
        else:
            identity, reason = None, 'source_unverified'
        if identity is None and reason == 'busy':
            return dict(state='unavailable', reason='busy', request=request)
        # Supplied sets need the full identity; frames extracted from this observation need only the
        # observation itself (so a file too large to hash still gets them).
        asked = self.contract.query(identity, args.get('duration_ms'), args.get('chapters'), dict(request=request, generation=generation, token=token))
        if asked is None:
            return dict(state='invalid', request=request)
        matched = []
        for publisher in self.client.publishers():
            answer = self.client.assets(publisher['name'], asked)
            if answer.get('state') == 'matched' and (scope == 'local_file' or answer.get('confidence') == 'extracted'):
                matched.append(answer)
        if not self._current(request, generation, token, scopes):
            return dict(state='stale', request=request)  # the video moved on while asking
        if len(matched) > 1:
            return dict(state='ambiguous', request=request)
        if not matched:
            if identity is None:  # as before: without a full identity and no extracted frames, say why
                return dict(state='unavailable', reason=reason or 'no_identity', request=request)
            return dict(state='no_match', request=request)
        m = matched[0]
        # The page gets images, times and provenance; never the hash.
        return dict(state='matched', request=request, generation=generation, token=token, chapters=m['chapters'], frames=m['frames'],
                    provenance=m['provenance'], confidence=m['confidence'], timeline=m['binding']['timeline'])



class SuppliedChapters(object):
    """A chapter set a user supplied for the current observation's exact bytes (cinema.chapter-sets). The
    full-file identity is the one Assets already asks for (once per observation, same limits and backoff);
    the observation is checked before and after asking every publisher. More than one publisher claiming
    the bytes is ambiguous: nothing. Read-only: nothing here seeks, skips or names a title or edition."""

    def __init__(self, ctx, assets):
        import cinema_chapter_sets as sets
        self.ctx, self.assets, self.contract = ctx, assets, sets
        self.client = sets.Client(ctx.services)

    def read(self, args):
        args = args if isinstance(args, dict) else {}
        request, generation, token = args.get('request'), args.get('generation'), args.get('token')
        if not isinstance(request, str) or type(generation) is not int or not isinstance(token, str):
            return dict(state='invalid')
        if not self.assets._current(request, generation, token):
            return dict(state='stale', request=request)
        identity, reason = self.assets._identity((request, generation, token))
        if identity is None:
            return dict(state='unavailable', reason=reason or 'no_identity', request=request)
        asked = self.contract.query(identity, args.get('duration_ms'))
        if asked is None:
            return dict(state='invalid', request=request)
        matched, failed = [], False
        for publisher in self.client.publishers():
            answer = self.client.chapters(publisher['name'], asked)
            if answer.get('state') == 'matched':
                matched.append(answer)
            elif answer.get('reason') == 'ambiguous':
                failed = True
        if not self.assets._current(request, generation, token):
            return dict(state='stale', request=request)  # the video moved on while asking
        if len(matched) > 1 or (failed and matched):
            return dict(state='ambiguous', request=request)
        if not matched:
            return dict(state='ambiguous' if failed else 'no_match', request=request)
        m = matched[0]
        # The page gets the chapters, the supplier's words and the explicit limits; never the hash.
        return dict(state='matched', request=request, generation=generation, token=token, chapters=m['chapters'], provenance=m['provenance'],
                    supplier=m['supplier'], confidence=m['confidence'], accuracy=m['accuracy'], edition=m['edition'])


class Preview(object):
    """A time-addressed scrub preview for the current observation (cinema.source-assets preview, published by
    Advanced Media). Read-only: the observation is checked before and after asking; the answer names the hovered
    span it is for and the decoded picture's own time. Nothing here seeks or plays."""

    def __init__(self, ctx, assets):
        import cinema_source_assets as contract
        self.ctx, self.assets, self.contract = ctx, assets, contract
        self.client = contract.Client(ctx.services)

    def read(self, args):
        args = args if isinstance(args, dict) else {}
        request, generation, token = args.get('request'), args.get('generation'), args.get('token')
        asked = self.contract.preview_query(dict(request=request, generation=generation, token=token), args.get('duration_ms'), args.get('at_ms'))
        if asked is None:
            return dict(state='invalid')
        if self.assets._scope(request, generation, token) not in ('local_file', 'unverified'):
            return dict(state='stale', request=request)
        answer = None
        for publisher in self.client.publishers():
            got = self.client.preview(publisher['name'], asked)
            if got['state'] != 'unavailable' or answer is None:
                answer = got
            if got['state'] != 'unavailable':
                break
        if self.assets._scope(request, generation, token) not in ('local_file', 'unverified'):
            return dict(state='stale', request=request)  # the video moved on while asking
        if answer is None:
            return dict(state='unavailable', reason='no_publisher', request=request)
        return dict(answer, request=request, generation=generation, token=token)


def start(ctx):
    information = Information(ctx)
    ctx.services.watch(manifests.CONTRACT, information.forget)  # a publisher came or went: rebuild
    ctx.reads.provide('information', information.read)
    import cinema_source_assets
    supplied = Assets(ctx)
    ctx.services.watch(cinema_source_assets.CONTRACT, supplied.forget)
    ctx.reads.provide('assets', supplied.read)
    ctx.reads.provide('supplied_chapters', SuppliedChapters(ctx, supplied).read)
    ctx.reads.provide('preview', Preview(ctx, supplied).read)
