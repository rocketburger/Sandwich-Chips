"""The continuing series (work) above AniList's seasonal/cour entries, from ATTRIBUTED relations only. Pure apart from the
detail() it is given.

AniList lists each season, cour, special and movie as its own entry and links them with typed relations. The work is
the chain of episodic entries (TV, TV_SHORT, ONA) joined by PREQUEL/SEQUEL edges:
- Its id is the chain's earliest entry ("anilist.series": that entry's id), derived, with provenance.
- Each installment keeps its own AniList id and its own episode numbering; nothing is renumbered or merged.
- Movies, specials, OVAs, side stories, spin-offs, alternative versions (remakes) and summaries are "related" entries,
  never inserted into the order. A movie AniList calls a sequel is still related, with a note.
- Two sequels from one entry (or two prequels) make the order "branching": both are shown at the same place, and the
  continuity answer is "uncertain", so the user chooses. Titles are never compared."""

EPISODIC = ('TV', 'TV_SHORT', 'ONA')
BUDGET = 12          # entries fetched to build one work (each is cached by the provider)
STATUS = dict(FINISHED='finished', RELEASING='releasing', NOT_YET_RELEASED='not_yet_released', CANCELLED='cancelled', HIATUS='hiatus')
RELATED = dict(SIDE_STORY='side_story', SPIN_OFF='side_story', ALTERNATIVE='alternative', SUMMARY='summary', PARENT='other',
               COMPILATION='summary', CONTAINS='other', CHARACTER='other', ADAPTATION='other', SOURCE='other', OTHER='other')


def _episodic(part):
    return part['format'] in EPISODIC


def build(start_id, detail):
    """{ids, identity, complete, label, order, installments, related, provenance, notes} for the work containing start_id.
    detail(id) -> (media, aired, parts) as catalog.Catalog.detail answers (cached by the caller).

    identity "established" ONLY when the walk back over PREQUEL edges reached, within BUDGET, a fetched episodic entry
    with no earlier episodic part, through exactly one prequel at every step and no cycle. Then ids name that entry, and
    every start inside the chain reaches the same one. Otherwise identity is "unestablished": ids are EMPTY, orders are
    None and the order is "unknown", so nothing can follow, bind or continue on a guessed root. The installments read
    from the start forward are still listed for browsing. complete is False when any part was left unread (budget)."""
    fetched, notes = {}, []
    complete = True

    def get(ident):
        nonlocal complete
        if ident not in fetched:
            if len(fetched) >= BUDGET:
                complete = False
                return None
            fetched[ident] = detail(ident)
        return fetched[ident]

    # Back to the earliest episodic entry over PREQUEL edges; any doubt leaves the root unestablished.
    root, established, branching, seen = start_id, False, False, [start_id]
    while True:
        entry = get(root)
        if entry is None:
            notes.append('Only part of this series could be read, so where it starts is not known.')
            break
        prequels = list(dict.fromkeys(p['id'] for p in entry[2] if p['relation_type'] == 'PREQUEL' and _episodic(p)))
        if not prequels:
            established = True
            break
        if len(prequels) > 1:
            notes.append('AniList lists more than one earlier part here, so where this series starts is not certain.')
            break
        if prequels[0] in seen:
            notes.append('AniList lists these parts as each other\'s prequel (a cycle), so where this series starts is not certain.')
            break
        root = prequels[0]
        seen.append(root)

    # Forward over SEQUEL edges: from the established root, or (browsing only) from the start.
    first = root if established else start_id
    installments, order, level, placed = [], 1, [first], set()
    while level:
        following = []
        if len(level) > 1:
            branching = True
        for ident in level:
            if ident in placed:
                continue
            placed.add(ident)
            entry = get(ident)
            if entry is None:
                notes.append('Only part of this series could be read; later parts may be missing.')
                continue
            media, aired, parts = entry
            installments.append(dict(locator='al:%d' % ident, ids=_ids(media), label=media['title'], order=order if established else None,
                                     status=STATUS.get(media['status'], 'unknown'), episodes=media['episodes'],
                                     aired=_aired(media, aired), next_airing=media.get('next_airing'),
                                     relation='start' if established and ident == root else 'sequel' if ident != start_id else 'this',
                                     season=media.get('season')))
            following += [p['id'] for p in parts if p['relation_type'] == 'SEQUEL' and _episodic(p) and p['id'] not in placed]
        level = list(dict.fromkeys(following))
        order += 1

    related, known = [], {i['locator'] for i in installments}
    for ident in list(placed):
        entry = fetched.get(ident)
        for part in entry[2] if entry else []:
            locator = 'al:%d' % part['id']
            if locator in known or _episodic(part) and part['relation_type'] in ('SEQUEL', 'PREQUEL'):
                continue
            known.add(locator)
            if part['format'] == 'MOVIE':
                relation = 'movie'
            elif part['format'] in ('SPECIAL', 'OVA', 'MUSIC'):
                relation = 'special'
            else:
                relation = RELATED.get(part['relation_type'], 'other')
            note = None
            if part['relation_type'] in ('SEQUEL', 'PREQUEL'):
                note = 'AniList lists this as a %s; it is not placed in the episode order.' % part['relation_type'].lower()
            elif part['relation_type'] == 'ALTERNATIVE':
                note = 'An alternative version (for example a remake); it is a separate work, not merged.'
            related.append(dict(locator=locator, label=part['title'], ids={'anilist': str(part['id'])}, relation=relation, note=note))
    root_media = fetched[root][0] if established else None
    return dict(ids={'anilist.series': str(root)} if established else {}, identity='established' if established else 'unestablished',
                complete=complete, label=root_media['title'] if root_media else None,
                order=('branching' if branching else 'linear') if established else 'unknown', installments=installments, related=related[:32],
                provenance=[dict(by='provider:cinema.anime', what='Earliest episodic entry of AniList PREQUEL/SEQUEL relations (derived)')],
                notes=list(dict.fromkeys(notes)))


def _ids(media):
    out = {'anilist': str(media['id'])}
    if media.get('mal'):
        out['mal'] = str(media['mal'])
    return out


def _aired(media, aired):
    if media['status'] == 'FINISHED':
        return media['episodes']
    if media['status'] == 'NOT_YET_RELEASED':
        return 0
    return aired
