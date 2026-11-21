# Anime extension: provenance

## Source of the code

All code in this folder was written for Cinema (2026-11-15). **No third-party code is copied, bundled, imported or run.**

## Otaku as a reference

Otaku (`plugin.video.otaku.testing` 5.3.4) declares GPL-3.0 in its `addon.xml`. Its local copy contains no license text
file. It was read only as a **behaviour reference**: which controls matter and which searches are made. That covers:

- quality limits, size per episode and HDR/DV filters;
- the dub/sub language source and release-title include/exclude;
- packs, cached-first and sort keys;
- per-source switches and the time limit;
- the episode search plus a batch search for finished shows.

No Otaku function, regular expression, string table or data file was reused. Its add-on is not needed, called or
imported.

## Services read

These are public, documented formats, read with a fixed-origin transport (`transport.py`):

- **AniList public GraphQL API** (`https://graphql.anilist.co/`, docs at docs.anilist.co): catalogue reads only, with
  no token.
- **Nyaa RSS search feed** (`https://nyaa.si/?page=rss`): `nyaa:infoHash`, `nyaa:seeders` and `nyaa:size`.
- **AnimeTosho JSON feed** (`https://feed.animetosho.org/json`): `info_hash`, `seeders` and `total_size`.

None was contacted while this was written. The tests use synthetic answers only.

Added in 0.4.0 (2026-11-17), the same way. Otaku 5.3.4's `resources/lib/pages` was read for WHICH sources it supports;
no Otaku code, regular expression or data was reused. Public read-only checks were made of each service below:

- **AniList** batched franchise reads (`Page.media(id_in: ...)` with typed relations), same origin and terms as above.
- **nekoBT public API** (`https://nekobt.to/api/v1/`): `media/search`, `media/<id>`, `torrents/search`. A media is used
  only when its stated AniList mapping names the entry; the episode only through that mapping.
- **Torrentio** (`https://torrentio.strem.fun/stream/...kitsu:<id>...json`), a public Stremio add-on answer; and
  **Kitsu's public mappings** (`https://kitsu.app/api/edge/mappings`) from a MyAnimeList id to a Kitsu id.
- **ani.zip public mappings** (`https://api.ani.zip/mappings?anilist_id=<id>`, from 0.5.0): each episode's own title,
  air date, overview, TheTVDB still and season-local/absolute numbers, for the exact AniList entry only (the answer
  must name that id; only the episode numbers listed for the entry are used). Otaku reads the same service for its
  episode lists; nothing of its code is used. Stills are TheTVDB artwork URLs, fetched by Cinema's artwork store
  when a row is shown.
- **AnimeTosho**: `feed.animetosho.org` no longer resolves and `animetosho.org/feed/json` answers HTTP 403 "Feed
  disabled". The site's notice of 2026-09-16 says the feed and API are shut down. It stays listed, off by default.

Otaku sources NOT implemented, and why:
- The hosted streaming sites (HiAnime, AnimeKai, AnimePahe, Gogoanime, AniWave, Anikoto, AniMixPlay, WCOStream). They
  need site scraping, player de-obfuscation or a third-party decryption service (Otaku's AnimeKai path calls
  `enc-dec.app`). That is a hosted backend and a protection bypass, and both are outside this extension's rules.
- Easynews and debrid cloud files: account sources. Accounts & Services offers no such operation today.
- Local files: Cinema's Kodi library already covers them.

## Accounts

Debrid cache checks and file links go through **Accounts & Services** (`cinema.accounts/1`). This extension holds no
key, token or client identity, and borrows none.
