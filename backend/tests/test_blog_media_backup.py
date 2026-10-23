"""Consistent backup stops/resumes only its previously running services.

No Docker daemon or server: execute a copy of the real shell script with its two
host storage roots redirected to a temporary directory and docker mocked.
"""
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("api_running,media_running,fail_dump", [
    (True, True, False), (True, False, False), (False, True, False), (True, True, True),
])
def test_consistent_backup_service_state_and_media_files(tmp_path, api_running, media_running, fail_dump):
    root = Path(__file__).resolve().parents[2]
    script = (root / "infra/staging/backup.sh").read_text()
    storage, backup, commands = tmp_path / "storage", tmp_path / "backup", tmp_path / "commands.jsonl"
    script = script.replace("/opt/vecher-test", str(storage)).replace("/root/backups/vecher-test", str(backup))
    target = tmp_path / "backup.sh"
    target.write_text(script)
    original = {}
    for directory in ("uploads", "downloads", "blog-media"):
        (storage / directory).mkdir(parents=True)
        original[directory] = (directory + " original bytes").encode()
        (storage / directory / "fixture.bin").write_bytes(original[directory])
    tools = tmp_path / "bin"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
args = sys.argv[1:]
with open(os.environ['MOCK_DOCKER_LOG'], 'a') as handle:
    handle.write(json.dumps(args) + '\\n')
if 'ps' in args:
    service = args[-1]
    key = 'MOCK_API_RUNNING' if service == 'api' else 'MOCK_MEDIA_RUNNING'
    if os.environ[key] == '1':
        print(service + '-container')
elif 'pg_dump' in args:
    if os.environ['MOCK_DUMP_FAIL'] == '1':
        sys.exit(1)
    print('-- disposable mock SQL backup')
''')
    docker.chmod(0o700)
    result = subprocess.run(["bash", str(target), "--consistent"], env={
        **os.environ, "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
        "MOCK_DOCKER_LOG": str(commands), "MOCK_API_RUNNING": str(int(api_running)),
        "MOCK_MEDIA_RUNNING": str(int(media_running)), "MOCK_DUMP_FAIL": str(int(fail_dump)),
    }, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=15)
    assert (result.returncode != 0) == fail_dump, result.stderr
    calls = [json.loads(line) for line in commands.read_text().splitlines()]
    stopped = [call[-1] for call in calls if "stop" in call]
    restarted = [call[-1] for call in calls if "start" in call]
    expected = (["api"] if api_running else []) + (["public-video-worker"] if media_running else [])
    assert stopped == expected and restarted == expected
    dump_index = next(i for i, call in enumerate(calls) if "pg_dump" in call)
    assert all(i < dump_index for i, call in enumerate(calls) if "stop" in call)
    assert all(i > dump_index for i, call in enumerate(calls) if "start" in call)
    # Backups never modify an APK/download, private upload or source video.
    for directory, data in original.items():
        assert (storage / directory / "fixture.bin").read_bytes() == data
    manifests = list(backup.glob("*.manifest.txt"))
    if fail_dump:
        assert not manifests
    else:
        assert len(manifests) == 1
        assert "media_worker_was_running=" + str(int(media_running)) in manifests[0].read_text()
        assert len(list(backup.glob("*.blog-media.tar.gz"))) == 1
        assert len(list(backup.glob("*.uploads.tar.gz"))) == 1
        assert len(list(backup.glob("*.downloads.tar.gz"))) == 1
        sql = next(backup.glob("*.sql.gz"))
        assert "disposable mock SQL" in gzip.decompress(sql.read_bytes()).decode()
