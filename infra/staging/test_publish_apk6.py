"""Fault injection tests: temporary directories and a fake/local HTTP API only."""

from __future__ import annotations

import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError


MODULE_PATH = Path(__file__).with_name("vecher-publish-apk6.py")
SPEC = importlib.util.spec_from_file_location("vecher_publish_apk6", MODULE_PATH)
p = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = p
SPEC.loader.exec_module(p)


def raw_info(code, name, notes="Проверка обновления"):
    return (json.dumps({"version_code": code, "version_name": name,
                       "notes": notes, "required": False}, ensure_ascii=False) + "\n").encode()


class DiskApi:
    """The backend reads each current file on every request, as production does."""

    def __init__(self, downloads):
        self.downloads = downloads
        self.environment = "staging"
        self.wrong_old_hash = False
        self.wrong_new_hash = False
        self.wrong_version = False
        self.on_new = None
        self.old_hash_reads = 0
        self.new_hash_reads = 0

    def get_json(self, endpoint):
        value = json.loads((self.downloads / "version.json").read_bytes())
        if value["version_code"] == 6 and self.on_new is not None:
            callback, self.on_new = self.on_new, None
            callback()
        if endpoint == "/health":
            return json.dumps({"ok": True, "env": self.environment}).encode()
        if self.wrong_version:
            value["version_code"] = 99
        value["url"] = "http://127.0.0.1:8092/download/vecher.apk"
        return json.dumps(value, ensure_ascii=False).encode()

    def apk_hash(self, expected):
        code = json.loads((self.downloads / "version.json").read_bytes())["version_code"]
        if code == 5:
            self.old_hash_reads += 1
        else:
            self.new_hash_reads += 1
        if (code == 5 and self.wrong_old_hash) or (code == 6 and self.wrong_new_hash):
            return p.FileHash(expected.size, "0" * 64)
        return p.file_hash(self.downloads / "vecher.apk")


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vecher-publisher-test-")
        self.root = Path(self.temporary.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir(mode=0o755)
        self.stage = self.downloads / (".release-6-" + "1" * 32)
        self.stage.mkdir(mode=0o700)
        self.backup = self.root / "private-backups" / "app-before-6"
        self.old_bytes = b"PK\x03\x04old-signed-fixture" * 256
        self.new_bytes = b"PK\x03\x04new-signed-fixture" * 257
        self.old = p.Pin(5, "0.2.0", len(self.old_bytes), hashlib.sha256(self.old_bytes).hexdigest())
        self.new = p.Pin(6, "0.2.1", len(self.new_bytes), hashlib.sha256(self.new_bytes).hexdigest())
        self.old_json = raw_info(5, "0.2.0", "Старый выпуск")
        self.new_json = raw_info(6, "0.2.1", "Исправлены звонки, добавлена мелодия")
        self.write_current(self.old_bytes, self.old_json)
        (self.stage / "vecher-0.2.1-6.apk").write_bytes(self.new_bytes)
        (self.stage / "vecher-0.2.1-6.version.json").write_bytes(self.new_json)
        self.old_pair = p.pair_at(self.downloads)
        self.api = DiskApi(self.downloads)
        self.messages = []
        self.layout = p.Layout(self.downloads, self.backup, "http://127.0.0.1:8092")
        self.publisher = p.Publisher(self.layout, old=self.old, new=self.new,
                                     api=self.api, output=self.messages.append, sleep=lambda _: None)

    def tearDown(self):
        self.temporary.cleanup()

    def write_current(self, apk, info):
        (self.downloads / "vecher.apk").write_bytes(apk)
        (self.downloads / "version.json").write_bytes(info)

    def run_publisher(self):
        self.publisher.run(self.stage)

    def assert_old(self):
        self.assertEqual(p.pair_at(self.downloads), self.old_pair)

    def assert_failed_without_publication(self):
        self.assert_old()
        self.assertNotIn("VECHER_UPDATE_PUBLISHED_OK", self.messages)

    def test_success_backup_and_public_permissions(self):
        self.run_publisher()
        self.assertEqual((self.downloads / "vecher.apk").read_bytes(), self.new_bytes)
        self.assertEqual((self.downloads / "version.json").read_bytes(), self.new_json)
        self.assertEqual(p.pair_at(self.backup), self.old_pair)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o700)
        for name in ("vecher.apk", "version.json", "manifest.json"):
            self.assertEqual(stat.S_IMODE((self.backup / name).stat().st_mode), 0o600)
        for name in ("vecher.apk", "version.json"):
            self.assertEqual(stat.S_IMODE((self.downloads / name).stat().st_mode), 0o644)
        self.assertEqual(self.api.old_hash_reads, 1)
        self.assertEqual(self.api.new_hash_reads, 1)
        self.assertEqual(self.messages[-1], "VECHER_UPDATE_PUBLISHED_OK")

    def test_identical_retry_does_not_replace_files_or_backup(self):
        self.run_publisher()
        manifest = (self.backup / "manifest.json").read_bytes()
        self.messages.clear()
        with mock.patch.object(p.os, "replace", side_effect=AssertionError("retry must not replace")):
            self.run_publisher()
        self.assertEqual((self.backup / "manifest.json").read_bytes(), manifest)
        self.assertIn("FILES_ALREADY_PUBLISHED=0.2.1 (6)", self.messages)
        self.assertEqual(self.messages[-1], "VECHER_UPDATE_PUBLISHED_OK")

    def test_candidate_hash_mismatch_stops_before_backup(self):
        (self.stage / "vecher-0.2.1-6.apk").write_bytes(b"truncated APK")
        with self.assertRaisesRegex(p.ReleaseError, "APK_HASH_OR_SIZE"):
            self.run_publisher()
        self.assert_failed_without_publication()
        self.assertFalse(self.backup.exists())

    def test_metadata_boolean_is_not_integer_version(self):
        invalid = json.loads(self.new_json)
        invalid["version_code"] = True
        (self.stage / "vecher-0.2.1-6.version.json").write_text(json.dumps(invalid))
        with self.assertRaisesRegex(p.ReleaseError, "METADATA_VERSION_OR_TYPES"):
            self.run_publisher()
        self.assert_failed_without_publication()

    def test_duplicate_json_keys_rejected(self):
        (self.stage / "vecher-0.2.1-6.version.json").write_bytes(
            self.new_json.rstrip()[:-1] + b',"version_code":6}')
        with self.assertRaisesRegex(p.ReleaseError, "JSON_DUPLICATE_KEY"):
            self.run_publisher()
        self.assert_failed_without_publication()

    def test_newer_current_release_is_not_overwritten(self):
        self.write_current(b"newer APK", raw_info(7, "0.2.2"))
        newer = p.pair_at(self.downloads)
        with self.assertRaisesRegex(p.ReleaseError, "NEWER_RELEASE_ALREADY_PUBLISHED"):
            self.run_publisher()
        self.assertEqual(p.pair_at(self.downloads), newer)
        self.assertFalse(self.backup.exists())

    def test_same_version_with_different_apk_is_not_idempotent(self):
        self.write_current(b"other build 6", self.new_json)
        different = p.pair_at(self.downloads)
        with self.assertRaises(p.ReleaseError):
            self.run_publisher()
        self.assertEqual(p.pair_at(self.downloads), different)
        self.assertNotIn("VECHER_UPDATE_PUBLISHED_OK", self.messages)

    def test_same_apk_with_different_version_metadata_is_not_idempotent(self):
        self.write_current(self.new_bytes, raw_info(6, "0.2.1", "Другие заметки"))
        different = p.pair_at(self.downloads)
        with self.assertRaises(p.ReleaseError):
            self.run_publisher()
        self.assertEqual(p.pair_at(self.downloads), different)

    def test_wrong_api_environment_stops_before_backup(self):
        self.api.environment = "production"
        with self.assertRaisesRegex(p.ReleaseError, "API_NOT_HEALTHY_STAGING"):
            self.run_publisher()
        self.assert_failed_without_publication()
        self.assertFalse(self.backup.exists())

    def test_api_version_mismatch_stops_before_backup(self):
        self.api.wrong_version = True
        with self.assertRaises(p.ReleaseError):
            self.run_publisher()
        self.assert_failed_without_publication()
        self.assertFalse(self.backup.exists())

    def test_api_old_apk_mismatch_stops_before_backup(self):
        self.api.wrong_old_hash = True
        with self.assertRaisesRegex(p.ReleaseError, "API_APK_HASH_MISMATCH"):
            self.run_publisher()
        self.assert_failed_without_publication()
        self.assertFalse(self.backup.exists())

    def test_symlink_candidate_is_never_followed(self):
        victim = self.root / "untouched-file"
        victim.write_bytes(b"must stay intact")
        candidate = self.stage / "vecher-0.2.1-6.apk"
        candidate.unlink()
        candidate.symlink_to(victim)
        with self.assertRaises((OSError, p.ReleaseError)):
            self.run_publisher()
        self.assertEqual(victim.read_bytes(), b"must stay intact")
        self.assert_failed_without_publication()

    def test_fifo_candidate_is_rejected_without_blocking(self):
        candidate = self.stage / "vecher-0.2.1-6.apk"
        candidate.unlink()
        os.mkfifo(candidate)
        with self.assertRaisesRegex(p.ReleaseError, "FILE_NOT_REGULAR"):
            self.run_publisher()
        self.assert_failed_without_publication()

    def test_stage_must_be_private(self):
        self.stage.chmod(0o755)
        with self.assertRaisesRegex(p.ReleaseError, "STAGE_NOT_PRIVATE"):
            self.run_publisher()
        self.assert_failed_without_publication()

    def test_another_release_lock_is_respected(self):
        with (self.downloads / ".release.lock").open("wb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(p.ReleaseError, "ANOTHER_RELEASE_IS_RUNNING"):
                self.run_publisher()
        self.assert_failed_without_publication()

    def test_failed_backup_copy_never_changes_published_pair_and_can_retry(self):
        with mock.patch.object(p, "durable_copy", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_publisher()
        self.assert_failed_without_publication()
        self.assertFalse(self.backup.exists())
        self.run_publisher()
        self.assertEqual(self.messages[-1], "VECHER_UPDATE_PUBLISHED_OK")

    def test_metadata_rename_failure_restores_old_pair_and_keeps_backup(self):
        real_replace = os.replace
        failed = False

        def injected(source, destination):
            nonlocal failed
            if not failed and source.name.startswith(".ready-") and source.suffix == ".json":
                failed = True
                raise OSError("simulated rename error")
            return real_replace(source, destination)

        with mock.patch.object(p.os, "replace", side_effect=injected):
            with self.assertRaisesRegex(p.ReleaseError, "OLD_RELEASE_RESTORED"):
                self.run_publisher()
        self.assert_failed_without_publication()
        self.assertEqual(p.pair_at(self.backup), self.old_pair)
        self.assertIn("ROLLBACK_API_OK", self.messages)

    def test_fsync_failure_after_apk_rename_also_rolls_back(self):
        real_sync = p.sync_dir
        failed = False

        def injected(directory):
            nonlocal failed
            if (not failed and directory == self.downloads and
                    p.pair_at(self.downloads).apk.sha256 == self.new.sha256):
                failed = True
                raise OSError("simulated fsync failure")
            return real_sync(directory)

        with mock.patch.object(p, "sync_dir", side_effect=injected):
            with self.assertRaisesRegex(p.ReleaseError, "OLD_RELEASE_RESTORED"):
                self.run_publisher()
        self.assert_failed_without_publication()
        self.assertIn("ROLLBACK_API_OK", self.messages)

    def test_post_publish_bad_api_hash_rolls_back_and_retry_succeeds(self):
        self.api.wrong_new_hash = True
        with self.assertRaisesRegex(p.ReleaseError, "OLD_RELEASE_RESTORED"):
            self.run_publisher()
        self.assert_failed_without_publication()
        self.assertEqual(p.pair_at(self.backup), self.old_pair)
        self.assertIn("ROLLBACK_API_OK", self.messages)
        manifest = (self.backup / "manifest.json").read_bytes()
        self.api.wrong_new_hash = False
        self.run_publisher()
        self.assertEqual((self.backup / "manifest.json").read_bytes(), manifest)
        self.assertEqual(self.messages[-1], "VECHER_UPDATE_PUBLISHED_OK")

    def test_corrupt_existing_backup_is_not_overwritten(self):
        self.api.wrong_new_hash = True
        with self.assertRaises(p.ReleaseError):
            self.run_publisher()
        (self.backup / "vecher.apk").write_bytes(b"corrupt backup")
        self.api.wrong_new_hash = False
        with self.assertRaisesRegex(p.ReleaseError, "BACKUP_CONTENT_MISMATCH"):
            self.run_publisher()
        self.assert_failed_without_publication()
        self.assertEqual((self.backup / "vecher.apk").read_bytes(), b"corrupt backup")

    def test_foreign_publication_before_first_rename_is_preserved(self):
        real_copy = p.durable_copy
        foreign_json = raw_info(7, "0.2.2")

        def injected(source, destination, expected, mode):
            result = real_copy(source, destination, expected, mode)
            if destination.name.startswith(".ready-") and destination.suffix == ".json":
                self.write_current(b"foreign 7", foreign_json)
            return result

        with mock.patch.object(p, "durable_copy", side_effect=injected):
            with self.assertRaisesRegex(p.ReleaseError, "CURRENT_RELEASE_CHANGED"):
                self.run_publisher()
        self.assertEqual((self.downloads / "vecher.apk").read_bytes(), b"foreign 7")
        self.assertEqual((self.downloads / "version.json").read_bytes(), foreign_json)
        self.assertFalse(any(line.startswith("FILES_PUBLISHED") for line in self.messages))

    def test_stale_rollback_never_overwrites_foreign_newer_release(self):
        foreign_json = raw_info(7, "0.2.2")
        self.api.on_new = lambda: self.write_current(b"foreign 7", foreign_json)
        with self.assertRaisesRegex(p.ReleaseError, "ROLLBACK_REQUIRES_REVIEW"):
            self.run_publisher()
        self.assertEqual((self.downloads / "vecher.apk").read_bytes(), b"foreign 7")
        self.assertEqual((self.downloads / "version.json").read_bytes(), foreign_json)
        self.assertIn("ROLLBACK_FAILED_OR_BLOCKED=ROLLBACK_BLOCKED_CURRENT_RELEASE_CHANGED", self.messages)

    def test_foreign_publication_during_rollback_preparation_is_preserved(self):
        self.api.wrong_new_hash = True
        real_copy = p.durable_copy
        foreign_json = raw_info(7, "0.2.2")

        def injected(source, destination, expected, mode):
            result = real_copy(source, destination, expected, mode)
            if destination.name.startswith(".rollback-") and destination.suffix == ".json":
                self.write_current(b"foreign 7", foreign_json)
            return result

        with mock.patch.object(p, "durable_copy", side_effect=injected):
            with self.assertRaisesRegex(p.ReleaseError, "ROLLBACK_REQUIRES_REVIEW"):
                self.run_publisher()
        self.assertEqual((self.downloads / "vecher.apk").read_bytes(), b"foreign 7")
        self.assertEqual((self.downloads / "version.json").read_bytes(), foreign_json)


class HttpClientTests(unittest.TestCase):
    def test_real_http_stream_and_redirect_rejection(self):
        payload = b"PK\x03\x04APK test stream" * 32
        expected = p.FileHash(len(payload), hashlib.sha256(payload).hexdigest())
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-follow")
                    self.end_headers()
                    return
                body = payload if self.path == "/download/vecher.apk" else b'{"ok":true,"env":"staging"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = p.ApiClient("http://127.0.0.1:" + str(server.server_port))
            self.assertEqual(p.decode_json(client.get_json("/health"))["env"], "staging")
            self.assertEqual(client.apk_hash(expected), expected)
            with self.assertRaisesRegex(p.ReleaseError, "API_APK_LENGTH_MISMATCH"):
                client.apk_hash(p.FileHash(expected.size + 1, expected.sha256))
            with self.assertRaises(HTTPError):
                client.get_json("/redirect")
            self.assertNotIn("/must-not-follow", requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
