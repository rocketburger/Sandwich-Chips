"""cinema.watchlists major 1: the service other parts of Cinema use. Lists (read-only, through Accounts), mapping a list
entry to a media reference, suggestions, destinations, tracking intents (held), local follows and continuity.

Playback facts arrive as neutral lifecycle events (cinema_playback attribution: request, type, media). A session
snapshots its destination when it starts, so a resume or a switch of source keeps it: the binding is the work's, and
the snapshot is the session's. Local progress is kept ONLY for works the user follows, on this device, and only when the
preference allows; it is never sent anywhere.

`context`, `adapters` and `policy` are given by backend.load()."""
import threading

MAX_FOLLOWS, MAX_SESSIONS = 200, 16
COMPLETED = ('ended',)
TERMINAL = ('ended', 'stopped', 'failed', 'replaced', 'closed')


# The account's "in progress" lists, in the order Next up reads them (0.5.0: moved here from Cinema's core, D2).
IN_PROGRESS = ('current', 'repeating')


class Watchlists(object):
    # cinema.watchlists major 1 as this implementation provides it (0.5.0, D2/D4): its minor (see Cinema's
    # cinema_watchlists_contract.OPERATION_MINOR) and the operations a consumer may call through its handle; nothing else
    # of this object is reachable.
    contract_minor = 4
    CONTRACT_OPERATIONS = ('contract_minor', 'services', 'lists', 'account_lists', 'suggest', 'destination', 'bind', 'set_rule',
                           'follows', 'follow', 'unfollow', 'following', 'progress', 'continuing', 'continuity', 'account_next',
                           'session', 'intents', 'on_playback')

    def __init__(self, stored, store, reader, chooser=None):
        """chooser(): the current selected media reference (cinema.selected-media), or None."""
        self.stored, self.store, self.reader = stored, store, reader
        # The list services: whatever Accounts providers declare (audit R4), never a pinned one.
        self.known = adapters.Known(reader)
        self.policy = policy.Policy(stored, store, reader, self.known)
        self.chooser = chooser or (lambda: None)
        self.lock = threading.Lock()
        self.sessions = {}

    # ---- services and lists -----------------------------------------------------------------------------------------
    def services(self):
        return [dict(a.info(), connection=self.reader.connection(a)) for a in self.known.values()]

    def lists(self, service, kind, status, page, cancelled=lambda: False):
        """One page of the connected user's own list, each entry with a media reference (no stream, no source)."""
        adapter = self.policy.adapters.get(service)
        if adapter is None or status not in adapters.STATUSES or type(page) is not int or not 1 <= page <= 20:
            raise ValueError('invalid_request')
        outcome, value = self.reader.read_list(adapter, kind, status, page, cancelled)
        if outcome != 'ok':
            return dict(state='refused', code=value, entries=[], has_next=False)
        entries = []
        for entry in value['entries']:
            ref = context.clean(dict(kind='installment', ids={adapter.namespaces[0]: str(entry['media_id'])}, label=entry['title'],
                                     installment=dict(ids={adapter.namespaces[0]: str(entry['media_id'])}, label=entry['title']),
                                     classifications=[dict(name=s, by='service:%s' % adapter.id, confidence='stated') for s in adapter.suits],
                                     provenance=[dict(by='service:%s' % adapter.id, what='Your %s list (%s)' % (adapter.label, status))]))
            # Two progress facts, never merged (0.4.0): the service's own count (`progress`, as the list states it) and
            # what Cinema recorded on THIS device for the same installment (`local`: the highest finished episode of a
            # followed work), or None. Neither is written anywhere by reading.
            # For the Lists page's title cards (0.4.2): the service's stated cover (an https URL the bridge turns into an
            # opaque artwork handle; the page never sees it), format and season year, when the account read states them.
            entries.append(dict(title=entry['title'], progress=entry['progress'], total=entry['total'], status=entry['status'], media=ref,
                                local=self._local(adapter.namespaces[0], str(entry['media_id'])),
                                cover=entry.get('cover'), format=entry.get('format'), year=entry.get('year'),
                                # 0.7.0: the service's stated airing facts (neutral terms), so Next up decides most rows from the list alone
                                aired=entry.get('aired'), airing=entry.get('airing'), next_airing=entry.get('next_airing')))
        return dict(state='ok', entries=entries, has_next=value['has_next'], truncated=value['truncated'])

    def account_lists(self, service, cancelled=lambda: False):
        """The account's in-progress entries for Next up (0.5.0; this POLICY was Cinema core's until D2): the first page of
        each IN_PROGRESS list of the service's first kind, one entry per title (a title on both lists is read once), and
        which lists were READ: ok (all), partial (some: the rows read are kept, the missing lists named), refused (none).
        Only "ok" may ever be said as "nothing new"."""
        adapter = self.policy.adapters.get(service)
        if adapter is None:
            raise ValueError('unknown_service')
        kind = (list(adapter.kinds) or [None])[0]
        entries, read, failed = [], [], []
        for status in IN_PROGRESS:
            page = self.lists(service, kind, status, 1, cancelled)
            if page.get('state') == 'ok':
                entries.extend(page['entries'])
                read.append(status)
            else:
                failed.append(dict(status=status, code=page.get('code') or 'refused'))
        unique, seen = [], set()
        for entry in entries:
            key = tuple(sorted(((entry.get('media') or {}).get('ids') or {}).items()))
            if key and key not in seen:
                seen.add(key)
                unique.append(entry)
        account = dict(state='ok' if not failed else 'partial' if read else 'refused', code=failed[0]['code'] if failed else None,
                       read=read, failed=failed)
        return dict(entries=unique, account=account)

    def _local(self, namespace, value):
        """The highest episode finished on this device for the installment stating namespace:value, or None."""
        pair, best = '%s:%s' % (namespace, value), None
        for per in self.progress().values():
            for stored_key, number in (per.items() if isinstance(per, dict) else ()):
                if type(number) is int and pair in str(stored_key).split('|'):
                    best = max(best or 0, number)
        return best

    # ---- suggestions and destinations ---------------------------------------------------------------------------------
    def suggest(self, media=None):
        ref = context.clean(media) if media is not None else self.chooser()
        key = context.work_key(ref)
        return dict(self.policy.suggest(ref), media=ref, followed=bool(key and key in self.follows()), key=key)

    def destination(self, media):
        return self.policy.destination(context.clean(media))

    def bind(self, media, destination):
        return self.policy.bind(context.clean(media), destination)

    def set_rule(self, classification, destination):
        return self.policy.set_rule(classification, destination)

    # ---- playback facts -----------------------------------------------------------------------------------------------
    def on_playback(self, event):
        """A lifecycle event (its `media_ref`: the Play's selected-media reference). Returns the intent it produced (also
        kept), or None."""
        if not isinstance(event, dict) or not isinstance(event.get('request'), str):
            return None
        with self.lock:
            session = self.sessions.get(event['request'])
            if session is None:
                ref = context.clean(event.get('media_ref'))
                session = dict(media=ref, destination=self.policy.destination(ref) if ref else None, done=False)
                self.sessions[event['request']] = session
                while len(self.sessions) > MAX_SESSIONS:
                    self.sessions.pop(next(iter(self.sessions)))
        if event.get('type') not in TERMINAL or session['done']:
            return None
        session['done'] = True
        completed = event['type'] in COMPLETED
        if completed:
            self._record(session['media'])
        intent = dict(self.policy.intent(session['media'], session['destination'], completed), request=event['request'])
        self.policy.keep(intent)
        return intent

    def session(self, request):
        with self.lock:
            found = self.sessions.get(request)
            return dict(found) if found else None

    def intents(self):
        return list((self.stored() or {}).get('intents') or [])

    # ---- follows and continuity (local) -------------------------------------------------------------------------------
    def follows(self):
        raw = (self.stored() or {}).get('follows')
        return dict(raw) if isinstance(raw, dict) else {}

    def follow(self, media, provider, locator):
        """Follow the WORK this media belongs to (one follow per work: following season 2 after season 1 is the same)."""
        ref = context.clean(media)
        key = context.work_key(ref)
        if key is None or not isinstance(provider, str) or not isinstance(locator, str) or not context.LOCATOR.fullmatch(locator):
            raise ValueError('no_identity')
        follows = self.follows()
        already = key in follows
        if not already:
            part = ref.get('work') or ref.get('installment') or dict(ids=ref['ids'], label=ref.get('label'))
            follows[key] = dict(ids=part['ids'], label=part.get('label'), provider=provider, locator=locator)
            while len(follows) > MAX_FOLLOWS:
                follows.pop(next(iter(follows)))
            self.store('follows', follows)
        return dict(key=key, already=already, follows=len(follows))

    def unfollow(self, key):
        """Stop following one work (its exact key). Only the local follow goes: its local progress, every destination and
        binding, and anything on a service stay as they are (a later follow of the same work finds its progress again)."""
        follows = self.follows()
        removed = follows.pop(key, None) is not None
        if removed:
            self.store('follows', follows)
        return removed

    def following(self, page=1, per_page=20):
        """One page of the local follows, the latest followed first: {items: [{key, label, provider, locator, ids}], total,
        page, pages}. Local only: no provider, catalogue or service is asked (0.3.0)."""
        if type(page) is not int or type(per_page) is not int or not 1 <= per_page <= 50 or page < 1:
            raise ValueError('invalid_request')
        items = list(self.follows().items())
        items.reverse()
        pages = max(1, -(-len(items) // per_page))
        page = min(page, pages)
        rows = [dict(key=key, label=follow.get('label'), provider=follow.get('provider'), locator=follow.get('locator'),
                     ids=dict(follow.get('ids') or {})) for key, follow in items[(page - 1) * per_page:page * per_page]]
        return dict(items=rows, total=len(items), page=page, pages=pages)

    def progress(self):
        raw = (self.stored() or {}).get('progress')
        return dict(raw) if isinstance(raw, dict) else {}

    def _record(self, ref):
        """A finished episode of a FOLLOWED work, on this device (preference local_progress)."""
        if not ref or (self.stored() or {}).get('local_progress', 'followed') != 'followed' or ref['kind'] != 'episode':
            return
        key = context.work_key(ref)
        if key not in self.follows() or not ref.get('installment') or not ref.get('episode'):
            return
        progress = self.progress()
        per = dict(progress.get(key) or {})
        installment = context.key(ref['installment']['ids'])
        per[installment] = max(per.get(installment) or 0, ref['episode']['number'])
        progress[key] = per
        self.store('progress', progress)

    def continuing(self, detail):
        """For each followed work: its next aired episode, or what is upcoming. detail(provider, locator) -> a provider's
        detail answer (contract 1.1), so the work is read from whichever provider the follow was made with. Sequential:
        a consumer that must bound its time reads the works itself and asks continuity() for each (Cinema's Home does)."""
        return [self.continuity(key, follow, detail(follow['provider'], follow['locator'])) for key, follow in self.follows().items()]

    def continuity(self, key, follow, answer):
        """One followed work's continuity from a provider's detail answer (or an error answer, or None when it was not
        read). The rule is next_eligible's: aired episodes only, never past an uncertain order, never a special or remake.
        follow: the follow as follows() gives it (its provider and opaque locator are echoed, nothing else)."""
        base = dict(key=key, label=follow.get('label'), provider=follow.get('provider'), locator=follow.get('locator'))
        work = answer.get('work') if isinstance(answer, dict) and answer.get('status') == 'ok' else None
        if work is None:
            code = answer.get('code') if isinstance(answer, dict) and answer.get('status') == 'error' else 'not_read' if answer is None else 'no_work'
            return dict(base, state='unavailable', code=code)
        nxt = context.next_eligible(work, self._watched(key, work))
        nxt['part'] = nxt.pop('label', None)        # the installment's own name; `label` is the followed series
        return dict(nxt, **dict(base, label=follow.get('label') or work['label'], order=work['order'], notes=work['notes']))

    # ---- the ACCOUNT's next episodes (0.4.1) -------------------------------------------------------------------------
    def account_next(self, entry, answer, service_label=None):
        """What comes next for one entry of the user's own in-progress list (current or rewatching), from the SERVICE's
        progress, given a provider's detail answer for that entry's installment (found by its exact ids), or None before
        anything was read (0.7.0: then decided from the entry's own airing facts when they suffice, see _from_list, else
        "unavailable"/not_read so the caller reads it). Read-only: no
        follow, binding, destination, tracking or write is made. The device's own record (`local`) rides along apart.

        next_aired: the entry's next aired episode (progress + 1), or, once that installment is finished and watched to
        its end, episode 1 of the NEXT part, only along an established, unbranched order (returning: True);
        upcoming: announced or unaired; caught_up; uncertain (the entry is not exactly one part of what was read, the order
        branches or is unknown, or later parts were not read); unavailable (the provider could not be read)."""
        remote = dict(progress=entry.get('progress') if type(entry.get('progress')) is int and entry['progress'] >= 0 else 0,
                      total=entry.get('total'), status=entry.get('status'))
        base = dict(label=entry.get('title'), remote=remote, local=entry.get('local'), service_label=service_label, source='account')
        if answer is None:
            stated = self._from_list(entry, base, remote)
            if stated is not None:
                return stated
        work = answer.get('work') if isinstance(answer, dict) and answer.get('status') == 'ok' else None
        if work is None:
            code = answer.get('code') if isinstance(answer, dict) and answer.get('status') == 'error' else 'not_read' if answer is None else 'no_work'
            return dict(base, state='unavailable', code=code)
        stated = ((entry.get('media') or {}).get('ids') or {})
        here = [i for i in work['installments'] if stated and all(i['ids'].get(k) == v for k, v in stated.items() if k in i['ids'])
                and any(i['ids'].get(k) == v for k, v in stated.items())]
        if len(here) != 1:
            return dict(base, state='uncertain', reason='This list entry is not exactly one part of the series the video extension read.')
        part, done = here[0], remote['progress']
        aired = part['aired'] if part['aired'] is not None else (part['episodes'] if part['status'] == 'finished' else 0)
        at = lambda i, n: dict(installment=i['locator'], ids=dict(i['ids']), episode=n, part=i['label'])
        if done < (aired or 0):
            return dict(base, state='next_aired', **at(part, done + 1))
        if part['status'] in ('releasing', 'not_yet_released', 'hiatus') or (part['episodes'] and done < part['episodes']):
            nxt = part['next_airing'] or {}
            return dict(base, state='upcoming', at=nxt.get('at'), **at(part, nxt.get('episode') or done + 1))
        # This part is finished and watched to its end: the next part, only along an established order.
        if work.get('identity') != 'established' or part['order'] is None:
            return dict(base, state='uncertain', reason='Where this part sits in its series is not established, so Cinema does not guess the next one.')
        following = [i for i in work['installments'] if i['order'] == part['order'] + 1]
        if len(following) > 1:
            return dict(base, state='uncertain', reason='Several parts follow here; choose the next one yourself.')
        if not following:
            later = any(i['order'] is None or i['order'] > part['order'] for i in work['installments'])
            return dict(base, state='uncertain', reason='Later parts could not be placed or read.') if later or not work.get('complete', True) \
                else dict(base, state='caught_up')
        nxt = following[0]
        nxt_aired = nxt['aired'] if nxt['aired'] is not None else (nxt['episodes'] if nxt['status'] == 'finished' else 0)
        if nxt_aired:
            return dict(base, state='next_aired', returning=True, **at(nxt, 1))
        return dict(base, state='upcoming', returning=True, at=(nxt['next_airing'] or {}).get('at'), **at(nxt, 1))

    @staticmethod
    def _from_list(entry, base, remote):
        """0.7.0 (2026-11-21, AniList's rate limit): the row decided from the list entry's OWN airing facts, as the accounts
        contract states them in neutral terms (aired, airing, next_airing; never a service's status words), or None when they do not decide it and the installment must be read. Decided: the
        next aired episode (progress + 1) of THIS entry, or upcoming while it still airs. Never decided here: a finished
        entry watched to its end (what follows is the next part, which needs the series' order). The row names the
        entry's own ids and no provider locator (installment None): Cinema finds the title by those ids when it is
        chosen, so nothing is read for a row that is only shown."""
        aired, still, nxt = entry.get('aired'), entry.get('airing'), entry.get('next_airing')
        if type(aired) is not int and type(still) is not bool:
            return None          # the service stated neither: read as before
        done = remote['progress']
        ids = dict(((entry.get('media') or {}).get('ids') or {}))
        if not ids:
            return None
        at = lambda n: dict(installment=None, ids=ids, episode=n, part=entry.get('title'))
        if type(aired) is int and done < aired:
            return dict(base, state='next_aired', **at(done + 1))
        if still is True:
            return dict(base, state='upcoming', at=(nxt or {}).get('at'), **at((nxt or {}).get('episode') or done + 1))
        return None

    def _watched(self, key, work):
        per = self.progress().get(key) or {}
        out = {}
        for installment in work['installments']:
            ikey = context.key(installment['ids'])
            # A progress key is the ids the reference stated; match on any shared id of the same installment.
            for stored_key, number in per.items():
                pairs = set(stored_key.split('|'))
                if pairs & set('%s:%s' % kv for kv in installment['ids'].items()):
                    out[ikey] = max(out.get(ikey) or 0, number)
        return out
