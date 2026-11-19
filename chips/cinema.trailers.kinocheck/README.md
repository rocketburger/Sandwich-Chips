# KinoCheck trailers for Cinema

This plugin is optional and off until you turn it on. It lists trailers for movies and shows from
[KinoCheck](https://www.kinocheck.com/), and Advanced Media plays them:
- on a title's thumbnail when you hover over it or move to it;
- in the title's card, continuing from the same moment.

## Turning it on

In **Settings › Extensions › Cinema plugins**, choose **KinoCheck** and turn it on. No key is needed.

The settings are optional:
- **Trailer language:** `en` (the default) or `de`.
- **API key:** raises the free allowance. It is sent as the `X-Api-Key` header and never shown again.
- **API host:** only if KinoCheck gave you one with the key.

## What it does and does not do

- **Lookups:** by the title's TMDB or IMDb id only, never by title text. One request per title, cached by Cinema.
- **Shows:** the trailer belongs to the show. KinoCheck has no season or episode filter, so an episode shows its show's
  trailer, labelled as the show's.
- **Playback:** KinoCheck names YouTube videos. They play in YouTube's official privacy-enhanced embedded player
  (`youtube-nocookie.com`). Cinema allows only that one player address to load. Nothing is downloaded, extracted or
  rehosted.
- **Limits:**
  - The free limit is about 1000 lookups a day. After a "too many requests" answer, Cinema waits before asking again.
  - When a video is blocked in your region or cannot be embedded, the card says so and keeps its picture.
