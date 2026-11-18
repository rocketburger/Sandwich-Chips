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

## Status (2026-11-18): local pilot only

- One chip, `cinema.metadata.franchises` (Wikidata), is the first pilot. Core installs it from a **local** catalogue
  signed with a **throwaway test key**.
- Nothing is published. No releases, keys or catalogues are in this repository.
- Signing-key custody, public hosting URLs and licensing are decisions still open for the owner.
- Internal ids stay `cinema.*` for compatibility with existing profiles.

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
