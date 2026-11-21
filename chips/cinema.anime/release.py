"""Release-title reading for the Anime extension: quality, tags, audio/subtitle languages, packs and episode ranges.

Written for Cinema (2026-11-15) from the release-naming conventions anime trackers use; Otaku 5.3.4 (GPL-3.0) was read
only as a behaviour reference for WHICH facts matter (its quality ladder, HDR/DV/hybrid handling, dub/sub "language
source", batch searches). No Otaku code is copied. Pure functions; no I/O."""
import re

# A stated resolution in the shapes release titles use (2026-11-16): "1080p" between any separators, glued to a source
# tag ("BD1080p", "WEB_1080p"), followed by a frame rate ("1080p60"), or as a frame size ("1920x1080"). The highest
# stated wins (a "1080p+2160p" batch states 2160p). A bare number ("[1080]") is not read as a resolution: unknown.
_QUALITY = [('2160p', re.compile(r'(?<![0-9])(?:2160[pi]|\d{3,4}x2160(?![0-9]))|\b(?:4k|uhd)\b', re.I)),
            ('1080p', re.compile(r'(?<![0-9])(?:1080[pi]|\d{3,4}x1080(?![0-9]))|\bfhd\b', re.I)),
            ('720p', re.compile(r'(?<![0-9])(?:720p|\d{3,4}x720(?![0-9]))', re.I)),
            ('480p', re.compile(r'(?<![0-9])(?:480p|576p|\d{3,4}x(?:480|576)(?![0-9]))|\bsd\b', re.I))]
_TAGS = [('dolby_vision', re.compile(r'\b(dv|dovi|dolby[ ._-]?vision)\b', re.I)), ('hdr', re.compile(r'\bhdr(10\+?)?\b', re.I)),
         ('hevc', re.compile(r'\b(hevc|x265|h\.?265)\b', re.I)), ('av1', re.compile(r'\bav1\b', re.I)),
         ('avc', re.compile(r'\b(avc|x264|h\.?264)\b', re.I)), ('10bit', re.compile(r'\b(10[ -]?bit|hi10p?)\b', re.I)),
         ('remux', re.compile(r'\bremux\b', re.I)), ('web', re.compile(r'\bweb(?:[ ._-]?(?:dl|rip))?\b', re.I)),
         ('bluray', re.compile(r'\b(blu[ -]?ray|bd(?:rip)?|bdmv)\b', re.I)), ('dvd', re.compile(r'\bdvd(?:rip)?\b', re.I)),
         ('hardsub', re.compile(r'\bhard[ -]?subs?\b', re.I)), ('multi_subs', re.compile(r'\bmulti[ -]?subs?\b', re.I)),
         ('multi_audio', re.compile(r'\b(multi[ -]?audio|dual[ -]?audio)\b', re.I))]
_BATCH = re.compile(r'\b(batch|complete(?:[ -]series)?|season[ -]?pack)\b', re.I)
_RANGE = re.compile(r'(?<![\d.])(\d{1,4})\s*(?:-|~|to)\s*(\d{1,4})(?![\d.]|p\b)', re.I)
_SXXEYY = re.compile(r'\bS(\d{1,2})E(\d{1,4})\b', re.I)
_SEASON = re.compile(r'\b(?:S(\d{1,2})(?!E\d)|season[ ._-]?(\d{1,2})|(\d{1,2})(?:st|nd|rd|th)[ ._-]season)\b', re.I)
_DASH_EP = re.compile(r'\s-\s(\d{1,4})(?:v\d)?(?=[\s\[(._]|$)')
_EP_WORD = re.compile(r'\b(?:ep(?:isode)?|e)[ ._-]?(\d{1,4})(?:v\d)?\b', re.I)
_GROUP = re.compile(r'^\s*\[([^\]]{1,60})\]')
_NOISE = re.compile(r'\[[^\]]*\]|\([^)]*\)')


def quality(title):
    for name, pattern in _QUALITY:
        if pattern.search(title):
            return name
    return 'unknown'


def tags(title):
    found = [name for name, pattern in _TAGS if pattern.search(title)]
    if 'hdr' in found and 'dolby_vision' in found:
        found.append('hybrid')
    return found


def season_of(title):
    """The season a release or a catalogue title names (1 when it names none)."""
    match = _SXXEYY.search(title)
    if match:
        return int(match.group(1))
    match = _SEASON.search(title)
    if match:
        return int(next(g for g in match.groups() if g))
    return None


def episodes_of(title):
    """(first, last) episode numbers a release covers, or None when it names none. A single episode is (n, n)."""
    bare = _NOISE.sub(' ', title)
    match = _SXXEYY.search(bare)
    if match:
        n = int(match.group(2))
        return (n, n)
    match = _RANGE.search(bare)
    if match:
        a, b = int(match.group(1)), int(match.group(2))
        if a < b <= 2000:
            return (a, b)
    match = _DASH_EP.search(bare) or _EP_WORD.search(bare)
    if match:
        n = int(match.group(1))
        return (n, n)
    return None


def is_pack(title):
    found = episodes_of(title)
    return bool(_BATCH.search(title)) or (found is not None and found[0] != found[1])


# Languages are what a release title STATES (2026-11-15): a language named right next to an audio, dub or subtitle marker
# ("English Dub", "JPN+ENG Audio", "Audio: Japanese/English", "English Subs"). Nothing is assumed: an unmarked release
# states no language, and "Dual Audio", "Multi Audio", "Dub", "Subbed" or "Multi Subs" without a language name state
# none either (they stay tags). These are the uploader's claims, read from the title; no stream is examined.
LANGUAGES = {'english': 'en', 'eng': 'en', 'japanese': 'ja', 'jpn': 'ja', 'jap': 'ja', 'spanish': 'es', 'spa': 'es', 'esp': 'es',
             'latino': 'es', 'castellano': 'es', 'portuguese': 'pt', 'por': 'pt', 'french': 'fr', 'fre': 'fr', 'fra': 'fr',
             'german': 'de', 'ger': 'de', 'deu': 'de', 'italian': 'it', 'ita': 'it', 'chinese': 'zh', 'chi': 'zh', 'mandarin': 'zh',
             'cantonese': 'zh', 'korean': 'ko', 'kor': 'ko', 'russian': 'ru', 'rus': 'ru', 'hindi': 'hi', 'hin': 'hi'}
_L = r'(?:%s)' % '|'.join(sorted(LANGUAGES, key=len, reverse=True))
_LIST = r'(%s(?:\s*[/+&,]\s*%s){0,5})' % (_L, _L)
_AUDIO = r'(?:dub(?:bed)?|audio)'
_SUBS = r'(?:sub(?:s|bed|titles?)?)'
_AUDIO_STATED = [re.compile(r'\b%s[ ._-]?%s\b' % (_LIST, _AUDIO), re.I),
                 re.compile(r'\b%s[ ._:-]+%s\b(?![ ._-]*%s)' % (_AUDIO, _LIST, _SUBS), re.I)]
_SUBS_STATED = [re.compile(r'\b%s[ ._-]?%s\b' % (_LIST, _SUBS), re.I),
                re.compile(r'\b%s[ ._:-]+%s\b' % (_SUBS, _LIST), re.I)]
_NAME = re.compile(r'\b%s\b' % _L, re.I)


def _stated(title, patterns):
    out = []
    for pattern in patterns:
        for match in pattern.finditer(title):
            for name in _NAME.findall(match.group(1)):
                code = LANGUAGES[name.lower()]
                if code not in out:
                    out.append(code)
    return out


def audio_languages(title):
    """The audio languages a release title names (codes), in the order named; [] when it names none."""
    return _stated(title, _AUDIO_STATED)


def subtitle_languages(title):
    """The subtitle languages a release title names (codes); [] when it names none ("Multi Subs" names none)."""
    return _stated(title, _SUBS_STATED)


CODECS = ('avc', 'hevc', 'av1')


def codecs(found_tags):
    """The video codecs a release title states (from its tags): [] none stated, one, or several (contradictory)."""
    return [c for c in CODECS if c in found_tags]


def group(title):
    match = _GROUP.match(title)
    return match.group(1).strip() if match else None


def covers(found, episode):
    """True when a release's episode range covers `episode`; a pack that names no range covers every episode."""
    return found is None or found[0] <= episode <= found[1]


def file_episode(path):
    """The episode number a file inside a pack names (its own name only), or None."""
    name = path.replace('\\', '/').rsplit('/', 1)[-1]
    found = episodes_of(name)
    return found[0] if found and found[0] == found[1] else None


# The containers Cinema's file selector treats as playable (cinema_episode_files.VIDEO, 2026-11-17); used here only to
# list a pack's videos first. Cinema decides playability itself.
VIDEO_EXT = tuple('.' + e for e in ('mkv', 'mk3d', 'mp4', 'm4v', 'avi', 'webm', 'ts', 'm2ts', 'mts', 'm2t', 'mov', 'qt', 'mpg', 'mpeg',
                                    'm2v', 'vob', 'wmv', 'asf', 'flv', 'f4v', 'ogm', 'ogv', 'rm', 'rmvb', 'divx', 'xvid', '3gp', '3g2',
                                    'evo', 'wtv', 'trp', 'tp', 'dv', 'nuv', 'vc1', 'avc', 'h264', 'hevc'))


def is_video(path):
    return path.lower().endswith(VIDEO_EXT)


def link_container(link):
    """The video container a file LINK's last path segment names (its path may name none), or None."""
    tail = link.split('?', 1)[0].split('#', 1)[0].rsplit('/', 1)[-1].lower()
    for ext in VIDEO_EXT:
        if tail.endswith(ext):
            return ext[1:]
    return None
