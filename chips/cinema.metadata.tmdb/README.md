# TMDB metadata plugin for Cinema

Optional and installed separately; Cinema does not bundle or enable it. It fills the
information panel with a synopsis (when the addon gave none), a backdrop (when the item
has no artwork), cast with portraits, director, studio, country, a content rating for
your country, and the release or air date.

Cinema uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved
by TMDB.

## Install

Drag this whole folder (`cinema.metadata.tmdb`) onto Cinema's window, or copy it into the
Extensions folder (`%LOCALAPPDATA%\Cinema\Extensions`, opened from **Settings ›
Extensions › Open Extensions folder**). Cinema installs it within a few seconds, with no
restart needed. Then choose **TMDB** in Settings › Extensions › Cinema plugins, **Turn
on**, and set:

- **API Read Access Token or API key** (required): from your TMDB account's API
  settings page. The Read Access Token is preferred: it is sent as an
  `Authorization: Bearer` header. A v3 API key can only be sent as a URL parameter.
  The value is stored in Cinema's profile (`metadata-settings.json`) and is never shown
  again or sent to the page.
- **Language** (optional, default `en-US`).
- **Rating country** (optional, default `US`): which country's certification to show.

## How items are matched

Matching is by the ids the addon set, conservatively:

- **Movies:** `tmdb`, then `imdb` (through TMDB's find), then a title search that must
  give exactly one exact-title result (and year, when known).
- **Shows:** `tmdb`, then `tvdb` or `imdb`, then the same strict search.
- **Episodes:** the show's `tvshow.tmdb`, then `tvshow.tvdb` or `tvshow.imdb`, then the
  episode's own `imdb` or `tvdb` (the episode TMDB finds must have the same season and
  episode), then a strict search on the show title. A bare `tmdb` id on an episode is
  ignored, because addons use it for the show or for the episode. The season and episode
  numbers are required and checked against TMDB's answer.

## Title pages (version 1.1)

The card also gets a tagline, genres and runtime. It gets these rows:
- **Cast & crew:** up to 20 cast, then directors, writers and creators.
- **More like this:** TMDB's recommendations, up to 20.
- **The collection:** the other films of a movie's collection, in release order.

Each person and title is a link. Cinema keeps TMDB's id behind an opaque token the page cannot read. Choosing it asks
this plugin for that identity (`resolve`):
- **A person's page:** photo, biography, born and died, birthplace, and Known for, by popularity. Talk shows, news shows
  and appearances as oneself are left out.
- **A title's page:** the same fields as a card.

A person page is one request; a title page is at most two (the title, then its collection).

Anything ambiguous or unmatched shows nothing extra. Never shown: review scores, trailers
(TMDB lists YouTube pages, which Cinema cannot play) or where to stream.

## Collections, studios and topics (version 1.4)

Search also asks TMDB for groups matching your words: film **collections**, production **companies** and **keywords**
(shown as topics). That is three requests per search, one page each, never one per title.

- A **collection** opens with all its films. They are sorted by release date, which is a sorting, not a viewing order.
- A **company** opens with the films and shows TMDB credits it on, sorted by popularity, one page at a time. Its country
  and stated parent company are shown. A production credit is not ownership or a shared story. Same-named companies
  stay separate, and subsidiaries are never added.
- A **topic** (keyword) opens with the titles TMDB tags with it. Tags are incomplete and are not a canon.

Each film or show opens its own card, where Find in add-ons looks for it by its id. Opening a group is at most three
requests; each "More" is one.

## Network behaviour

- HTTPS only, through Cinema's fetch (8 s per request, 2 MB limit).
- At most four requests and 12 s per item.
- Results are cached in memory for the session. Failures (offline, rate limit, rejected
  key) are retried after a minute.
- After an HTTP 429 there are no requests until Retry-After (or 30 s).
- Images are cached by Cinema's artwork cache.

Only the error type is logged, never the key, URLs or responses.
