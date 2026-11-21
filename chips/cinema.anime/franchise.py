"""One franchise (the umbrella a viewer knows as "the show") above AniList's per-season, per-OVA and per-movie entries,
from STATED relations only. Pure apart from the read() it is given. (2026-11-17, user report: "That Time I Got
Reincarnated as a Slime" opened on season 1 listed only season 1, while "2nd Season" listed seasons 2-4 but not 1.)

Why the old series walk failed: AniList links Slime's season 1 to its SEQUEL, the OVA "Coleus no Yume", and that OVA to
its SEQUEL, season 2. The walk followed only TV-to-TV links, so the chain ended at the OVA: season 1 stood alone, and
season 2 (whose only PREQUEL is that OVA) was taken for the start of its own series. Here the chain runs through any
format; only the SEASONS list keeps the episodic entries.

The franchise is the opened entry's PREQUEL/SEQUEL chain (read breadth-first in batches of up to 50 entries per request,
bounded by a request budget and an entry limit) plus the PARENT/SIDE_STORY neighbours of chain entries, ONE hop and never
walked further (2026-11-19, user report: One Piece listed the one-episode ONA "MONSTERS" as season 1 and Dragon Ball Z
specials as seasons, reached through a crossover special's own relations). Then:
- Seasons: the season entries (TV, TV_SHORT, an ONA of more than one episode) of the PREQUEL/SEQUEL chain, ordered along
  the stated sequels, then by start date.
  Each keeps its own AniList id, episode numbering, status and progress destination; nothing is renumbered or merged.
- OVAs & Specials, Movies: by the format AniList states, each its own entry (never folded into a season's episodes).
- Side stories & spin-offs: an episodic side story, and a SPIN_OFF one relation away (listed, never walked into).
- Other versions: an ALTERNATIVE one relation away (a remake or another adaptation): listed apart, never merged.
CHARACTER, ADAPTATION, SOURCE and OTHER relations are not this franchise. Titles are never compared.

The continuing series ("work", consumed by Watchlists' continuity) is the Seasons list. Its identity is ESTABLISHED only
when the read was complete and the seasons have exactly one start (no season before it over PREQUEL relations, through
any format); its id is that start's AniList id ("anilist.series"), so every entry of the franchise names the same work.
An incomplete read is said as partial with a retry, never presented as a single-season show."""

EPISODIC = ('TV', 'TV_SHORT', 'ONA')
CHAIN = ('PREQUEL', 'SEQUEL')       # the only relations WALKED (they build the seasons)
HOP = ('PARENT', 'SIDE_STORY')      # read one hop from a chain entry, never walked further
WALK = CHAIN + HOP                  # every relation that makes an entry part of this franchise
ONE_HOP = dict(SPIN_OFF='side_stories', ALTERNATIVE='versions', SUMMARY=None, COMPILATION=None)
BATCH = 50
REQUESTS, ENTRIES = 16, 150      # a detail's budget: AniList requests (one per step of a chain), entries
SECTIONS = (('seasons', 'Seasons'), ('specials', 'OVAs & Specials'), ('movies', 'Movies'),
            ('side_stories', 'Side stories & spin-offs'), ('versions', 'Other versions'))
STATUS = dict(FINISHED='finished', RELEASING='releasing', NOT_YET_RELEASED='not_yet_released', CANCELLED='cancelled', HIATUS='hiatus')
FAILED_TEXT = dict(rate_limited='AniList is limiting requests just now', unreachable='AniList could not be reached',
                   bad_response='AniList answered something that could not be read', budget='this series has more entries than one read covers')


def in_chain(node):
    """Whether an entry states any PREQUEL/SEQUEL relation (it is part of a continuity chain)."""
    return any(k in CHAIN for k, _o, _f in (node or {}).get('edges') or ())


def hop_anchor(node):
    """For an entry outside any PREQUEL/SEQUEL chain (a side-story OVA, a one-episode ONA, a film AniList hangs off a
    season as PARENT): the ONE entry its chain is read from, over a single PARENT (else SIDE_STORY) relation. Several
    parents (a crossover special names every show it crosses): the first stated episodic one, else the first stated.
    None for an entry in a chain or with no such relation."""
    if node is None or in_chain(node):
        return None
    for kinds in (('PARENT',), ('SIDE_STORY',)):
        found = [(o, f) for k, o, f in node.get('edges') or () if k in kinds]
        if found:
            return next((o for o, f in found if f in EPISODIC), found[0][0])
    return None


def is_season(node):
    """A season is an episodic entry: TV, or a TV_SHORT/ONA that is not a one-episode piece (2026-11-19, user report:
    One Piece listed the one-episode ONA "MONSTERS" as season 1). Unknown counts (still airing) stay seasons."""
    fmt = node.get('format')
    if fmt == 'TV':
        return True
    return fmt in ('TV_SHORT', 'ONA') and node.get('episodes') != 1


def explore(starts, read, seed=None, requests=REQUESTS, entries=ENTRIES, sides=True):
    """read(ids) -> ({id: node}, requests made) and may raise an error with a .code. seed: {id: node-like {edges}} known
    without asking (a search page). Returns (nodes {id: node}, reached [ids in reach order], complete, failure or None).

    1. The chain: breadth-first over PREQUEL/SEQUEL only, from the starts (a start outside any chain first takes ONE
       PARENT/SIDE_STORY step to its hop_anchor). Only this walk decides completeness.
    2. sides (detail only): the PARENT/SIDE_STORY neighbours of chain entries, read ONE hop and never walked further, so a
       crossover special joins as a special but the other show it crosses into does not (2026-11-19, user report: Dragon
       Ball Z specials listed under One Piece). With them, an OTHER neighbour that itself names a chain entry over a
       PREQUEL/SEQUEL/PARENT/SIDE_STORY relation ("Guren no Kizuna-hen" -> season 2 part 2 is PARENT; AniList states the
       reverse only as OTHER, 2026-11-17 live read). A failed or budget-limited side read leaves those entries to what the
       chain entries state about them (franchise.build lists them from that), never a partial series."""
    have, order = dict(seed or {}), []
    state = dict(used=0, complete=True, failure=None)

    def fetch(ids, wanted, strict=True):
        missing = [i for i in dict.fromkeys(ids) if i not in have]
        for at in range(0, len(missing), BATCH):
            if state['used'] >= requests:
                if strict:
                    state['complete'], state['failure'] = False, state['failure'] or 'budget'
                return False
            chunk = missing[at:at + BATCH]
            try:
                got, made = read(chunk)
            except Exception as error:
                if strict:
                    state['complete'], state['failure'] = False, getattr(error, 'code', None) or 'unreachable'
                return False
            state['used'] += made
            have.update(got)
            if strict and any(i not in got for i in chunk if i in wanted):
                state['complete'] = False        # asked and not answered: unknown, said as partial
        return True

    frontier = list(dict.fromkeys(starts))
    seen, hopped = set(frontier), set()
    while frontier:
        if not fetch(frontier, set(frontier)):
            order.extend(i for i in frontier if i in have)
            break
        following = []
        for ident in frontier:
            node = have.get(ident)
            if node is None:
                continue
            order.append(ident)
            targets = [o for k, o, _f in node.get('edges') or () if k in CHAIN]
            if ident in starts and not targets:
                anchor = hop_anchor(node)
                if anchor is not None:
                    hopped.add(ident)
                    targets = [anchor]
            for other in targets:
                if other in seen:
                    continue
                if len(seen) >= entries:
                    state['complete'], state['failure'] = False, state['failure'] or 'budget'
                    continue
                seen.add(other)
                following.append(other)
        frontier = following
    if sides and not state['failure']:
        chain = [i for i in order if i not in hopped]
        side, probes = [], []
        for ident in chain:
            for kind, other, _f in have[ident].get('edges') or ():
                if other in seen or other in side or other in probes:
                    continue
                if kind in HOP:
                    side.append(other)
                elif kind == 'OTHER':
                    probes.append(other)
        room = max(0, entries - len(seen))
        side = side[:room]
        probes = probes[:max(0, room - len(side))]
        fetch(side + probes, set(), strict=False)
        members = set(chain)
        for ident in side:
            if ident in have and 'title' in have[ident]:
                seen.add(ident)
                order.append(ident)
        for ident in probes:
            node = have.get(ident)
            if node is not None and any(k in CHAIN + HOP and o in members for k, o, _f in node.get('edges') or ()):
                seen.add(ident)
                order.append(ident)
    return have, order, state['complete'], state['failure']


def _date(node):
    start = node.get('start')
    if start:
        return start
    return (node['year'], 0, 0) if node.get('year') else (9999, 0, 0)


def _ids(node):
    out = {'anilist': str(node['id'])}
    if node.get('mal'):
        out['mal'] = str(node['mal'])
    return out


def _aired(node):
    if node.get('status') == 'FINISHED':
        return node.get('episodes')
    if node.get('status') == 'NOT_YET_RELEASED':
        return 0
    return node.get('aired')


def _chain_edges(node, kind):
    return [other for k, other, _f in node.get('edges') or () if k == kind]


def build(start_id, nodes, reached, complete, failure):
    """{state, message, retry, label, ids, sections, work} for the franchise of start_id, from explore()'s answer."""
    start = nodes.get(start_id)
    if start is None or 'title' not in start:
        reason = FAILED_TEXT.get(failure, FAILED_TEXT['unreachable'])
        return dict(state='failed', retry=True, label=None, ids={}, sections=[], work=None,
                    message='The rest of this series could not be read (%s), so its other seasons, OVAs and movies are not listed yet. '
                            'Retry to read them.' % reason)
    member = [i for i in reached if i in nodes and 'title' in nodes[i]]
    members = set(member)
    # The chain's anchor: the opened entry, or, for an entry outside any PREQUEL/SEQUEL chain (a side-story OVA, a film
    # AniList hangs off a season as PARENT), the one entry it names as PARENT.
    anchor = start_id
    hop = hop_anchor(start)
    if hop is not None and hop in members:
        anchor = hop
    def chain_of(first):
        found, todo = {first}, [first]
        while todo:
            node = nodes[todo.pop()]
            for kind, other, _f in node.get('edges') or ():
                if kind in CHAIN and other in members and other not in found:
                    found.add(other)
                    todo.append(other)
        return found
    chain = chain_of(anchor)
    if not any(is_season(nodes[i]) for i in chain):
        # Still no season (a film AniList hangs off two entries as PARENT, 2026-11-17 live read): the nearest season
        # entry over the franchise relations (breadth-first, so deterministic) that has a PREQUEL/SEQUEL chain anchors it.
        nearest = next((i for i in member if is_season(nodes[i]) and in_chain(nodes[i])), None)
        if nearest is not None:
            chain = chain_of(nearest)
    seasons = sorted((nodes[i] for i in chain if is_season(nodes[i])), key=lambda n: (_date(n), n['id']))
    season_ids = {n['id'] for n in seasons}

    # One direction of the chain, from what EITHER side states: b follows a when a states SEQUEL b or b states PREQUEL a.
    after, before = {i: set() for i in chain}, {i: set() for i in chain}
    for a in chain:
        for kind, b, _f in nodes[a].get('edges') or ():
            if b in chain and kind in CHAIN:
                first, second = (a, b) if kind == 'SEQUEL' else (b, a)
                after[first].add(second)
                before[second].add(first)

    def episodic_next(ident, kind):
        """The episodic chain entries reached from ident in one direction (SEQUEL: later, PREQUEL: earlier), through
        non-episodic entries."""
        step = after if kind == 'SEQUEL' else before
        out, todo, seen = set(), [ident], {ident}
        while todo:
            for other in step[todo.pop()]:
                if other in seen or other not in chain:
                    continue
                seen.add(other)
                if other in season_ids:
                    out.add(other)
                else:
                    todo.append(other)
        return out
    roots = [n['id'] for n in seasons if not episodic_next(n['id'], 'PREQUEL')]
    branching = any(len(episodic_next(n['id'], 'SEQUEL')) > 1 for n in seasons) or len(roots) > 1
    # Where the series starts is certain when the read was complete, or when everything BEFORE the opened entry (its
    # PREQUEL relations, through any format) was read: then an unread later part cannot move the start (it only leaves
    # the series "not complete", so nothing is called caught up).
    behind, todo, back_read = {anchor}, [anchor], True
    while todo:
        here = todo.pop()
        for other in set(_chain_edges(nodes[here], 'PREQUEL')) | before.get(here, set()):
            if other not in members:
                back_read = False
            elif other not in behind:
                behind.add(other)
                todo.append(other)
    established = len(roots) == 1 and (complete or (back_read and roots[0] in behind))
    root = roots[0] if len(roots) == 1 else None
    # Orders: each season's step along the stated SEQUEL relations from the start (two sequels of one season share a
    # step: the order branches, and the viewer chooses). Seasons are listed by step, then start date.
    level = {}
    if established:
        level[root], todo = 1, [root]
        while todo:
            ident = todo.pop(0)
            for other in sorted(episodic_next(ident, 'SEQUEL')):
                if other not in level:
                    level[other] = level[ident] + 1
                    todo.append(other)
        seasons.sort(key=lambda n: (level.get(n['id'], 10 ** 6), _date(n), n['id']))

    sections = {key: [] for key, _label in SECTIONS}
    placed = set(season_ids)
    for node in seasons:
        sections['seasons'].append(_entry(node, order=level.get(node['id'])))
    in_chain_note = {}
    for ident in chain - season_ids:
        kinds = {k for n in seasons for k, o, _f in nodes[n['id']].get('edges') or () if o == ident and k in CHAIN}
        if kinds:
            in_chain_note[ident] = 'AniList lists this as a %s; it is not placed in the episode order.' % ('sequel' if 'SEQUEL' in kinds else 'prequel')
    for ident in member:
        if ident in placed:
            continue
        node = nodes[ident]
        placed.add(ident)
        sections[_by_format(node, 'side_stories')].append(_entry(node, note=in_chain_note.get(ident)))
    # One relation away from a CHAIN entry only (never from a side entry: a crossover special's other show is not this
    # franchise), from what the chain entry states about it. PARENT/SIDE_STORY neighbours are normally read (explore's
    # sides) and placed above; one that was not read is listed from the chain entry's statement.
    for ident in (i for i in member if i in chain):
        node = nodes[ident]
        for kind, other, fmt in node.get('edges') or ():
            if (kind not in ONE_HOP and kind not in HOP) or other in placed:
                continue
            seen = (node.get('neighbours') or {}).get(other)
            if seen is None:
                continue
            placed.add(other)
            key = _by_format(seen, 'side_stories') if kind in HOP else ONE_HOP[kind] or _by_format(seen, 'specials')
            note = 'Recap' if kind in ('SUMMARY', 'COMPILATION') else 'Another version, not merged' if kind == 'ALTERNATIVE' else None
            sections[key].append(_entry(seen, note=note))
    for key in ('specials', 'movies', 'side_stories', 'versions'):
        sections[key].sort(key=lambda e: (e['_date'], int(e['ids']['anilist'])))
    out_sections = [dict(id=key, label=label, items=[{k: v for k, v in e.items() if k != '_date'} for e in sections[key][:64]])
                    for key, label in SECTIONS if sections[key]]

    state = 'complete' if complete else 'partial'
    message = None
    if not complete:
        message = 'Part of this series could not be read (%s). What is listed is what was read; Retry to read the rest.' \
                  % FAILED_TEXT.get(failure, 'some entries were not answered')
    label = nodes[root]['title'] if root else (seasons[0]['title'] if seasons else start['title'])
    work = _work(start_id, seasons, sections, root, established, branching, complete, nodes)
    return dict(state=state, message=message, retry=not complete, label=label,
                ids={'anilist.series': str(root)} if established else {}, sections=out_sections, work=work)


def _by_format(node, episodic_key):
    fmt = node.get('format')
    if fmt == 'MOVIE':
        return 'movies'
    if fmt in ('SPECIAL', 'OVA', 'MUSIC') or fmt is None:
        return 'specials'
    if node.get('episodes') == 1 and fmt in ('ONA', 'TV_SHORT'):
        return 'specials'                # a one-episode piece ("MONSTERS"): a special, never a season or a series
    return episodic_key


def _entry(node, order=None, note=None):
    out = dict(locator='al:%d' % node['id'], label=node['title'], ids=_ids(node), format=node.get('format'),
               year=node.get('year') or (node['start'][0] if node.get('start') else None), status=STATUS.get(node.get('status'), 'unknown'),
               episodes=node.get('episodes'), aired=_aired(node), next_airing=node.get('next_airing'), _date=_date(node))
    if order is not None:
        out['order'] = order
    if note:
        out['note'] = note
    if node.get('cover'):
        out['art'] = dict(poster=node['cover'])
    return out


def _work(start_id, seasons, sections, root, established, branching, complete, nodes):
    """The continuing series in the shape Watchlists reads (installments in order, related apart)."""
    notes = []
    if not complete:
        notes.append('Only part of this series could be read; later parts may be missing.')
    if branching:
        notes.append('AniList lists more than one way through these seasons, so the order is not certain.')
    if not established and complete and not branching:
        notes.append('Where this series starts could not be established.')
    installments = [dict(locator=e['locator'], ids=e['ids'], label=e['label'], order=e.get('order') if established else None,
                         status=e['status'], episodes=e['episodes'], aired=e['aired'], next_airing=e['next_airing'],
                         relation='start' if established and e['locator'] == 'al:%d' % root else 'this' if e['locator'] == 'al:%d' % start_id else 'sequel',
                         season=nodes[int(e['ids']['anilist'])].get('season'))
                    for e in sections['seasons']]
    related = []
    for key, relation in (('specials', 'special'), ('movies', 'movie'), ('side_stories', 'side_story'), ('versions', 'alternative')):
        for e in sections[key]:
            related.append(dict(locator=e['locator'], label=e['label'], ids=e['ids'], relation=relation, note=e.get('note')))
    return dict(ids={'anilist.series': str(root)} if established else {}, identity='established' if established else 'unestablished',
                complete=complete, label=nodes[root]['title'] if root else None,
                order=('branching' if branching else 'linear') if established else 'unknown', installments=installments, related=related[:32],
                provenance=[dict(by='provider:cinema.anime', what='Earliest season of the AniList PREQUEL/SEQUEL chain, through OVAs and films (derived)')],
                notes=notes)
