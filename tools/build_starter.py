"""Build the separate, optional starter: a signed catalogue of exactly the chips and versions starter/<channel>.json
lists, from ONE recorded clean commit of this repository (2026-11-18 packaging pilot).

    python tools/build_starter.py --out <dir outside this repository> --key-file <path> --serial <n> [--starter stable]
                                  [--git <git.exe>]

- The commit is resolved and its chips/ and starter/ trees must be clean BEFORE anything is read; every chip is read
  from that commit's Git objects (git archive), never the working tree; after building, HEAD must still be that commit.
- Each listed version must be exactly the manifest's version at that commit (no "latest": the list is the release).
- Artifacts are deterministic and immutable (an existing <id>-<version>.zip with other bytes stops the build).
- Writes, under --out: artifacts/, index-stable.json (+ .sig), STARTER.json (the receipt: commit, chips, artifact
  hashes, key fingerprint) and README.txt (how to import it, offline, by its fingerprint).
Keys and output stay outside this repository (refused otherwise). Production signing keys are an owner decision."""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, 'tools'))
import build_catalogue as bc  # noqa: E402


def git(exe, *args):
    return subprocess.run([exe, '-c', 'safe.directory=%s' % os.path.abspath(HERE).replace('\\', '/'), '-C', HERE] + list(args),
                          capture_output=True, check=True).stdout


def clean_commit(exe):
    dirty = git(exe, 'status', '--porcelain', '--', 'chips', 'starter', 'tools').decode().strip()
    if dirty:
        sys.exit('refused: uncommitted changes under chips/, starter/ or tools/:\n' + dirty)
    return git(exe, 'rev-parse', 'HEAD').decode().strip()


def chip_files(exe, commit, ident):
    """{relative path: bytes} of chips/<ident> exactly as stored at commit."""
    prefix = 'chips/%s/' % ident
    out = {}
    with tarfile.open(fileobj=io.BytesIO(git(exe, 'archive', '--format=tar', commit, 'chips/%s' % ident))) as tar:
        for m in tar.getmembers():
            if m.isfile():
                rel = m.name[len(prefix):]
                if not rel or rel.startswith('/') or '..' in rel.split('/'):
                    sys.exit('refused: unexpected path %r' % m.name)
                out[rel] = tar.extractfile(m).read()
    return out


def artifact(files, root):
    """The same deterministic archive as build_catalogue.artifact, from in-memory files."""
    buf = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        # os.walk order, as build_catalogue.artifact writes: a folder's files (sorted), then its sub-folders (sorted).
        walk_order = lambda rel: tuple((0 if i == len(rel.split('/')) - 1 else 1, part) for i, part in enumerate(rel.split('/')))
        for rel in sorted(files, key=walk_order):
            if rel.endswith(('.pyc', '.tmp')) or '__pycache__' in rel.split('/'):
                continue
            info = zipfile.ZipInfo('%s/%s' % (root, rel), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[rel])
    return buf.getvalue()


def fingerprint(public_hex):
    digest = hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:32]
    return ' '.join(digest[i:i + 4] for i in range(0, 32, 4))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--serial', type=int, required=True)
    parser.add_argument('--starter', default='stable')
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--git', default='git')
    o = parser.parse_args()
    if bc._inside_repo(o.out) or bc._inside_repo(o.key_file):
        sys.exit('refused: the key and the built starter live outside the chips repository')
    commit = clean_commit(o.git)
    starter = json.loads(git(o.git, 'show', '%s:starter/%s.json' % (commit, o.starter)).decode('utf-8'))
    if starter.get('format') != 1 or not isinstance(starter.get('chips'), list) or not starter['chips']:
        sys.exit('refused: starter/%s.json is not a format-1 starter list' % o.starter)
    key = bc.load_key(o.key_file)
    public = bc.public_hex(key)
    entries, receipt = [], []
    for item in starter['chips']:
        ident, version = item['id'], item['version']
        files = chip_files(o.git, commit, ident)
        manifest = json.loads(files['manifest.json'].decode('utf-8'))
        if manifest.get('id') != ident or manifest.get('version') != version or 'chip' not in manifest:
            sys.exit('refused: %s at %s is version %s, the starter lists %s' % (ident, commit[:12], manifest.get('version'), version))
        data = artifact(files, ident)
        rel = 'artifacts/%s/%s-%s.zip' % (ident, ident, version)
        target = os.path.join(o.out, *rel.split('/'))
        if os.path.isfile(target) and open(target, 'rb').read() != data:
            sys.exit('refused: %s %s already exists with other bytes; raise its version' % (ident, version))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as stream:
            stream.write(data)
        sha = hashlib.sha256(data).hexdigest()
        entries.append(dict(id=ident, version=version, kind=manifest['type'], api=manifest['api'], name=manifest['name'], chip=manifest['chip'],
                            access=bc.access(manifest), artifact=dict(path=rel, bytes=len(data), sha256=sha)))
        receipt.append(dict(id=ident, version=version, artifact_sha256=sha, files=sorted(files)))
    index_path = os.path.join(o.out, 'index-stable.json')
    if os.path.isfile(index_path) and json.load(open(index_path, encoding='utf-8')).get('serial', 0) >= o.serial:
        sys.exit('refused: serial %d is not higher than the existing one' % o.serial)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    index = dict(format=1, repository=starter['id'], channel='stable', serial=o.serial, generated=now.strftime('%Y-%m-%dT%H:%M:%SZ'),
                 expires=(now + timedelta(days=o.days)).strftime('%Y-%m-%dT%H:%M:%SZ'), keys=[public], revoked_keys=[], chips=entries)
    data = json.dumps(index, indent=1, sort_keys=True).encode('utf-8')
    with open(index_path, 'wb') as stream:
        stream.write(data)
    with open(index_path + '.sig', 'wb') as stream:
        stream.write(key.sign(data))
    if clean_commit(o.git) != commit:
        sys.exit('refused: the checkout moved while building; build again')
    record = dict(starter=starter['id'], name=starter.get('name'), source_commit=commit, serial=o.serial, key_fingerprint=fingerprint(public),
                  key='THROWAWAY test key (local pilot; not a production key)', chips=receipt)
    with open(os.path.join(o.out, 'STARTER.json'), 'w', encoding='utf-8') as stream:
        json.dump(record, stream, indent=1)
    with open(os.path.join(o.out, 'README.txt'), 'w', encoding='utf-8', newline='\r\n') as stream:
        stream.write('%s\n\nOptional. Sandwich works without it.\n\nTo import it (offline): Settings > Chips > Repositories > Add a repository,\n'
                     'enter this folder\'s path, then compare the key fingerprint Sandwich shows with this one:\n\n    %s\n\n'
                     'Trust it only if they match. Then Get chips: choose what to install (it installs off), review what each\n'
                     'declares, confirm, and turn on what you want.\n\nBuilt from Sandwich-Chips %s. Chips: %s.\n'
                     % (starter.get('name'), fingerprint(public), commit, ', '.join('%s %s' % (c['id'], c['version']) for c in receipt)))
    print(json.dumps(dict(starter=starter['id'], commit=commit, serial=o.serial, fingerprint=fingerprint(public),
                          chips=[(c['id'], c['version'], c['artifact_sha256'][:12]) for c in receipt])))


if __name__ == '__main__':
    main()
