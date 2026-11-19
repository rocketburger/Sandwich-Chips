# AniBridge mappings: notice

**Data:** AniBridge mappings, https://github.com/anibridge/anibridge-mappings. Its repository's LICENSE is MIT, and its
README states the dataset is open for anyone. Cinema ships none of the data. When this plugin is on, the user's own
machine downloads the v3 release asset `mappings.min.json` (about 9.35 MB on 2026-11-16, rebuilt daily). Cinema keeps the
last good copy and a small derived index under `<profile>/cinema/metadata-data/cinema.identity.anibridge/`.

**Upstream sources, as AniBridge's README lists them:**
- Anime-Lists/anime-lists
- cedya77/anime-offline-database
- notseteve/AnimeAggregations
- varoOP/shinkro-mapping
- QLever
- the AniList, MyAnimeList, TMDB and TVDB APIs (metadata)

Their own terms apply to their data.

**Limitations, as published:**
- Mappings are community-made. They are directional (A→B does not imply B→A) and season-scoped for TMDB/TVDB/IMDb
  shows.
- The v3 build of 2026-11-16 reported 9,459 validation issues (mostly AniDB and TVDB).

**How Cinema uses it:**
- Exact ids only, never titles.
- Display grouping of one series or movie only.
- A season mapping stays a season (installment) id.
- An entry that maps to more than one TMDB or TVDB series, to specials (season 0), or to more than one movie is not
  used.
- Episode numbers, progress, tracking and playback are never matched through it.
- `$includes` and other files are never followed.
