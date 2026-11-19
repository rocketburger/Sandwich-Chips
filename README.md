# Sandwich-Chips
Official repository for Sandwich modules and extensions

**Chips** are Sandwich's independently installable packages: features, sources, providers and skins. All official
chips live together in this one repository. Each chip keeps its own manifest, version and artifact, and one signed
catalogue lists them. The core application is in [rocketburger/Sandwich](https://github.com/rocketburger/Sandwich).

| Folder | Holds |
|---|---|
| `chips/<id>/` | One chip's source. Its `manifest.json` carries a `chip` section (format 1): type, publisher, category, requires, conflicts, network. |
| `tools/build_catalogue.py` | Builds deterministic artifacts and a signed `index-<channel>.json` (outside this folder). |
| `catalogue/repository.json` | The official catalogue's identity. Its keys and public URLs are not set yet. |
| `conformance/` | What every chip must pass before it is listed. |

## Status (2026-11-18): local only

This repository is the **one canonical source** of these chips. Sandwich core keeps no copy: its build and tests read
them from here, and the running app knows only installed packages.

| Chip | Kind | Version |
|---|---|---|
| `cinema.metadata.tmdb` | metadata: details, groups, trailers (TMDB; its attribution `notice` is in its manifest) | 1.4.3 |
| `cinema.metadata.franchises` | metadata: cross-media franchises (Wikidata, CC0) | 1.1.2 |
| `cinema.metadata.tenrai` | metadata: anime information (Tenrai) | 0.1.2 |
| `cinema.identity.anibridge` | identity: cross-ids (AniBridge, MIT; NOTICE.md) | 0.1.1 |
| `cinema.chapters.chaptersdb` | chapters (ChaptersDB) | 0.1.1 |
| `cinema.markers.theintrodb` | markers (TheIntroDB) | 0.1.1 |
| `cinema.editions.cadrame` | editions (Cadrame, CC0); no category, so Settings lists it under Other | 0.1.1 |
| `cinema.trailers.kinocheck` | trailers (KinoCheck) | 1.0.1 |
| `cinema.skin.cinema`, `cinema.skin.atrium` | skins (pilot) | 1.0.0 |

- Core installs them from a **local** catalogue signed with a **throwaway test key**.
- Nothing is published. No releases, keys or catalogues are in this repository.
- Signing-key custody, public hosting URLs and licensing are decisions still open for the owner.
- Internal ids stay `cinema.*` for compatibility with existing profiles.
- `.gitattributes` keeps chip sources byte-exact (no line-ending conversion), so artifact and installed hashes do not
  depend on the checkout.

## Conformance

```
python tools/check_chips.py --core <Sandwich core checkout>
```

This checks layout, source-only content, the files the manifest names, deterministic artifacts, core's own manifest
validation, and access equal to what the catalogue builder states.
## Trust, in short

A catalogue signature identifies its publisher; it does not make code safe. Chips run inside Sandwich as trusted code,
and the access they declare (capabilities, network hosts, data, secrets) is shown before installing.
- Sandwich installs chips **off**; turning one on is a separate, explicit step.
- Each installed chip is **pinned** to the repository and key it came from. Another repository offering the same id is
  refused unless you switch its source explicitly.

## Building a local test catalogue

```
python tools/build_catalogue.py --new-test-key ../scratch/test.pem
python tools/build_catalogue.py --out ../scratch/catalogue --repository sandwich.test --serial 1 --key-file ../scratch/test.pem
```

Keys and built catalogues must stay outside this folder; the tool refuses otherwise.
