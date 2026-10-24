#!/usr/bin/env python3
"""Build the standalone Windows diagnostic from adjacent reviewed sources."""
from pathlib import Path
import argparse
import hashlib
import json


LOAD_LINE = "$VecherRemotePython = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'diagnose_access.py'), [Text.Encoding]::UTF8)"


def build(destination):
    folder = Path(__file__).resolve().parent
    source = (folder / 'diagnose_access.py').read_text(encoding='utf-8')
    # The native PowerShell 5.1 stdin pipe uses ASCII by default. Keep that
    # transport independent of console locale and do not change its settings.
    source.encode('ascii')
    compile(source, 'diagnose_access.py', 'exec')
    if "\n'@" in source:
        raise ValueError('Python source conflicts with PowerShell here-string delimiter')
    wrapper = (folder / 'check-access.ps1').read_text(encoding='utf-8-sig')
    if wrapper.count(LOAD_LINE) != 1:
        raise ValueError('Expected exactly one adjacent-source loader')
    bundled = wrapper.replace(LOAD_LINE, "$VecherRemotePython = @'\n" + source.rstrip() + "\n'@")
    raw = bundled.encode('utf-8-sig')
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError('Output must not be a symlink')
    destination.write_bytes(raw)
    return {'path': str(destination.resolve()), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'server_diagnostic_sha256': hashlib.sha256(source.encode()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output), indent=2))
