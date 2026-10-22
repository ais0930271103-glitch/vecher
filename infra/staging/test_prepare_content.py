import importlib.util
import json
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('prepare_content', Path(__file__).with_name('prepare-content.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_initialization_survives_restrictive_umask_and_preserves_later_snapshot(tmp_path):
    source = tmp_path / 'seed.json'
    source.write_text(json.dumps({'version': 1, 'items': []}))
    directory = tmp_path / 'published'
    previous = os.umask(0o077)
    try:
        assert module.prepare(directory, source) == 'CONTENT_SEEDED'
    finally:
        os.umask(previous)
    target = directory / 'publications.json'
    assert directory.stat().st_mode & 0o777 == 0o755
    assert target.stat().st_mode & 0o777 == 0o644
    edited = json.dumps({'version': 1, 'items': [{'id': 'later-editorial-snapshot'}]})
    target.write_text(edited)
    assert module.prepare(directory, source) == 'CONTENT_ALREADY_PRESENT'
    assert target.read_text() == edited
    assert not list(directory.glob('.seed-*'))


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'invalid', 'fifo'])
def test_invalid_existing_destination_is_never_replaced(tmp_path, kind):
    source = tmp_path / 'seed.json'
    source.write_text('{"version":1,"items":[]}')
    directory = tmp_path / 'published'
    directory.mkdir()
    target = directory / 'publications.json'
    if kind == 'symlink':
        target.symlink_to(source)
    elif kind == 'directory':
        target.mkdir()
    elif kind == 'fifo':
        os.mkfifo(target)
    else:
        target.write_text('not a snapshot')
    before = target.lstat()
    with pytest.raises((OSError, ValueError)):
        module.prepare(directory, source)
    assert target.lstat().st_ino == before.st_ino


def test_symlink_directory_is_rejected(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    directory = tmp_path / 'published'
    directory.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        module.prepare(directory, tmp_path / 'unused')
    assert list(real.iterdir()) == []
