#!/usr/bin/env python3
"""Offline command doubles; never opens SSH or changes a real server."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

cfg_path = Path(os.environ['VECHER_ENTRYPOINT_FIXTURE'])
cfg = json.loads(cfg_path.read_text())
name, args = Path(sys.argv[0]).name, sys.argv[1:]
event = {'tool': name, 'args': args}
stage = None
if name == 'ssh.exe' and args == ['-G', 'vecher']:
    print('user root')
    print('hostname ' + cfg.get('ssh_host', '94.228.166.214'))
    print('port 22')
elif name == 'ssh.exe':
    pos = args.index('vecher')
    assert len(args) == pos + 2, 'Remote shell command split into unsafe argv'
    command = args[pos + 1]
    assert '\r' not in command and '\n' not in command
    stage_match = re.search(r'/root/vecher-release14-tools/run-[a-f0-9]{32}', command)
    assert stage_match
    stage = cfg_path.parent / 'remote' / Path(stage_match[0]).name
    action = re.search(r"python3 ./server_release.py '(preflight|deploy|enable-blogs|disable-blogs|status|public-status)'", command)
    event['action'] = action[1] if action else 'stage'
    if action:
        assert 'sha256sum -c -' in command
        assert '--force' not in command
        pins = re.findall(r"'([a-f0-9]{64})' '(server_release.py|media_rehearsal.py|diagnose_access.py)'", command)
        assert {filename for _, filename in pins} == {'server_release.py', 'media_rehearsal.py', 'diagnose_access.py'}
        event['hashes_checked'] = [filename for _, filename in pins]
        if action[1] == 'public-status':
            assert "'public-status' --json </dev/null" in command
    else:
        stage.mkdir(parents=True)
elif name == 'scp.exe':
    assert '--' in args and args[-1].startswith('vecher:/root/vecher-release14-tools/run-')
    uploads = [Path(x) for x in args[args.index('--') + 1:-1]]
    assert [path.name for path in uploads] == ['server_release.py', 'media_rehearsal.py', 'diagnose_access.py']
    stage = cfg_path.parent / 'remote' / Path(args[-1].removeprefix('vecher:').rstrip('/')).name
    for path in uploads:
        shutil.copyfile(path, stage / path.name)
    if cfg.get('corrupt_uploaded_diagnostic'):
        (stage / 'diagnose_access.py').write_bytes(b'UNREVIEWED_REMOTE_DEPENDENCY')
    event['action'] = 'upload'
else:
    raise AssertionError('Unrecognized native double: ' + name)
with (cfg_path.parent / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps(event) + '\n')
if event.get('action') == 'public-status':
    # This is intentionally stderr: the Windows jump-host password prompt must
    # retain its native console route and must never become captured JSON.
    print('SSH_AUTHENTICATION_PROMPT_FIXTURE (no password read)', file=sys.stderr)
if cfg.get('fail_action') and event.get('action') == cfg['fail_action']:
    print('PUBLIC_HTTPS_REQUEST_FAILED' if event.get('action') == 'public-status' else 'INTENTIONAL_FIXTURE_FAILURE', file=sys.stderr)
    sys.exit(23)

if name != 'ssh.exe' or 'action' not in event:
    sys.exit(0)
if event['action'] == 'stage':
    print('FIXTURE_SERVER_ACTION=stage')
    sys.exit(0)
for expected_hash, filename in pins:
    if hashlib.sha256((stage / filename).read_bytes()).hexdigest() != expected_hash:
        print('DEPENDENCY_HASH_MISMATCH', file=sys.stderr)
        sys.exit(29)
    print(filename + ': OK')
if event['action'] != 'public-status':
    print('FIXTURE_SERVER_ACTION=' + event['action'])
    if event['action'] == 'status':
        print(json.dumps({'phase': cfg.get('server_phase', 'no_checkpoint'), 'ready_for_apk14': False}))
        print('RELEASE14_STATUS_OK')
    sys.exit(0)

events = [json.loads(line) for line in (cfg_path.parent / 'events.jsonl').read_text().splitlines()]
check_index = sum(row.get('action') == 'public-status' for row in events) - 1
versions = cfg.get('server_public_versions', ['0.8.0', '0.9.0'])
report = {'api_version': versions[min(check_index, len(versions) - 1)], 'env': 'staging',
          'version_name': '0.8.0', 'version_code': 13, 'sha256': '13' * 32,
          'bytes': 36591005, 'required': False, **cfg.get('server_public_overrides', {})}
print(cfg.get('server_public_raw', json.dumps(report, sort_keys=True)))
if cfg.get('server_public_extra_output'):
    print(cfg['server_public_extra_output'])
if not cfg.get('server_public_missing_marker'):
    print('RELEASE14_PUBLIC_HTTPS_OK')
