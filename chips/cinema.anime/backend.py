"""Anime (cinema.anime 0.1.0): a NATIVE Cinema video extension. It provides cinema.media-provider major 1 (contract 1.1:
search, detail, episodes, sources, resolve) under the name "anime"; Cinema's own screens use it, Cinema plays the result.

Written as the template a Cinema video extension follows (templates/cinema.video-template/README.md): manifest-declared capabilities,
one registry entry, declared controls whose persistent defaults are manifest preferences (native Settings), accounts
only through Accounts & Services, every network origin fixed in transport.py, no Kodi or add-on code. Otaku 5.3.4
(GPL-3.0) was read as a behaviour reference only (NOTICE.md); nothing of it is copied or run.

Lifecycle: off by default. While on, it answers; turning it off removes its service (consumer handles stop working),
clears its server-held tokens, and any answer that arrives afterwards is refused by the registry."""
import importlib.util
import os

NAME = 'anime'
CONTRACT, MAJOR = 'cinema.media-provider', 1
ORDER = ('release', 'transport', 'controls', 'catalog', 'sources', 'work', 'franchise', 'episode_meta', 'provider')
NEEDS = dict(catalog=('release', 'transport'), sources=('release', 'transport'),
             provider=('release', 'controls', 'catalog', 'sources', 'work', 'franchise', 'episode_meta'))

_provider = None


def load(folder=None):
    """This module's files, loaded afresh by path (as the host loads backend.py), each given the siblings it uses.
    Nothing is put in sys.modules, so two instances (or two extensions with a "release.py") never share a file."""
    folder = folder or os.path.dirname(os.path.abspath(__file__))
    loaded = {}
    for name in ORDER:
        path = os.path.join(folder, name + '.py')
        spec = importlib.util.spec_from_file_location('cinema_anime_%s_%x' % (name, id(loaded)), path)
        module = importlib.util.module_from_spec(spec)
        for need in NEEDS.get(name, ()):
            setattr(module, need, loaded[need])
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded


def build(services, stored, transport=None, spawn=None, folder=None, clock=None, sleep=None, cache=None):
    """The provider over a transport (the real fixed-origin one unless a test substitutes it). cache: Cinema's ctx.cache
    (0.6.0), where catalogue answers outlive a restart; None keeps them in memory only."""
    parts = load(folder)
    net = transport or parts['transport'].Transport()
    readers = dict(nyaa=parts['sources'].Nyaa(net), nekobt=parts['sources'].NekoBT(net), torrentio=parts['sources'].Torrentio(net),
                   animetosho=parts['sources'].AnimeTosho(net))
    extra = {k: v for k, v in dict(clock=clock, sleep=sleep).items() if v is not None}
    debrid = parts['provider'].Debrid(services, **extra)
    facts = parts['episode_meta'].EpisodeFacts(net, **({'clock': clock} if clock else {}))
    catalog = parts['catalog'].Catalog(net, cache=cache, **({'sleep': sleep} if sleep else {}))
    return parts['provider'].AnimeProvider(catalog, readers, debrid, stored, spawn=spawn, facts=facts, **extra), parts


def start(ctx):
    global _provider
    # ctx.cache (Cinema 2026-11-21+): an older Cinema has none, and answers stay in memory as before.
    cache = ctx.cache if hasattr(ctx, 'cache') and ctx.has('cache') else None
    _provider, _ = build(ctx.services, ctx.storage.get, spawn=lambda fn: ctx.spawn(fn), cache=cache)
    ctx.services.provide(CONTRACT, MAJOR, NAME, _provider)
    ctx.log('Anime started (catalogue: AniList; episode facts: ani.zip; sources: Nyaa, nekoBT, Torrentio, AnimeTosho; playback through Accounts & Services)')


def stop():
    if _provider is not None:
        _provider.stop()
