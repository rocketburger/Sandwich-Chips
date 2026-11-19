"""Conformance: the static checks every chip in chips/ must pass before a version is listed (2026-11-18).

    python tools/check_chips.py [--chip <id> ...] [--core <Sandwich core checkout>] [--json]

Per chip, without running any of its code:
- source layout: chips/<id>/manifest.json, its "id" is the folder name, a valid version, a "chip" section (format 1)
  with type and publisher (category is an optional Settings hint);
- source only: no caches, compiled files, keys, environment files, built artifacts or nested repositories;
- every file the manifest names (module, ui, backend, styles, renderer...) is there;
- the artifact is deterministic: built twice, byte-identical (tools/build_catalogue.artifact);
- with --core: the manifest validates in Sandwich core's own installer (cinema_modules.validate), the same check
  core runs before installing, and core derives the same access the catalogue builder states.
Exit status 1 when any chip fails. Behaviour checks (install off, update, rollback, lifetime) run in core's test suite
and its local-catalogue validation (see conformance/README.md)."""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHIPS = os.path.join(HERE, 'chips')
sys.path.insert(0, os.path.join(HERE, 'tools'))
import build_catalogue  # noqa: E402

VERSION = re.compile(r'^\d+\.\d+\.\d+$')
FORBIDDEN_DIRS = {'__pycache__', '.git', 'node_modules', 'dist', 'out', 'artifacts'}
FORBIDDEN_FILES = re.compile(r'(\.pyc|\.pyo|\.pem|\.key|\.sig|\.zip|\.tmp|\.log)$|^(\.env|credentials.*|secrets.*|index-.*\.json)$', re.I)
NAMED = ('module', 'ui', 'backend', 'renderer', 'tokens_css')
TYPES = {'metadata': {'provider'}, 'extension': {'feature', 'source', 'provider'}, 'skin': {'skin'}}


def named_files(manifest):
    out = [manifest[k] for k in NAMED if isinstance(manifest.get(k), str)]
    for key in ('styles', 'layouts'):
        value = manifest.get(key)
        if isinstance(value, list):
            out += [v for v in value if isinstance(v, str)]
    page = manifest.get('page') if isinstance(manifest.get('page'), dict) else {}
    for k in ('ui', 'styles'):
        v = page.get(k)
        out += [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    return [f for f in out if not f.startswith(('/', 'http:', 'https:'))]


def check(ident, core=None):
    folder = os.path.join(CHIPS, ident)
    problems = []
    path = os.path.join(folder, 'manifest.json')
    if not os.path.isfile(path):
        return ['no manifest.json']
    with open(path, encoding='utf-8') as stream:
        manifest = json.load(stream)
    if manifest.get('id') != ident:
        problems.append('manifest id %r is not the folder name' % manifest.get('id'))
    if not isinstance(manifest.get('version'), str) or not VERSION.match(manifest['version']):
        problems.append('no valid version')
    chip = manifest.get('chip')
    if not isinstance(chip, dict) or chip.get('format') != 1:
        problems.append('no chip section (format 1)')
    else:
        if chip.get('type') not in TYPES.get(manifest.get('type'), ()):
            problems.append('chip.type %r does not fit a %s package' % (chip.get('type'), manifest.get('type')))
        if not isinstance(chip.get('publisher'), str) or not chip['publisher']:
            problems.append('chip.publisher missing')
        if 'category' in chip and not isinstance(chip['category'], str):
            problems.append('chip.category is not a name')   # a hint; absent: Settings places it under Other
    for base, dirs, files in os.walk(folder):
        for d in dirs:
            if d in FORBIDDEN_DIRS:
                problems.append('not source: %s/' % os.path.relpath(os.path.join(base, d), folder).replace(os.sep, '/'))
        dirs[:] = [d for d in dirs if d not in FORBIDDEN_DIRS]
        for name in files:
            if FORBIDDEN_FILES.search(name):
                problems.append('not source: %s' % os.path.relpath(os.path.join(base, name), folder).replace(os.sep, '/'))
    for rel in named_files(manifest):
        if not os.path.isfile(os.path.join(folder, *rel.split('?')[0].split('/'))):
            problems.append('names %s, which is not there' % rel)
    if build_catalogue.artifact(folder, ident) != build_catalogue.artifact(folder, ident):
        problems.append('the artifact is not deterministic')
    if core:
        sys.path.insert(0, core)
        import cinema_modules
        import cinema_chips
        try:
            cinema_modules.validate(folder)
        except cinema_modules.Invalid as exc:
            problems.append('core refuses it: %s' % exc)
        if 'chip' in manifest and cinema_chips.manifest_access(manifest) != build_catalogue.access(manifest):
            problems.append('core derives other access than the catalogue builder states')
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chip', action='append')
    parser.add_argument('--core', help='a Sandwich core checkout: also validate each manifest in core\'s installer')
    parser.add_argument('--json', action='store_true')
    options = parser.parse_args()
    ids = options.chip or sorted(n for n in os.listdir(CHIPS) if os.path.isdir(os.path.join(CHIPS, n)))
    results = {ident: check(ident, options.core) for ident in ids}
    if options.json:
        print(json.dumps(results, indent=1, sort_keys=True))
    else:
        for ident, problems in results.items():
            print(('ok    ' if not problems else 'FAIL  ') + ident + ''.join('\n      - ' + p for p in problems))
    sys.exit(1 if any(results.values()) else 0)


if __name__ == '__main__':
    main()
