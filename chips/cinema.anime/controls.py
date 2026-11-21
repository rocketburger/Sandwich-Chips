"""The Anime extension's declared source controls, and how they are applied (filter, then sort). Pure.

Each control is declared ONCE here. backend.py publishes them as the descriptor's "controls" (per-call values from
Cinema's source chooser), and manifest.json declares the same keys as module preferences (the persistent defaults Cinema
renders in Settings > Extensions > Anime); tests check the two agree. Meaningful Otaku 5.3.4 controls kept (by meaning,
not code): quality min/max, size limit per episode, the dub/sub "language source", HDR/DV filters, release-title
include/exclude, packs, cached-first, sort keys, per-source on/off and the search timeout."""

QUALITY_ORDER = ['480p', '720p', '1080p', '2160p']
# The audio choice "sub" is an INFERENCE, said as one: a country of origin (AniList's, stated) is not a language. These
# are the only countries it infers a language for; for any other, or none, "sub" keeps no source and says why.
INFERRED_LANGUAGE = {'JP': 'ja', 'KR': 'ko', 'CN': 'zh', 'TW': 'zh'}
COUNTRY_NAMES = {'JP': 'Japan', 'KR': 'South Korea', 'CN': 'China', 'TW': 'Taiwan'}
LANGUAGE_NAMES = {'ja': 'Japanese', 'ko': 'Korean', 'zh': 'Chinese'}
CONTROLS = [
    dict(key='source_nyaa', kind='choice', label='Nyaa', choices=['on', 'off'], default='on', labels=dict(on='On', off='Off'),
         help='Search Nyaa for torrents.', group='Where to search'),
    # 2026-11-17: nekoBT and Torrentio (exact-id matching, see sources.py). AnimeTosho's operator shut its feed down
    # (2026-09-16 notice; both feed addresses refuse): off for anyone who never chose, a saved "on" is kept and told why
    # each search skips it.
    dict(key='source_nekobt', kind='choice', label='nekoBT', choices=['on', 'off'], default='on', labels=dict(on='On', off='Off'),
         help='Search nekoBT for torrents (matched by AniList id).', group='Where to search'),
    dict(key='source_torrentio', kind='choice', label='Torrentio', choices=['on', 'off'], default='on', labels=dict(on='On', off='Off'),
         help='Ask Torrentio for torrents (matched by Kitsu id, from Kitsu\'s own MyAnimeList mapping).', group='Where to search'),
    dict(key='source_animetosho', kind='choice', label='AnimeTosho', choices=['on', 'off'], default='off', labels=dict(on='On', off='Off'),
         help='AnimeTosho\'s operator has shut down its feed and search API (site notice, 2026-09-16), so it answers nothing; '
              'Cinema tries it again every few hours while this is on.', group='Where to search'),
    dict(key='quality_max', kind='choice', label='Highest quality', choices=['2160p', '1080p', '720p', '480p'], default='2160p',
         help='Leave out sources above this resolution.', group='Picture'),
    dict(key='quality_min', kind='choice', label='Lowest quality', choices=['480p', '720p', '1080p', '2160p'], default='480p',
         help='Leave out sources below this resolution (sources of unknown resolution are kept).', group='Picture'),
    dict(key='size_max_gb', kind='choice', label='Largest size per episode', choices=[0, 1, 2, 4, 8, 16], default=0,
         labels={'0': 'No limit', '1': '1 GB', '2': '2 GB', '4': '4 GB', '8': '8 GB', '16': '16 GB'},
         help='For a pack, its size is divided by the episodes it covers.', group='Size and packs'),
    # audio (2026-11-15): the stored values are unchanged. dub: English named as audio in the release title. sub: the
    # language INFERRED from AniList's stated country of origin (INFERRED_LANGUAGE) named as audio; the inference is said
    # on every search that uses it. A title that names no audio language passes only "any".
    dict(key='audio', kind='choice', label='Audio', choices=['any', 'dub', 'sub'], default='any',
         labels=dict(any='Any (stated or not)', dub='English dub (stated)', sub='Country-of-origin language (inferred)'),
         help='Which audio language a release title must name. "Country-of-origin language" infers the language from '
              'AniList\'s country of origin (Japan: Japanese); that is an inference, not a stated fact. Titles that name '
              'no audio language are kept only with Any.', group='Sound'),
    dict(key='codec', kind='choice', label='Video codec', choices=['any', 'only_avc', 'only_hevc', 'only_av1', 'no_hevc', 'no_av1'], default='any',
         labels=dict(any='Any', only_avc='Only H.264 (AVC)', only_hevc='Only H.265 (HEVC)', only_av1='Only AV1',
                     no_hevc='Leave out H.265 (HEVC)', no_av1='Leave out AV1'),
         help='As the release title states it. "Only" keeps titles that name exactly that codec; "Leave out" drops titles that '
              'name it. Titles that name no codec are kept by Any and Leave out, never by Only.', group='Picture'),
    dict(key='hdr', kind='choice', label='HDR and Dolby Vision', choices=['allow', 'no_dv', 'no_hdr'], default='allow',
         labels=dict(allow='Allow', no_dv='No Dolby Vision', no_hdr='No HDR or Dolby Vision'), group='Picture'),
    dict(key='packs', kind='choice', label='Packs (several episodes)', choices=['include', 'exclude'], default='include',
         labels=dict(include='Include', exclude='Leave out'), group='Size and packs'),
    dict(key='cached', kind='choice', label='Cached sources', choices=['first', 'only', 'any'], default='first',
         labels=dict(first='Cached first', only='Cached only', any='No preference'),
         help='Needs a debrid connection in Accounts & Services to know which are cached.', group='Order and cache'),
    dict(key='sort', kind='choice', label='Sort by', choices=['quality', 'seeders', 'size_small', 'size_large'], default='quality',
         labels=dict(quality='Quality, then seeders', seeders='Seeders', size_small='Smallest first', size_large='Largest first'), group='Order and cache'),
    dict(key='timeout', kind='choice', label='Search time limit', choices=[10, 20, 30, 60], default=20,
         labels={'10': '10 seconds', '20': '20 seconds', '30': '30 seconds', '60': '60 seconds'}, group='Where to search'),
    dict(key='release_filter', kind='text', label='Release title filter', default='',
         help='Words a release title must contain; a word starting with ! must NOT appear (for example: SubsPlease !720p).', group='Release name'),
]
# Module preferences that are NOT source controls (2026-11-17): Settings shows them; a source search never does.
DISPLAY = [
    dict(key='episode_titles', kind='choice', label='Episode titles', choices=['english', 'romaji', 'japanese'], default='english',
         labels=dict(english='English', romaji='Romaji (Japanese in Latin letters)', japanese='Japanese'),
         help='The language of episode titles, when the episode facts state one in it (else another stated language; '
              '"Episode N" only when none is stated).'),
]
PERSISTENT = [c for c in CONTROLS if c['kind'] == 'choice'] + DISPLAY   # the text filter is per search (preferences are choices)


def display(stored, key):
    """A display preference's value in force: the stored one when valid, else its default."""
    control = next(c for c in DISPLAY if c['key'] == key)
    saved = stored.get(key) if isinstance(stored, dict) else None
    return saved if saved in control['choices'] else control['default']


def effective(stored, per_call):
    """The values in force: each control's default, then the stored preference (if valid), then this call's value."""
    out = {}
    for control in CONTROLS:
        value = control['default']
        saved = stored.get(control['key']) if isinstance(stored, dict) else None
        if control['kind'] == 'choice' and saved in control['choices'] and type(saved) is type(control['default']):
            value = saved
        if control['key'] in (per_call or {}):
            value = per_call[control['key']]
        out[control['key']] = value
    return out


def inferred(country):
    """(language code, note) for the audio choice "sub": the inference from AniList's stated country, said as one; (None,
    note) when no language can be inferred."""
    language = INFERRED_LANGUAGE.get(country)
    if language is None:
        return None, ('AniList states no country of origin for this title' if not country else 'AniList states the country %s for this title' % country) + \
            ', so no language is inferred and "Country-of-origin language" keeps no source.'
    return language, ('"Country-of-origin language" is an inference: AniList states the country %s (%s), from which Cinema infers %s audio. '
                      'Only titles that name %s audio are kept.' % (COUNTRY_NAMES[country], country, LANGUAGE_NAMES[language], LANGUAGE_NAMES[language]))


def _qrank(quality):
    return QUALITY_ORDER.index(quality) if quality in QUALITY_ORDER else None


def keep(source, values, original=None):
    """True when a source passes the filters in force. original: the language INFERRED for "sub" (inferred(country)), or
    None when none can be inferred."""
    return not rejections(source, values, original)


def rejections(source, values, original=None):
    """The keys of EVERY control in force that leaves this source out (empty: it is kept). Same rules as keep(); used to
    say which controls emptied a source list (user report, alpha aq: an empty list was blamed on the resolution range
    while another saved control removed the sources)."""
    out = []
    rank = _qrank(source['quality'])
    if rank is not None and rank > QUALITY_ORDER.index(values['quality_max']):
        out.append('quality_max')
    if rank is not None and rank < QUALITY_ORDER.index(values['quality_min']):
        out.append('quality_min')
    if values['size_max_gb'] and source['size']:
        # A pack's size is shared by its episodes: its range, or (a batch naming none) the entry's episode count.
        per_episode = source['size'] / max(1, source.get('_count') or 1)
        if per_episode > values['size_max_gb'] * 1024 ** 3:
            out.append('size_max_gb')
    if values['audio'] == 'dub' and 'en' not in source['audio']:
        out.append('audio')
    if values['audio'] == 'sub' and (original is None or original not in source['audio']):
        out.append('audio')
    stated = [c for c in ('avc', 'hevc', 'av1') if c in source['tags']]
    codec = values.get('codec', 'any')
    if (codec.startswith('only_') and stated != [codec[5:]]) or (codec.startswith('no_') and codec[3:] in stated):
        out.append('codec')
    if (values['hdr'] == 'no_dv' and 'dolby_vision' in source['tags'] and 'hybrid' not in source['tags']) or \
            (values['hdr'] == 'no_hdr' and ({'hdr', 'dolby_vision'} & set(source['tags']))):
        out.append('hdr')
    if values['packs'] == 'exclude' and source['pack']:
        out.append('packs')
    if values['cached'] == 'only' and source['cached'] is not True:
        out.append('cached')
    words = (values.get('release_filter') or '').lower().split()
    title = (source['release'] or '').lower()
    if any((w.startswith('!') and len(w) > 1 and w[1:] in title) or (not w.startswith('!') and w not in title) for w in words):
        out.append('release_filter')
    return out


def said(key, values):
    """A control and its value in force as the chooser names them, e.g. 'Audio: English dub (stated)'."""
    control = next(c for c in CONTROLS if c['key'] == key)
    value = values.get(key)
    return '%s: %s' % (control['label'], (control.get('labels') or {}).get(str(value), value) if control['kind'] == 'choice' else value)


def left_out(rows, values, original=None):
    """{control key: how many of rows it leaves out}, in the controls' declared order (a row may count for several)."""
    counts = {}
    for row in rows:
        for key in rejections(row, values, original):
            counts[key] = counts.get(key, 0) + 1
    return {c['key']: counts[c['key']] for c in CONTROLS if c['key'] in counts}


def order(sources, values):
    """Sorted per the controls; ties keep the readers' order. "Cached first" puts known-cached sources first."""
    key = values['sort']
    if key == 'seeders':
        primary = lambda s: -(s['seeders'] or 0)
    elif key == 'size_small':
        primary = lambda s: s['size'] if s['size'] is not None else 1 << 62
    elif key == 'size_large':
        primary = lambda s: -(s['size'] or 0)
    else:
        primary = lambda s: (-(_qrank(s['quality']) if _qrank(s['quality']) is not None else -1), -(s['seeders'] or 0))
    cached = (lambda s: 0 if s['cached'] is True else 1) if values['cached'] == 'first' else (lambda s: 0)
    return sorted(sources, key=lambda s: (cached(s), primary(s)))
