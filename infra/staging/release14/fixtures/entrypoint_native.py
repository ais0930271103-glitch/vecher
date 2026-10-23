#!/usr/bin/env python3
"""Offline command doubles; never opens SSH or changes a real server."""
import json
import os
from pathlib import Path
import re
import sys

cfg_path = Path(os.environ['VECHER_ENTRYPOINT_FIXTURE'])
cfg = json.loads(cfg_path.read_text())
name, args = Path(sys.argv[0]).name, sys.argv[1:]
event = {'tool': name, 'args': args}
if name == 'ssh.exe' and args == ['-G', 'vecher']:
    print('user root')
    print('hostname ' + cfg.get('ssh_host', '94.228.166.214'))
    print('port 22')
elif name == 'ssh.exe':
    pos = args.index('vecher')
    assert len(args) == pos + 2, 'Remote shell command split into unsafe argv'
    command = args[pos + 1]
    assert '\r' not in command and '\n' not in command
    assert re.search(r'/root/vecher-release14-tools/run-[a-f0-9]{32}', command)
    action = re.search(r"python3 ./server_release.py '(preflight|deploy|enable-blogs|disable-blogs|status)'", command)
    event['action'] = action[1] if action else 'stage'
    if action:
        assert 'sha256sum -c -' in command
        assert '--force' not in command
    print('FIXTURE_SERVER_ACTION=' + event['action'])
elif name == 'scp.exe':
    assert '--' in args and args[-1].startswith('vecher:/root/vecher-release14-tools/run-')
    assert [Path(x).name for x in args[args.index('--') + 1:-1]] == ['server_release.py', 'media_rehearsal.py']
    event['action'] = 'upload'
else:
    raise AssertionError('Unrecognized native double: ' + name)
with (cfg_path.parent / 'events.jsonl').open('a') as stream:
    stream.write(json.dumps(event) + '\n')
if cfg.get('fail_action') and event.get('action') == cfg['fail_action']:
    print('INTENTIONAL_FIXTURE_FAILURE', file=sys.stderr)
    sys.exit(23)
