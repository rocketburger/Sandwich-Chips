"""Watchlists (cinema.watchlists 0.3.0; 0.2.0 added continuity() for Home's rows, 0.3.0 following() to list and manage follows): list browsing, suggestions and tracking POLICY, as an optional module of its own.

It provides cinema.watchlists major 1 (service.Watchlists). Accounts & Services keeps every connection and secret (this
module reads through cinema.accounts/1 only); video providers stay watchlist-neutral (they state media references,
this module never asks them to choose a service). Read-only: no write capability exists, so every tracking intent is
held and shown, never sent. Independently enableable: without Accounts every service reads "accounts_off"; without
Cinema's selected-media service, suggest() needs its media passed in; Advanced Media is not needed.

Turning it off removes its service and its playback subscription; its stored choices (bindings, rules, follows, local
progress of followed works) stay in its own storage."""
import importlib.util
import os

CONTRACT, MAJOR, NAME = 'cinema.watchlists', 1, 'cinema.watchlists'
SELECTED = ('cinema.selected-media', 1, 'cinema')
ORDER = ('adapters', 'policy', 'service')
NEEDS = dict(policy=('context', 'adapters'), service=('context', 'adapters', 'policy'))

_service = None


def load(folder=None):
    """This module's files by path, each given the siblings it uses (and Cinema's cinema_media_context)."""
    import cinema_media_context
    folder = folder or os.path.dirname(os.path.abspath(__file__))
    loaded = dict(context=cinema_media_context)
    for name in ORDER:
        spec = importlib.util.spec_from_file_location('cinema_watchlists_%s_%x' % (name, id(loaded)), os.path.join(folder, name + '.py'))
        module = importlib.util.module_from_spec(spec)
        for need in NEEDS.get(name, ()):
            setattr(module, need, loaded[need])
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded


def build(services, stored, store, folder=None, clock=None, sleep=None):
    parts = load(folder)
    extra = {k: v for k, v in dict(clock=clock, sleep=sleep).items() if v is not None}
    reader = parts['adapters'].AccountsReader(services, **extra)

    def chooser():
        try:
            selection = services.get_one(*SELECTED[:2]) if hasattr(services, 'get_one') else services.get(*SELECTED)
            return selection.current()['media'] if selection is not None else None
        except Exception:
            return None
    return parts['service'].Watchlists(stored, store, reader, chooser), parts


def start(ctx):
    global _service
    _service, _ = build(ctx.services, ctx.storage.get, ctx.storage.set)
    ctx.services.provide(CONTRACT, MAJOR, NAME, _service)
    ctx.playback.subscribe(_service.on_playback)
    ctx.log('Watchlists started (read-only; tracking intents are held, never sent)')


def stop():
    pass
