"""Cadrame edition-candidate provider (a Cinema metadata plugin that "provides": "editions").

Read API (public GETs; CC0 data; anonymous reads and caching permitted per cadrame.org/data), as observed in the saved
research of 2026-11-11 (..\\outputs\\Cadrame-research-2026-11-11: RESEARCH.md, FOLLOW-UP.md, raw JSON):
    GET https://cadrame.org/api/v1/ids/imdb_title_id/<tt>   -> {resultats: [{id, sorte, categorie, rev, affichage}]}
    GET https://cadrame.org/api/v1/entites/<id>             -> an entity record ("format": "cadrame-fiche-1")
VERIFIED in saved actual responses:
    work   {id, sorte: "oeuvre", versions: [], affichage: {titre, langue}, affirmations, anciens}  (every saved work had
           versions: [], e.g. Blade Runner)
    edition {id, sorte: "edition", oeuvre: <work id>, version: <version id>, duree_ms, cadence: "24000/1001",
            fichiers: [{duree_ms, cadence, confiance, empreintes: {oshash, phash-stash-1}}],
            affirmations: {tag: [{id, rang, valeur: {entite}, edition?, debut_ms?, sources?, heritee_de?}]}}
            (a TIMED tag carries edition == this edition and debut_ms; a tag without them is an inherited, untimed
            title tag and is NOT a timed point)
VERIFIED LATER (user-authorized public research, saved positive-work.json, positive-version.json, timed-concept.json):
    a populated work.versions[]: version ids as UUID strings
    a version record: {id, sorte: "version", oeuvre: <work id>, editions: [{id, cadence, duree_ms}], affichage, ...}
    a concept record: {id, sorte: "concept", affichage: {titre, langue}, affirmations: {name, broader}}
    (the narrowest reading here, an id as a string or {id}, parses them; the edition back-references still guard.)
    A version's and edition's affichage.titre is the WORK's title in actual records: it is not a cut or edition name
    and is not used as one (only an own, different title would be).
Rules (provider-specific): an exact IMDb id only (no title or fuzzy search); exactly one "oeuvre" result, else
refused (ambiguous); every fetched record must echo its own id and kind (a redirect, a parent or another id does
not establish the queried record); version and edition back-references must name the queried work and version; a
record seen twice is a cycle (refused); fixed request, version, edition, point and concept budgets: a budget
reached stops traversal and the result says it is partial (never claimed complete). A 404 is no match; any other
failure raises (retried later by the registry). URLs, fingerprints and refs never leave the backend contracts.
"""
import re
from collections import OrderedDict

BASE = 'https://cadrame.org/api/v1'
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
IMDB = re.compile(r'tt[0-9]{7,9}')
CADENCE = re.compile(r'([1-9][0-9]{0,6})/([1-9][0-9]{0,6})')
SCHEMES = ('oshash', 'phash-stash-1')
HEX16 = re.compile(r'[0-9a-f]{16}')
MAX_REQUESTS, MAX_VERSIONS, MAX_EDITIONS, MAX_POINTS, MAX_CONCEPTS, MAX_FILES = 40, 6, 8, 64, 32, 4
MAX_MS = 48 * 3600 * 1000
_labels = OrderedDict()  # concept id -> label or None (bounded, per session)
LABEL_CACHE = 256


class Refused(Exception):
    """The provider's records do not establish the query (ambiguous, redirected, wrong back-reference, cycle)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Budget(Exception):
    pass


class Walk(object):
    def __init__(self, fetch):
        self.fetch, self.requests, self.seen = fetch, 0, set()

    def get(self, path):
        if self.requests >= MAX_REQUESTS:
            raise Budget()
        self.requests += 1
        try:
            return self.fetch(BASE + path)
        except Exception as exc:
            if getattr(exc, 'code', None) == 404:
                return None
            raise

    def entity(self, ident, sorte):
        if not (isinstance(ident, str) and UUID.fullmatch(ident)):
            raise Refused('malformed_ref')
        if ident in self.seen:
            raise Refused('cycle')
        self.seen.add(ident)
        data = self.get('/entites/' + ident)
        if data is None:
            return None
        if not isinstance(data, dict) or data.get('id') != ident or data.get('sorte') != sorte:
            raise Refused('not_this_record')  # a redirect, a parent or another kind does not establish this record
        return data


def _ref(raw):
    """A link element in its narrowest form: an id string, or {id}."""
    value = raw.get('id') if isinstance(raw, dict) else raw
    return value if isinstance(value, str) and UUID.fullmatch(value) else None


def _label(record):
    shown = record.get('affichage') if isinstance(record, dict) else None
    title = shown.get('titre') if isinstance(shown, dict) else None
    return title if isinstance(title, str) and title.strip() else None


def _own_label(record, work):
    """A version's or edition's display title only when it is its OWN: in actual records a version shows the work's
    title (affichage.titre inherited), which is not a cut or edition name and is never presented as one."""
    label = _label(record)
    return label if label and label != _label(work) else None


def _cadence(value):
    match = CADENCE.fullmatch(value) if isinstance(value, str) else None
    return value if match else None


def _ms(value, high=MAX_MS):
    return value if type(value) is int and 0 <= value <= high else None


def _files(raw):
    """(files, complete): complete False when the edition lists more file representations than are read."""
    listed = raw if isinstance(raw, list) else []
    out = []
    for f in listed[:MAX_FILES]:
        if not isinstance(f, dict):
            continue
        prints = f.get('empreintes') if isinstance(f.get('empreintes'), dict) else {}
        keys = [dict(scheme=s, value=prints[s]) for s in SCHEMES if isinstance(prints.get(s), str) and HEX16.fullmatch(prints[s])]
        confidence = f.get('confiance')
        out.append(dict(keys=keys, duration_ms=_ms(f.get('duree_ms')), frame_rate=_cadence(f.get('cadence')),
                        provider_confidence=float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and 0 <= confidence <= 1 else None))
    return out, len(listed) <= MAX_FILES


def _points(edition, walk):
    """(points, complete): every edition-scoped point inside the stated bounds, up to MAX_POINTS. complete is False
    when more such points exist than are kept (truncated coverage). A point whose concept label could not be read
    (unknown record, budget) keeps its point with label null: a missing label is not missing coverage."""
    duration = _ms(edition.get('duree_ms'))
    tags = (edition.get('affirmations') or {}).get('tag') if isinstance(edition.get('affirmations'), dict) else None
    qualifying = []
    for tag in (tags if isinstance(tags, list) else []):
        if not isinstance(tag, dict) or tag.get('edition') != edition['id']:
            continue  # inherited or untimed title tags are not timed points
        at = _ms(tag.get('debut_ms'))
        if at is None or duration is None or at > duration:
            continue  # only explicitly edition-scoped starts within this edition's stated bounds
        qualifying.append((at, tag))
    out = []
    for at, tag in qualifying[:MAX_POINTS]:
        concept = (tag.get('valeur') or {}).get('entite') if isinstance(tag.get('valeur'), dict) else None
        concept = concept if isinstance(concept, str) and UUID.fullmatch(concept) else None
        sources = [s.get('sorte') for s in (tag.get('sources') or []) if isinstance(s, dict) and isinstance(s.get('sorte'), str)][:4]
        out.append(dict(at_ms=at, concept_ref=concept, concept_label=_concept(concept, walk) if concept else None,
                        rank=tag.get('rang') if isinstance(tag.get('rang'), str) else None, provenance=sources))
    return out, len(qualifying) <= MAX_POINTS


def _concept(ident, walk):
    if ident in _labels:
        _labels.move_to_end(ident)
        return _labels[ident]
    if walk.concepts >= MAX_CONCEPTS:
        return None  # unresolved: an opaque concept, never an invented name
    walk.concepts += 1
    try:
        record = walk.get('/entites/' + ident)
    except Budget:
        return None
    label = _label(record) if isinstance(record, dict) and record.get('id') == ident and record.get('sorte') == 'concept' else None
    _labels[ident] = label
    while len(_labels) > LABEL_CACHE:
        _labels.popitem(last=False)
    return label


def lookup(identity, settings, fetch):
    if identity.get('media_type') != 'movie':
        return None
    imdb = (identity.get('ids') or {}).get('imdb')
    if not (isinstance(imdb, str) and IMDB.fullmatch(imdb)):
        return None
    walk = Walk(fetch)
    walk.concepts = 0
    try:
        found = walk.get('/ids/imdb_title_id/' + imdb)
        if found is None:
            return None
        results = found.get('resultats') if isinstance(found, dict) else None
        if not isinstance(results, list):
            raise ValueError('unexpected lookup')
        works = [r for r in results if isinstance(r, dict)]
        if not works:
            return None
        if len(works) != 1 or works[0].get('sorte') != 'oeuvre':
            return dict(refused='ambiguous_id')
        work = walk.entity(works[0].get('id'), 'oeuvre')
        if work is None:
            return None
        listed = work.get('versions')
        if not isinstance(listed, list):
            return dict(refused='malformed_container')
        editions, complete = [], len(listed) <= MAX_VERSIONS
        try:
            for element in listed[:MAX_VERSIONS]:
                vid = _ref(element)
                if vid is None:
                    return dict(refused='malformed_ref')
                version = walk.entity(vid, 'version')
                if version is None:
                    complete = False
                    continue
                if version.get('oeuvre') != work['id']:
                    return dict(refused='wrong_back_reference')
                children = version.get('editions')
                if not isinstance(children, list):
                    return dict(refused='malformed_container')
                for child in children:
                    if len(editions) >= MAX_EDITIONS:
                        complete = False
                        break
                    eid = _ref(child)
                    if eid is None:
                        return dict(refused='malformed_ref')
                    edition = walk.entity(eid, 'edition')
                    if edition is None:
                        complete = False
                        continue
                    if edition.get('oeuvre') != work['id'] or edition.get('version') != vid:
                        return dict(refused='wrong_back_reference')
                    files, files_complete = _files(edition.get('fichiers'))
                    points, points_complete = _points(edition, walk)
                    complete = complete and files_complete and points_complete  # omitted evidence is never claimed complete
                    editions.append(dict(edition_ref=eid, version_ref=vid, version_label=_own_label(version, work), edition_label=_own_label(edition, work),
                                         duration_ms=_ms(edition.get('duree_ms')), frame_rate=_cadence(edition.get('cadence')),
                                         files=files, points=points, points_complete=points_complete))
        except Budget:
            complete = False
        return dict(work_ref=work['id'], complete=complete, editions=editions)
    except Refused as refused:
        return dict(refused=refused.code)
    except Budget:
        return dict(refused='budget')
