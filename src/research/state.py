"""Consistent SQLite checkpoints with hashes; never silently initialize on restore."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def checkpoint(files, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {'schema_version': 1, 'files': {}}
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        for name, source in files.items():
            if Path(name).name != name or not Path(source).is_file():
                raise ValueError(f'Invalid or missing checkpoint file: {name}')
            snapshot = staged / name
            with sqlite3.connect(f'file:{Path(source).resolve()}?mode=ro', uri=True) as db:
                with sqlite3.connect(snapshot) as backup:
                    db.backup(backup)
                    if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('SQLite integrity failure')
            manifest['files'][name] = {'sha256': file_hash(snapshot), 'size': snapshot.stat().st_size}
            with open(snapshot, 'rb') as src, gzip.open(staged / f'{name}.gz', 'wb') as dst:
                shutil.copyfileobj(src, dst)
        for name in files:
            os.replace(staged / f'{name}.gz', destination / f'{name}.gz')
        # Caller publishes the entire directory in one git commit, not individual files.
        (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def restore(source, targets):
    source = Path(source)
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or set(manifest.get('files', {})) != set(targets):
        raise ValueError('Unexpected checkpoint manifest')
    staged = []
    try:
        for name, target in targets.items():
            meta = manifest['files'][name]
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=target.parent)
            os.close(fd)
            staged.append((Path(temp), target))
            with gzip.open(source / f'{name}.gz', 'rb') as src, open(temp, 'wb') as dst:
                # Bound decompression and verify exact expected size.
                size = int(meta['size'])
                if not 0 < size <= 2 * 1024**3:
                    raise ValueError('Invalid checkpoint size')
                remaining = size
                while remaining:
                    chunk = src.read(min(1024**2, remaining))
                    if not chunk:
                        raise ValueError('Truncated checkpoint')
                    dst.write(chunk)
                    remaining -= len(chunk)
                if src.read(1):
                    raise ValueError('Checkpoint exceeds manifest size')
            if file_hash(temp) != meta['sha256']:
                raise ValueError('Checkpoint checksum mismatch')
            with sqlite3.connect(temp) as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('SQLite integrity failure')
        # No live processes may use these paths during restore.
        for temp, target in staged:
            for suffix in ('-wal', '-shm'):
                Path(str(target) + suffix).unlink(missing_ok=True)
            os.replace(temp, target)
    finally:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)


def assert_private_state_repository(repository, token):
    """Use authenticated metadata only; never expose a secret or signed URL."""
    import re
    import requests
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or '') or not token:
        raise ValueError('Private state repository and scoped token are required')
    response = requests.get(f'https://api.github.com/repos/{repository}',
                            headers={'Authorization': f'Bearer {token}',
                                     'Accept': 'application/vnd.github+json'}, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f'State repository metadata unavailable: HTTP {response.status_code}')
    meta = response.json()
    if meta.get('private') is not True or meta.get('archived') or not meta.get('permissions', {}).get('push'):
        raise ValueError('State repository must be private, writable and active')
