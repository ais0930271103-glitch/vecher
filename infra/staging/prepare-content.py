#!/usr/bin/env python3
"""Seed the public editorial directory once; preserve an operator's publication.

This is file preparation using only the host's standard library. The new API
validates the complete schema before serving it; app.publish_content is the
validated compare-and-swap path for all subsequent editorial changes.
"""
import json
import os
import stat
import tempfile
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
DIRECTORY = Path('/opt/vecher-test/content')
SOURCE = Path(__file__).resolve().parents[2] / 'backend/app/content/publications.json'


def read_public(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_BYTES:
            raise ValueError('Content snapshot must be a bounded regular file')
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Content snapshot exceeds its size limit')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('items'), list):
        raise ValueError('Content snapshot has an unexpected shape')
    return raw


def prepare(directory: Path, source: Path) -> str:
    try:
        directory.mkdir(mode=0o755, parents=False)
    except FileExistsError:
        pass
    else:
        # Deployment runs with umask 077; the API is uid10001, not root.
        directory.chmod(0o755)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022 or info.st_mode & 0o005 != 0o005):
        raise ValueError('Content directory must be an owned directory without public write access')
    destination = directory / 'publications.json'
    if destination.exists() or destination.is_symlink():
        read_public(destination)
        if destination.stat().st_mode & 0o044 != 0o044:
            raise ValueError('Existing public snapshot must be readable by the API; it was preserved')
        return 'CONTENT_ALREADY_PRESENT'
    raw = read_public(source)
    fd, name = tempfile.mkstemp(prefix='.seed-', dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as target:
            os.fchmod(target.fileno(), 0o644)
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        # No overwrite even if another initializer/editor wins the race.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            read_public(destination)
            return 'CONTENT_ALREADY_PRESENT'
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return 'CONTENT_SEEDED'
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('Prepare the staging content directory as root')
    try:
        print(prepare(DIRECTORY, SOURCE))
    except (OSError, ValueError, UnicodeError) as error:
        # No draft body, credentials, or private file content in diagnostics.
        raise SystemExit(f'CONTENT_PREPARE_STOPPED: {type(error).__name__}') from None
