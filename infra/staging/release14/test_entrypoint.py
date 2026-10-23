"""Execute the real PowerShell entry point with isolated command/HTTP fixtures.

Only the public origin is replaced in a temporary script copy, so the full
streaming byte/hash check can use a loopback HTTP server. No real APK, SSH,
Windows signing key or remote data is used by these tests.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PWSH = os.environ.get('VECHER_PWSH') or shutil.which('pwsh')
if not PWSH and Path('/tmp/vecher-powershell-7.6.8/pwsh').is_file():
    PWSH = '/tmp/vecher-powershell-7.6.8/pwsh'
SOURCE = '40e43d85a2e434d4ee2090884a872a507f60fc93'
ORIGIN = 'https://api-test.xn--b1aga8b7a.online'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ps_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(PWSH, 'PowerShell is required for real entry-point execution')
class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='vecher-entrypoint-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / 'reviewed package with spaces'
        self.repo = self.root / 'repo with spaces'
        self.package.mkdir()
        (self.repo / 'artifacts').mkdir(parents=True)
        self.data = b'EXACT_ACCEPTED_APK14_BYTES_FOR_HASH_CHECK_ONLY'
        self.metadata = {
            'version_code': 14, 'version_name': '0.9.0', 'required': False,
            'notes': 'Fixture only', 'sha256': hashlib.sha256(self.data).hexdigest(),
            'bytes': len(self.data),
        }
        (self.repo / 'artifacts/vecher-0.9.0-14.apk').write_bytes(self.data)
        (self.repo / 'artifacts/vecher-0.9.0-14.version.json').write_text(json.dumps(self.metadata))
        self.cfg = {}
        self.cfg_path = self.root / 'fixture.json'
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == '/health':
                    body = json.dumps({'ok': True, 'env': 'staging', 'version': '0.9.0'}).encode()
                elif path == '/app/version':
                    body = json.dumps({**outer.metadata, **outer.cfg.get('public_metadata', {})}).encode()
                elif path == '/download/vecher.apk':
                    body = outer.cfg.get('public_data', outer.data)
                    if outer.cfg.get('redirect_apk'):
                        self.send_response(302)
                        self.send_header('Location', '/unexpected-redirect')
                        self.end_headers()
                        return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json' if path != '/download/vecher.apk' else 'application/octet-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        source = (HERE / 'run-release14.ps1').read_text(encoding='utf-8-sig')
        self.assertEqual(source.count(ORIGIN), 1)
        source = source.replace(ORIGIN, f'http://127.0.0.1:{self.server.server_port}')
        self.script = self.package / 'run-release14.ps1'
        self.script.write_text(source)
        # The leaf is separately exercised by test_runner.py. This fixture
        # records dispatch only, and deliberately cannot build/publish anything.
        leaf = r"""param($Mode,$RepoPath,$FlutterPath,$AndroidSdk)
$VecherCfg = Get-Content -LiteralPath $env:VECHER_ENTRYPOINT_FIXTURE -Raw | ConvertFrom-Json
$VecherRoot = Split-Path -Parent $env:VECHER_ENTRYPOINT_FIXTURE
@{tool='leaf';action=$Mode} | ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $VecherRoot 'events.jsonl')
if ($VecherCfg.fail_leaf -eq $true) { throw 'INTENTIONAL_LEAF_FAILURE' }
Write-Host ('FIXTURE_LEAF_OK=' + $Mode)
"""
        (self.package / 'release-apk14.ps1').write_text(leaf)
        for name in ('server_release.py', 'media_rehearsal.py', 'vecher-publish-apk14.py', 'notes-0.9.0.txt'):
            (self.package / name).write_text('NONEXECUTABLE FIXTURE ONLY\n')
        self.record = {'format': 1, 'source': {'commit': SOURCE},
                       'target': {'version_code': 14, 'version_name': '0.9.0'},
                       'files': [{'path': p.name, 'sha256': digest(p)} for p in self.package.iterdir()]}
        self.write_record()
        self.win = self.root / 'Windows fixture'
        bindir = self.win / 'System32/OpenSSH'
        bindir.mkdir(parents=True)
        native = self.root / 'native.py'
        shutil.copyfile(HERE / 'fixtures/entrypoint_native.py', native)
        native.chmod(0o700)
        for name in ('ssh.exe', 'scp.exe'):
            (bindir / name).symlink_to(native)
        self.env = {**os.environ, 'WINDIR': str(self.win), 'VECHER_ENTRYPOINT_FIXTURE': str(self.cfg_path),
                    'XDG_CACHE_HOME': str(self.root / 'cache'), 'XDG_CONFIG_HOME': str(self.root / 'config'),
                    'XDG_DATA_HOME': str(self.root / 'data')}

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def write_record(self):
        (self.package / 'release-record.json').write_text(json.dumps(self.record))

    def run_mode(self, mode):
        # The byte fixture is owned by the HTTP server and is not JSON data.
        self.cfg_path.write_text(json.dumps({'fail_leaf': False, **{k: v for k, v in self.cfg.items() if k != 'public_data'}}))
        driver = self.root / 'driver.ps1'
        driver.write_text("$ErrorActionPreference='Stop'\n$PSNativeCommandArgumentPassing='Legacy'\ntry { & " +
                          ps_quote(self.script) + ' -Mode ' + ps_quote(mode) + ' -RepoPath ' + ps_quote(self.repo) +
                          " } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }\n")
        result = subprocess.run([str(PWSH), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(driver)],
                                env=self.env, text=True, capture_output=True, timeout=40)
        return result

    def events(self):
        path = self.root / 'events.jsonl'
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_build_is_local_only(self):
        self.assert_ok(self.run_mode('Build'))
        self.assertEqual(self.events(), [{'tool': 'leaf', 'action': 'Build'}])

    def test_prepare_orders_deploy_enable_build_without_publish(self):
        result = self.run_mode('Prepare')
        self.assert_ok(result)
        self.assertEqual([e['action'] for e in self.events() if e.get('action') in ('deploy', 'enable-blogs', 'Build', 'Publish')],
                         ['deploy', 'enable-blogs', 'Build'])
        self.assertIn('VECHER_RELEASE14_READY_FOR_PHYSICAL_DEVICE_TESTS', result.stdout)

    def test_server_never_builds_or_publishes(self):
        self.assert_ok(self.run_mode('Server'))
        self.assertFalse(any(e['tool'] == 'leaf' for e in self.events()))

    def test_deploy_failure_stops_enable_and_build(self):
        self.cfg['fail_action'] = 'deploy'
        self.assertNotEqual(self.run_mode('Prepare').returncode, 0)
        self.assertFalse(any(e.get('action') in ('enable-blogs', 'Build', 'Publish') for e in self.events()))

    def test_enable_failure_stops_build(self):
        self.cfg['fail_action'] = 'enable-blogs'
        self.assertNotEqual(self.run_mode('Prepare').returncode, 0)
        self.assertFalse(any(e['tool'] == 'leaf' for e in self.events()))

    def test_changed_package_stops_before_ssh(self):
        (self.package / 'server_release.py').write_text('CHANGED')
        self.assertNotEqual(self.run_mode('Prepare').returncode, 0)
        self.assertEqual(self.events(), [])

    def test_unbound_required_file_stops(self):
        self.record['files'] = [f for f in self.record['files'] if f['path'] != 'server_release.py']
        self.write_record()
        self.assertNotEqual(self.run_mode('Prepare').returncode, 0)
        self.assertEqual(self.events(), [])

    def test_wrong_ssh_target_stops_before_upload(self):
        self.cfg['ssh_host'] = '192.0.2.1'
        self.assertNotEqual(self.run_mode('Server').returncode, 0)
        self.assertFalse(any(e.get('action') for e in self.events()))

    def test_status_does_not_deploy(self):
        self.assert_ok(self.run_mode('Status'))
        self.assertEqual([e['action'] for e in self.events() if e.get('action') not in (None, 'stage', 'upload')], ['status'])

    def test_disable_blogs_does_not_restore_database_or_build(self):
        self.assert_ok(self.run_mode('DisableBlogs'))
        self.assertEqual([e['action'] for e in self.events() if e.get('action') not in (None, 'stage', 'upload')], ['disable-blogs'])

    def test_accept_never_publishes(self):
        self.assert_ok(self.run_mode('Accept'))
        self.assertEqual(self.events(), [{'tool': 'leaf', 'action': 'Accept'}])

    def test_publish_verifies_full_download(self):
        result = self.run_mode('Publish')
        self.assert_ok(result)
        self.assertEqual(self.events(), [{'tool': 'leaf', 'action': 'Publish'}])
        self.assertIn('VECHER_APK14_PUBLIC_HTTPS_VERIFIED', result.stdout)

    def test_leaf_failure_is_not_publication_success(self):
        self.cfg['fail_leaf'] = True
        result = self.run_mode('Publish')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('VECHER_APK14_PUBLIC_HTTPS_VERIFIED', result.stdout)

    def test_public_wrong_hash_same_size_rejected(self):
        self.cfg['public_data'] = b'X' * len(self.data)
        self.assertNotEqual(self.run_mode('Publish').returncode, 0)

    def test_public_metadata_mismatch_rejected(self):
        self.cfg['public_metadata'] = {'version_code': 13}
        self.assertNotEqual(self.run_mode('Publish').returncode, 0)

    def test_public_redirect_rejected(self):
        self.cfg['redirect_apk'] = True
        self.assertNotEqual(self.run_mode('Publish').returncode, 0)


if __name__ == '__main__':
    unittest.main()
