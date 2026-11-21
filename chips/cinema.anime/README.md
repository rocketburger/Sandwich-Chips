# Anime (cinema.anime 0.1.0)

This is a native Cinema video extension, and the first one built on the video-extension template
(`templates/cinema.video-template/README.md`). It is off by default.

## The flow

1. Search AniList titles.
2. Open a title to see its facts and its parts: sequels, specials and movies are separate entries and are never merged.
3. Open its episodes: aired ones only, while a show is still airing.
4. Open an episode to see its sources from Nyaa and AnimeTosho. Release titles are read for quality, HDR/DV, codec,
   audio, pack and episode range, and each source is marked cached or not through Accounts & Services.
   - **Languages (0.2.0):** only those a title names next to "dub", "audio" or "subs" ("English Dub", "JPN Audio",
     "ENG Subs"). An unmarked title states no language, and "Dual Audio" or "Multi Subs" state none either.
   - **Audio control:** "English dub (stated)" needs English named as audio.
     - **"Country-of-origin language (inferred)"** keeps titles that name, as audio, a language Cinema INFERS from
       AniList's stated country of origin: Japan→Japanese, South Korea→Korean, China or Taiwan→Chinese. A country is
       not a language. Every search that uses this says so in a note, with the country.
     - **No country, or another country:** nothing is inferred; it keeps nothing and says why.
     - **"Any"** keeps every title.
   - **Earlier saved choices:** the stored values `any`, `dub` and `sub` keep their keys, and nothing is switched.
     - `dub` no longer takes "Dual Audio" as English.
     - `sub` no longer means "not English": it is the inference above, labelled as such and explained on each search.
   - **Video codec (new):** Any (the default, as before), Only H.264/HEVC/AV1 (titles naming exactly that codec), or
     Leave out HEVC/AV1 (titles naming it are dropped; titles naming no codec are kept).
5. Choose a source. A pack shows its files, with this episode marked.
6. Choose a file. Cinema plays it.

## Files

| File | Role |
|---|---|
| `backend.py` | start/stop; loads its siblings by path; registers `cinema.media-provider` 1 as `anime` |
| `provider.py` | the operations, locators, server-held tokens, Accounts use, cancellation and time limits |
| `controls.py` | the declared controls (the descriptor and the manifest preferences) and how they filter and sort |
| `catalog.py` | AniList: search, detail, episode numbers |
| `sources.py` | Nyaa and AnimeTosho readers, searches, relevance |
| `release.py` | release-title reading (pure) |
| `transport.py` | the only network access: three fixed https origins, no redirects, 2 MB and time bounds |

## What it does not do yet

These are honest limits, not hidden ones:

- **Two source sites only.** There are no hoster, direct-stream or local-file sources, and no Real-Debrid. Real-Debrid
  in Accounts can only say "Not connected".
- **Playing needs a debrid service connected in Accounts** (Premiumize today). Without one, sources are listed with
  "Cached: unknown", and Play explains what is needed.
- **Playback is unowned** (Player.Open): no Cinema chapters or resume for it. See the template README.
- **No watchlist, history, skip-intro, subtitles search or auto-play next.**
- **The release-title filter applies per search only.** Module preferences are choices, so it cannot be saved.
