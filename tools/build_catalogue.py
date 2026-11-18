"""Build a signed Sandwich chips catalogue from chips/<id>/ folders.

    python tools/build_catalogue.py --out <dir> --repository <id> --serial <n> --key-file <path> [--channel stable]
                                    [--days 14] [--chip <id> ...]
    python tools/build_catalogue.py --new-test-key <path>        (a THROWAWAY key for local testing)

Writes, under --out:
    artifacts/<id>/<id>-<version>.zip     deterministic: sorted entries, fixed times, one root folder named <id>
    index-<channel>.json                  catalogue format 1 (read by core's cinema_chips.parse_index)
    index-<channel>.json.sig              raw 64-byte Ed25519 signature of the exact index bytes

Rules:
- An artifact is immutable. If <id>-<version>.zip already exists under --out with other bytes, the build stops: a
  changed chip needs a new version.
- The serial must be higher than the one of any index already in --out.
- Keys are never read from or written into this repository's folder. Production signing keys are an owner decision
  (docs: catalogue/repository.json); this tool only uses the key file it is given.

Needs pyca/cryptography for signing.
"""
import argparse
import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHIPS = os.path.join(HERE, 'chips')


def _inside_repo(path):
    path = os.path.normcase(os.path.abspath(path))
    return path == os.path.normcase(HERE) or path.startswith(os.path.normcase(HERE) + os.sep)


def artifact(folder, root):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d not in ('__pycache__', '.git'))
            for name in sorted(files):
                if name.endswith(('.pyc', '.tmp')):
                    continue
                path = os.path.join(base, name)
                info = zipfile.ZipInfo('%s/%s' % (root, os.path.relpath(path, folder).replace(os.sep, '/')), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(path, 'rb') as stream:
                    z.writestr(info, stream.read())
    return out.getvalue()


def access(manifest):
    # The same access model core derives from the archive (cinema_chips.manifest_access): sorted, exact.
    contracts = lambda key: sorted('%s/%s' % (c.get('contract'), c.get('major')) for c in manifest.get(key) or [] if isinstance(c, dict))
    return dict(capabilities=sorted(str(c) for c in manifest.get('capabilities') or []), provides=contracts('provides'), consumes=contracts('consumes'),
                data_bytes=(manifest.get('data') or {}).get('bytes', 0),
                secrets=any(isinstance(s, dict) and s.get('secret') for s in manifest.get('settings') or []))


def load_key(path):
    from cryptography.hazmat.primitives import serialization
    with open(path, 'rb') as stream:
        return serialization.load_pem_private_key(stream.read(), password=None)


def public_hex(key):
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def new_test_key(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if _inside_repo(path):
        sys.exit('refused: keys are never written inside the chips repository')
    key = Ed25519PrivateKey.generate()
    with open(path, 'xb') as stream:
        stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    print(json.dumps(dict(key_file=os.path.basename(path), public=public_hex(key), purpose='throwaway test key')))


def build(options):
    if _inside_repo(options.key_file) or _inside_repo(options.out):
        sys.exit('refused: the key and the built catalogue live outside the chips repository')
    key = load_key(options.key_file)
    index_path = os.path.join(options.out, 'index-%s.json' % options.channel)
    if os.path.isfile(index_path):
        with open(index_path, encoding='utf-8') as stream:
            previous = json.load(stream).get('serial', 0)
        if options.serial <= previous:
            sys.exit('refused: serial %d is not higher than the existing %d' % (options.serial, previous))
    entries = []
    chips_dir = options.chips_dir or CHIPS
    for ident in options.chip or sorted(os.listdir(chips_dir)):
        folder = os.path.join(chips_dir, ident)
        with open(os.path.join(folder, 'manifest.json'), encoding='utf-8') as stream:
            manifest = json.load(stream)
        if manifest.get('id') != ident or 'chip' not in manifest:
            sys.exit('refused: chips/%s has no matching manifest with a chip section' % ident)
        data = artifact(folder, ident)
        rel = 'artifacts/%s/%s-%s.zip' % (ident, ident, manifest['version'])
        target = os.path.join(options.out, *rel.split('/'))
        if os.path.isfile(target):
            with open(target, 'rb') as stream:
                if stream.read() != data:
                    sys.exit('refused: %s %s was already published with other bytes; raise its version' % (ident, manifest['version']))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as stream:
            stream.write(data)
        entries.append(dict(id=ident, version=manifest['version'], kind=manifest['type'], api=manifest['api'], name=manifest['name'],
                            chip=manifest['chip'], access=access(manifest),
                            artifact=dict(path=rel, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    index = dict(format=1, repository=options.repository, channel=options.channel, serial=options.serial,
                 generated=now.strftime('%Y-%m-%dT%H:%M:%SZ'), expires=(now + timedelta(days=options.days)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                 keys=[public_hex(key)], revoked_keys=[], chips=entries)
    data = json.dumps(index, indent=1, sort_keys=True).encode('utf-8')
    with open(index_path, 'wb') as stream:
        stream.write(data)
    with open(index_path + '.sig', 'wb') as stream:
        stream.write(key.sign(data))
    print(json.dumps(dict(index=os.path.basename(index_path), serial=options.serial,
                          chips=[(e['id'], e['version'], e['artifact']['sha256'][:12]) for e in entries])))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--new-test-key')
    parser.add_argument('--out')
    parser.add_argument('--repository')
    parser.add_argument('--channel', default='stable', choices=('stable', 'preview'))
    parser.add_argument('--serial', type=int)
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--key-file')
    parser.add_argument('--chip', action='append')
    parser.add_argument('--chips-dir', help='chip sources (default: this repository''s chips/)')
    options = parser.parse_args()
    if options.new_test_key:
        return new_test_key(options.new_test_key)
    if not (options.out and options.repository and options.serial and options.key_file) or not 1 <= options.days <= 30:
        parser.error('--out, --repository, --serial and --key-file are needed; --days 1-30')
    build(options)


if __name__ == '__main__':
    main()
