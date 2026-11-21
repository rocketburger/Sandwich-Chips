"""Watchlists tracking policy: suggestions, explicit destinations, and tracking INTENTS. Pure apart from storage.

- A connection is never consent. A destination is either an explicit binding for the work (a service, or "none") or a
  rule the user set ("anime -> AniList"). Neither -> no destination -> no intent.
- Bindings are keyed by the work's identity (cinema_media_context.work_key), never by provider, source or title.
- A destination that cannot take this fact (mapping missing or ambiguous, not connected, unavailable) gives NO intent and
  its reason. No other service is tried: no fallback, no broadcast.
- Every intent is "held": no write capability exists, so nothing is ever sent. Intents are kept (bounded) to show.

`context` is cinema_media_context and `adapters` the adapter list, both given by backend.load()."""

MAX_BINDINGS, MAX_RULES, MAX_INTENTS = 300, 8, 20
HELD = 'Not sent: Watchlists has no write capability; tracking writes are not enabled in this version.'
REASONS = dict(no_destination='No destination is chosen for this title.', none_chosen='Tracking is set to None for this title.',
               mapping_missing='This title has no identity this service needs.', mapping_ambiguous='Which entry of this service this is, is not certain.',
               not_connected='This service is not connected in Accounts & Services.', accounts_off='Accounts & Services is off.',
               not_available='This service has no connection available in Cinema.', no_identity='This playback has no stated media identity.',
               needs_attention='This service connection needs attention in Accounts & Services.')


class Policy(object):
    def __init__(self, stored, store, reader, adapter_list):
        """stored(): the module storage dict; store(key, value); reader: adapters.AccountsReader."""
        self.stored, self.store, self.reader = stored, store, reader
        # A live mapping (adapters.Known: what Accounts providers declare) or a fixed list of adapters.
        self.adapters = adapter_list if hasattr(adapter_list, 'get') else {a.id: a for a in adapter_list}

    # ---- the user's choices ---------------------------------------------------------------------------------------
    # The user's choices are kept as chosen (2026-11-18, R4): a destination is a service id whether or not that service is
    # declared right now (Accounts may be off or start late); choosing one is only possible while it is declared.
    @staticmethod
    def _choice(value):
        return value == 'none' or (isinstance(value, str) and 0 < len(value) <= 40 and value.replace('.', '').replace('-', '').replace('_', '').isalnum())

    def bindings(self):
        raw = (self.stored() or {}).get('bindings')
        return {k: v for k, v in raw.items() if isinstance(k, str) and self._choice(v)} if isinstance(raw, dict) else {}

    def rules(self):
        raw = (self.stored() or {}).get('rules')
        return [r for r in raw if isinstance(r, dict) and isinstance(r.get('classification'), str) and self._choice(r.get('destination'))] \
            if isinstance(raw, list) else []

    def bind(self, ref, destination):
        """The user's explicit choice for this work: a service id, "none", or None (forget the binding)."""
        key = context.work_key(ref)
        if key is None:
            raise ValueError('no_identity')
        if destination is not None and destination != 'none' and destination not in self.adapters:
            raise ValueError('unknown_service')
        bound = self.bindings()
        bound.pop(key, None)
        if destination is not None:
            bound[key] = destination
        while len(bound) > MAX_BINDINGS:
            bound.pop(next(iter(bound)))
        self.store('bindings', bound)
        return self.destination(ref)

    def set_rule(self, classification, destination):
        """A scoped policy the user chose: works classified `classification` (stated or derived) go to `destination`."""
        if not isinstance(classification, str) or not context.NAME.fullmatch(classification):
            raise ValueError('invalid_classification')
        if destination is not None and destination != 'none' and destination not in self.adapters:
            raise ValueError('unknown_service')
        rules = [r for r in self.rules() if r['classification'] != classification]
        if destination is not None:
            rules.append(dict(classification=classification, destination=destination))
        self.store('rules', rules[-MAX_RULES:])
        return self.rules()

    def destination(self, ref):
        """{destination: service id | 'none' | None, by: binding | rule | None, explanation}."""
        key = context.work_key(ref)
        if key is None:
            return dict(destination=None, by=None, explanation=REASONS['no_identity'])
        bound = self.bindings().get(key)
        if bound is not None:
            return dict(destination=bound, by='binding', explanation='You chose %s for this title.' % self._label(bound))
        for rule in self.rules():
            if context.classified(ref, rule['classification']):
                return dict(destination=rule['destination'], by='rule',
                            explanation='Your rule: %s titles go to %s.' % (rule['classification'], self._label(rule['destination'])))
        return dict(destination=None, by=None, explanation=REASONS['no_destination'])

    def _label(self, destination):
        if destination == 'none':
            return 'None'
        adapter = self.adapters.get(destination)
        return adapter.label if adapter is not None else destination   # not declared now: its id, as chosen

    # ---- suggestions (recommendations only) -------------------------------------------------------------------------
    def suggest(self, ref):
        """Each service, with whether it suits this media and why. Never a choice: the user picks, or picks None."""
        out = []
        for adapter in self.adapters.values():
            mapping, problem = adapter.map(ref) if ref else (None, 'no_identity')
            suited = [c['name'] for c in (ref or {}).get('classifications', []) if c['name'] in adapter.suits and c['confidence'] != 'uncertain']
            uncertain = [c['name'] for c in (ref or {}).get('classifications', []) if c['name'] in adapter.suits and c['confidence'] == 'uncertain']
            if mapping and suited:
                reason = '%s: this title is classified %s (%s).' % (adapter.label, suited[0], next(
                    '%s by %s' % (c['confidence'], c['by']) for c in ref['classifications'] if c['name'] == suited[0]))
            elif mapping and uncertain:
                reason = '%s may suit this title, but its %s classification is uncertain.' % (adapter.label, uncertain[0])
            elif mapping:
                reason = '%s can identify this title.' % adapter.label
            else:
                reason = '%s cannot take this title: %s' % (adapter.label, REASONS.get(problem, problem))
            out.append(dict(service=adapter.id, label=adapter.label, compatible=mapping is not None, recommended=bool(mapping and suited),
                            connection=self.reader.connection(adapter), reason=reason))
        out.sort(key=lambda s: (not s['recommended'], not s['compatible'], s['label']))
        return dict(suggestions=out, current=self.destination(ref) if ref else None,
                    note='Suggestions only: nothing is sent anywhere, and connecting a service does not turn tracking on.')

    # ---- intents (never sent) ----------------------------------------------------------------------------------------
    def intent(self, ref, destination, completed):
        """What a playback fact would ask of its session's destination (snapshotted at session start)."""
        if ref is None:
            return dict(state='none', reason='no_identity', message=REASONS['no_identity'])
        target = destination.get('destination') if destination else None
        if target is None:
            return dict(state='none', reason='no_destination', message=REASONS['no_destination'])
        if target == 'none':
            return dict(state='none', reason='none_chosen', message=REASONS['none_chosen'])
        adapter = self.adapters.get(target)
        if adapter is None:   # chosen, but not declared now (Accounts off, or the provider gone): no intent, said honestly
            reason = 'accounts_off' if self.reader.declared() is None else 'not_available'
            return dict(state='none', reason=reason, service=target, message=REASONS[reason])
        mapping, problem = adapter.map(ref)
        if mapping is None:
            return dict(state='none', reason=problem, service=target, message=REASONS[problem])
        connection = self.reader.connection(adapter)
        if connection not in ('connected', 'unknown'):   # unknown: stored, not verified since start (never sent anyway)
            reason = connection if connection in REASONS else 'not_connected'
            return dict(state='none', reason=reason, service=target, message=REASONS[reason])
        return dict(state='held', service=target, action='completed' if completed else 'progress', mapping=mapping, message=HELD)

    def keep(self, intent):
        log = [i for i in ((self.stored() or {}).get('intents') or []) if isinstance(i, dict)]
        log.append(intent)
        self.store('intents', log[-MAX_INTENTS:])
