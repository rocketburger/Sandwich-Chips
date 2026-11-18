# Chip conformance

Every chip in `chips/` must pass these checks before its version is listed in a catalogue. Core runs them today in its
own test suite (`tests/test_chips.py` in the Sandwich repository). A runner here comes with the next milestone.

**Manifest and artifact**
- The manifest validates for its kind (`metadata` api 1 or `extension` api 2).
- It has a valid `chip` section (format 1): type, publisher, category, requires, conflicts and network.
- The artifact rebuilds byte for byte (`tools/build_catalogue.py` is deterministic).
- A published version is never rebuilt with other bytes.

**Install, update and remove**
- It installs OFF, and none of its code runs before it is turned on.
- It updates by being staged, and the update is applied at the next start, keeping its data and settings.
- A failing update rolls back to the previous version and is rejected.
- Removing it keeps its data. Deleting the data is a separate step and deletes only what the chip owns.

**Lifetime**
- Turned off, on again or removed while working, it makes no further request, applies nothing and keeps nothing.
- Sandwich still works with the chip absent.

**Dependencies**
- Each required dependency is planned visibly and never turned on by itself.
- An optional dependency that is missing degrades the chip with a stated reason.

**Appearance** (from the skin milestone on)
- Its settings and surfaces are readable in both skins at 1280×800 and 960×540, and in safe mode.
