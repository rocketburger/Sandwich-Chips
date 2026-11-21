"""Episode facts for ONE AniList entry, by its exact id, from ani.zip's public mappings (https://api.ani.zip/mappings
?anilist_id=<id>, read 2026-11-17; no key, no account). Otaku 5.3.4 reads the same service for its episode lists
(behaviour reference only, nothing copied).

What is used, per episode of THIS entry (digit keys only; "S1".. are other specials, never this entry's episodes):
- its own title in the languages stated (en, x-jat = romaji, ja), a placeholder ("Episode 12", "TBA") counting as none;
- its air date (YYYY-MM-DD) and overview;
- its still: an image on TheTVDB's artwork host that ani.zip states for that episode (never the series' poster/fanart);
- its TheTVDB season-local number (seasonNumber, episodeNumber) and absoluteEpisodeNumber: the numbering a split cour
  or a later season is released under (Slime 2nd Season Part 2 episode 1 = S2E13 = absolute 37).

Guards: the answer must name THIS AniList id in its mappings, or nothing is used; only episode numbers the caller lists
for this entry are taken (ani.zip's Slime 2nd Season Part 1 also carries Part 2's episodes 13-16 under its keys); every
value is bounded and checked. One request per entry, cached (bounded, expiring); a failure is remembered briefly."""
import json
import re
import threading
import time
from collections import OrderedDict

ORIGIN = 'anizip'
SOURCE = 'ani.zip (TheTVDB / AniDB)'
TTL, FAILED_TTL, LIMIT = 6 * 3600, 300, 128
# An answer that does not cover the episodes asked about (a show airing now: ani.zip states its newest episodes, titles
# and stills some time after they air, or does not know the entry yet) is read again after SHORT_TTL, not 6 hours.
SHORT_TTL = 1800
STILL = re.compile(r'https://artworks\.thetvdb\.com/banners/[A-Za-z0-9/_.-]{1,240}\.(?:jpg|jpeg|png|webp)')
DATE = re.compile(r'(?:19|20)\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])')
PLACEHOLDER = re.compile(r'(?:episode|ep\.?|第)\s*\d+\s*(?:話|话|集)?|tba|tbd|n/?a|\d+', re.I)
TITLE = re.compile(r'[^\x00-\x1f\x7f]{1,200}')
LANGUAGES = dict(english=('en', 'x-jat', 'ja'), romaji=('x-jat', 'en', 'ja'), japanese=('ja', 'x-jat', 'en'))


def _title(value):
    if not isinstance(value, str):
        return None
    value = ' '.join(value.replace('`', '’').split())
    if not value or not TITLE.fullmatch(value) or PLACEHOLDER.fullmatch(value) or '://' in value:
        return None
    return value


def _whole(value, lo, hi):
    return value if type(value) is int and lo <= value <= hi else None


def parse(raw, anilist_id):
    """{episode number: facts} for this entry, or None when the answer is not this entry's."""
    if not isinstance(raw, dict) or not isinstance(raw.get('mappings'), dict) or raw['mappings'].get('anilist_id') != anilist_id:
        return None
    episodes = raw.get('episodes') if isinstance(raw.get('episodes'), dict) else {}
    out = {}
    for key, e in list(episodes.items())[:2000]:
        if not (isinstance(key, str) and key.isdigit() and isinstance(e, dict)) or not 0 < int(key) <= 5000:
            continue
        titles = e.get('title') if isinstance(e.get('title'), dict) else {}
        named = {lang: _title(titles.get(lang)) for lang in ('en', 'x-jat', 'ja')}
        aired = e.get('airDate') if isinstance(e.get('airDate'), str) and DATE.fullmatch(e['airDate'][:10] if e['airDate'] else '') else None
        image = e.get('image') if isinstance(e.get('image'), str) and STILL.fullmatch(e['image']) else None
        overview = e.get('overview') if isinstance(e.get('overview'), str) else None
        season, number = _whole(e.get('seasonNumber'), 0, 999), _whole(e.get('episodeNumber'), 0, 100000)
        out[int(key)] = dict(titles={k: v for k, v in named.items() if v}, aired=aired[:10] if aired else None, still=image,
                             overview=' '.join(overview.split())[:600] if overview and overview.strip() else None,
                             season=season if number is not None else None, number=number if season is not None else None,
                             absolute=_whole(e.get('absoluteEpisodeNumber'), 1, 100000))
    return out


STREAMING_SOURCE = 'AniList (streaming episodes)'
STREAM_TITLE = re.compile(r'\s*(?:episode|ep\.?)\s*(\d{1,5})(?![\d.])\s*(?:[-–—:]\s*(.*))?', re.I | re.S)
STREAM_STILL = re.compile(r'https://[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,6}/[A-Za-z0-9/_.%~+=-]{1,400}')


def streaming(items):
    """{episode number: facts} from AniList's streamingEpisodes (catalog._streaming), the fallback when ani.zip states no
    title or still for an episode (2026-11-19, user report: a show airing now listed bare numbers). The number and the
    name are read from the stated title ("Episode 3 - The Name"); a title naming no episode number is not used (it could
    be any episode), a placeholder name ("Episode 3") is no name. The first statement of a number is kept."""
    out = {}
    for e in items or ():
        match = STREAM_TITLE.fullmatch(e.get('title') or '')
        if not match or not 0 < int(match.group(1)) <= 5000 or int(match.group(1)) in out:
            continue
        named = _title(match.group(2)) if match.group(2) else None
        thumb = e.get('thumbnail') if isinstance(e.get('thumbnail'), str) and STREAM_STILL.fullmatch(e['thumbnail']) else None
        out[int(match.group(1))] = dict(titles={'en': named} if named else {}, still=thumb)
    return out


def missing(facts, numbers):
    """Whether any listed episode has no title or no still in facts (it is read again sooner: SHORT_TTL)."""
    return any(not (facts or {}).get(n, {}).get('titles') or not (facts or {}).get(n, {}).get('still') for n in numbers)


def name(facts, preference):
    """The episode's own title in the preferred language, then the others; None when it has none."""
    for lang in LANGUAGES.get(preference, LANGUAGES['english']):
        if facts and facts.get('titles', {}).get(lang):
            return facts['titles'][lang]
    return None


class EpisodeFacts(object):
    """ani.zip reads, one per entry, cached. read() -> ({number: facts} | None, state: found | failed | unrelated)."""

    def __init__(self, transport, clock=time.monotonic):
        self.transport, self.clock = transport, clock
        self.lock = threading.Lock()
        self.kept = OrderedDict()

    def cached(self, anilist_id):
        with self.lock:
            held = self.kept.get(anilist_id)
            if held is None or held[0] <= self.clock():
                return None
            return held[1]

    def read(self, anilist_id, timeout, numbers=()):
        """numbers: the episodes the caller lists; an answer missing a title or still for any of them is kept SHORT_TTL."""
        held = self.cached(anilist_id)
        if held is not None:
            return held
        try:
            raw = json.loads(self.transport.fetch(ORIGIN, params=dict(anilist_id=str(anilist_id)), timeout=max(1.0, timeout)).decode('utf-8'))
            facts = parse(raw, anilist_id)
            result = (facts, 'found') if facts is not None else (None, 'unrelated')
        except Exception:
            result = (None, 'failed')
        if result[1] == 'failed':
            ttl = FAILED_TTL
        elif result[1] == 'unrelated' or not result[0] or missing(result[0], numbers):
            ttl = SHORT_TTL
        else:
            ttl = TTL
        with self.lock:
            self.kept[anilist_id] = (self.clock() + ttl, result)
            self.kept.move_to_end(anilist_id)
            while len(self.kept) > LIMIT:
                self.kept.popitem(last=False)
        return result
