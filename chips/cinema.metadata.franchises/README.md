# Franchises (Wikidata) for Cinema

Optional, and off until you turn it on in Settings › Extensions › Cinema plugins. It gives Search one card for each
cross-media franchise that Wikidata's relations connect to what you typed. That card holds the franchise's films and
series across studios, decades and continuities. Each film or series opens its own card, where Find in add-ons looks
for it.

Data from [Wikidata](https://www.wikidata.org/) (CC0). Cinema uses the Wikidata API but is not endorsed by Wikimedia.

## How it works: the same for every franchise

No franchise, title, alias or member is written anywhere in this plugin. The same steps run for any search:

1. **Seeds.** Wikidata's own entity search for your words gives up to five items. Only works count: an item that states
   a publication date, an author or a director. A character, a place or a concept is never followed.
2. **Roots.** Each seed, plus what it states:
   - its media franchise (P8345);
   - the series it is part of (P179);
   - what it is based on (P144);
   - the works it follows or is followed by (P155, P156).
3. **Series.** Film series, novel series, series of works and franchise items that relate to a root become roots too.
4. **Members.** Films and series with a TMDB id (P4947 or P4983) that state based on, part of the series, or media
   franchise with any root. The TMDB plugin opens them.

The card is named as the data names it: the media franchise, else the source work, else the seed. Each member is
labelled with its film series, or else what it is based on, and with its form (animated, television film, miniseries).
Members are sorted by release date, which is a sorting, not a viewing order.

**Coverage is what Wikidata's editors linked.** The card says how many works Wikidata links in all, and how many open as
titles here. A film or series that states no such relation, or relates only to a work your words did not reach, is
missing. That is a gap in the data; it is fixed in Wikidata, never with a rule here.

## Requests (1.1.0)

- **Where.** HTTPS to `www.wikidata.org` only, through Cinema's bounded fetch.
- **Identified.** The User-Agent is `Sandwich-Franchises/<version> (https://github.com/rocketburger)`. That is the
  packaged version and the maintainer's public profile, nothing about you. Cinema sends it as given (until 2026-11-18
  Cinema replaced it with "Cinema").
- **One at a time.** Every search, open and background refresh goes through one gate: one request in flight, and
  request starts at most every 0.4 s. That is 150 a minute; "Request pace: Gentle" in the plugin's settings makes it 60.
  Wikimedia publishes 200 a minute for an identified client without an account. That is a published tier, not a promise:
  its services may still limit any client.
- **Order.** A card you opened goes first, then a search, then a refresh. A search you replaced by another one stops
  waiting.
- **Limits per call.** A search makes at most 6 requests. Opening a franchise makes at most 16: at most 40 titles, their
  facts read five at a time.
- **Throttling.** After an HTTP 429 or 503, or Wikidata's JSON `maxlag` or `ratelimited`, nothing is asked until the
  cooldown ends. This holds for every caller and across a restart.
  - A stated Retry-After, in seconds or as a date, is kept in full.
  - Without one, the cooldown is 5 s, doubled each time, at most 300 s.
  - A call that can wait within its own time limit waits and asks again, at most twice. Otherwise the card or Search
    says it could not be read just now, never that there is no franchise.

## Kept on this computer

The data is in Cinema's own folder for this plugin (`metadata-data/cinema.metadata.franchises/data`, at most 8 MiB).

**What is kept:**
- of each item read, only the facts used here: label, the relations above, dates, TMDB ids, Wikidata's revision, and
  when it was read;
- the answers to its searches;
- its last good answers.

**How long:**
- Item facts are reused for a week.
- Searches are reused for an hour, search answers for a day, and a franchise card for six hours.
- After that, the last good answer (at most 30 days old) is shown at once, marked "As read on ...", while it is read
  again in the background.

**Never kept:** a throttled, failed or partial read is never kept as an answer, and "no franchise" is kept only when
Wikidata's data says so.
