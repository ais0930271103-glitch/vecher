#!/usr/bin/env python3
"""Merge TURN settings only for staging api-test.xn--b1aga8b7a.online.

Run as root. Backups remain outside Git under /root/backups/vecher-test.
Unrelated env bytes are retained; unsupported syntax stops before any writes.
The domain/ports are an intentional allowlist for this host; no services restart.
After recreating only the API, --verify checks its health and exact TURN settings.
"""
import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

KEYS = (b'VECHER_STUN_URLS', b'VECHER_TURN_URLS', b'VECHER_TURN_SECRET',
        b'VECHER_TURN_CREDENTIAL_TTL_SECONDS')
DOMAIN = 'api-test.xn--b1aga8b7a.online'
BACKUP_DIR = Path('/root/backups/vecher-test/turn-settings')
SOURCE_FILE = Path('/opt/vecher-turn/backend-turn.env')
TARGET_FILE = Path('/opt/vecher-test/repo/infra/staging/.env')

# The generated secret travels through stdin, never argv or printed diagnostics.
RUNTIME_CHECK = r'''
import json, sys, urllib.request
try:
    from dotenv import dotenv_values
    from app.config import get_settings
    from app.routers.calls import _relay_configured
    expected = dotenv_values(stream=sys.stdin, interpolate=False)
    settings = get_settings()
    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5) as response:
        health = json.load(response)
    if not (
        health.get("ok") is True and health.get("env") == "staging"
        and settings.env == "staging" and _relay_configured()
        and settings.stun_urls == json.loads(expected["VECHER_STUN_URLS"])
        and settings.turn_urls == json.loads(expected["VECHER_TURN_URLS"])
        and settings.turn_secret == expected["VECHER_TURN_SECRET"]
        and settings.turn_credential_ttl_seconds == int(expected["VECHER_TURN_CREDENTIAL_TTL_SECONDS"])
    ):
        raise ValueError()
except Exception:
    sys.exit("API_TURN_CHECK_FAILED: values omitted")
print("API_HEALTH_OK")
print("CALLS_ENABLED_OK")
'''

def parts(rhs):
    lead = rhs[:len(rhs) - len(rhs.lstrip(b' \t'))]
    value = rhs[len(lead):]
    if value.startswith((b"'", b'"')):
        m = re.fullmatch(rb'''('(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")([ \t]*(?:#.*)?)''', value)
        if not m:
            raise ValueError('Unsupported quoted or multiline assignment')
        tail = m[2]
        return lead, (b' ' if tail.startswith(b'#') else b'') + tail
    if not value or value.startswith(b'#'):
        return b'', (b' ' if rhs.startswith(b'#') else b'') + rhs
    m = re.search(rb'[ \t]+#|[ \t]+$', value)
    return lead, value[m.start():] if m else b''

def merge_bytes(old, generated):
    patch = {}
    for line in generated.splitlines():
        key, sep, value = line.partition(b'=')
        if not sep or key not in KEYS or key in patch:
            raise ValueError('Unexpected or duplicate generated key')
        patch[key] = value
    if set(patch) != set(KEYS):
        raise ValueError('Missing generated key')
    for key, expected in ((KEYS[0], [f'stun:{DOMAIN}:3478']), (KEYS[1], [
            f'turn:{DOMAIN}:3478?transport=udp', f'turn:{DOMAIN}:3478?transport=tcp',
            f'turns:{DOMAIN}:5349?transport=tcp'])):
        value = patch[key]
        if value[:1] != b"'" or value[-1:] != b"'" or json.loads(value[1:-1]) != expected:
            raise ValueError('Unexpected generated URLs')
    if not re.fullmatch(rb'[0-9a-f]{64}', patch[KEYS[2]]) or patch[KEYS[3]] != b'15000':
        raise ValueError('Invalid generated credential settings')
    seen, output = set(), []
    for line in old.splitlines(keepends=True):
        body = line.rstrip(b'\r\n')
        if not body.strip() or body.lstrip().startswith(b'#'):
            output.append(line)
            continue
        m = re.fullmatch(rb'([ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z_0-9]*)[ \t]*=)(.*)', body)
        if not m:
            raise ValueError('Unsupported existing env syntax')
        lead, tail = parts(m[3])  # Also rejects multiline values in unrelated keys.
        if m[2] in patch:
            if m[2] in seen:
                raise ValueError('Duplicate target key')
            seen.add(m[2])
            line = m[1] + lead + patch[m[2]] + tail + line[len(body):]
        output.append(line)
    result, newline = b''.join(output), b'\r\n' if b'\r\n' in old else b'\n'
    missing = [key for key in KEYS if key not in seen]
    if missing and result and not result.endswith((b'\r', b'\n')):
        result += newline
    return result + b''.join(key + b'=' + patch[key] + newline for key in missing)

def stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

def apply(src, dst, backup_dir=BACKUP_DIR):
    if os.geteuid() != 0:
        raise ValueError('Run as root')
    for path in (src, dst):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise ValueError('Expected root-owned regular files, no symlinks')
    original, before = dst.read_bytes(), dst.stat()
    updated = merge_bytes(original, src.read_bytes())
    if updated == original and stat.S_IMODE(before.st_mode) == 0o600:
        return None
    if backup_dir.resolve() != backup_dir:
        raise ValueError('Backup directory must not use symlinks')
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = backup_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Expected root-only backup directory')
    def staged(data, directory, prefix):
        fd, name = tempfile.mkstemp(prefix=prefix, suffix='.env', dir=directory)
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return Path(name)
    backup = staged(original, backup_dir, 'before-turn-')
    temporary = staged(updated, dst.parent, '.turn-new-')
    try:
        if stamp(dst.lstat()) != stamp(before) or dst.read_bytes() != original:
            raise ValueError('Env changed during merge')
        os.replace(temporary, dst)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def verify_runtime(src=SOURCE_FILE):
    # Reuse the strict generated-file validation without writing any files.
    payload = merge_bytes(b'', src.read_bytes())
    result = subprocess.run(
        ['docker', 'exec', '-i', 'vecher-test-api-1', 'python', '-c', RUNTIME_CHECK],
        input=payload, capture_output=True, timeout=20,
    )
    if result.returncode or result.stdout.strip() != b'API_HEALTH_OK\nCALLS_ENABLED_OK':
        raise ValueError('API health or TURN settings do not match')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true', help='Check the recreated API; write nothing')
    args = parser.parse_args()
    try:
        if args.verify:
            verify_runtime()
            print('API_HEALTH_OK')
            print('CALLS_ENABLED_OK')
            return 0
        backup = apply(SOURCE_FILE, TARGET_FILE)
        print('TURN_ENV_MERGED' if backup else 'TURN_ENV_UNCHANGED')
        if backup:
            print('TURN_ENV_BACKUP=' + str(backup))
    except Exception:
        marker = 'API_TURN_CHECK_FAILED' if args.verify else 'TURN_ENV_MERGE_FAILED'
        print(marker + ': значения скрыты; сервисы этим скриптом не перезапускаются', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
