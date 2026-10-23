"""Release14 fault injection: temporary files and mocked HTTP/commands; no network or Docker."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError


MODULE_PATH = Path(__file__).with_name("vecher-publish-apk14.py")
SPEC = importlib.util.spec_from_file_location("vecher_publish_apk14", MODULE_PATH)
p = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = p
SPEC.loader.exec_module(p)


OLD_PAYLOAD = b"PK\x03\x04old-signed-fixture" * 256
NEW_PAYLOAD = b"PK\x03\x04new-signed-fixture" * 257


def raw_info(code, name, notes="Проверка обновления"):
    payload = OLD_PAYLOAD if code == 13 else NEW_PAYLOAD
    return (json.dumps({"version_code": code, "version_name": name,
                       "notes": notes, "required": False,
                       "sha256": hashlib.sha256(payload).hexdigest(),
                       "bytes": len(payload)}, ensure_ascii=False) + "\n").encode()


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
        if value["version_code"] == 14 and self.on_new is not None:
            callback, self.on_new = self.on_new, None
            callback()
        if endpoint == "/health":
            return json.dumps({"ok": True, "env": self.environment, "version": p.API_VERSION}).encode()
        if self.wrong_version:
            value["version_code"] = 99
        value["url"] = "http://127.0.0.1:8092/download/vecher.apk"
        value["download_path"] = "/download/vecher.apk?sha256=" + value["sha256"]
        return json.dumps(value, ensure_ascii=False).encode()

    def apk_hash(self, expected):
        code = json.loads((self.downloads / "version.json").read_bytes())["version_code"]
        if code == 13:
            self.old_hash_reads += 1
        else:
            self.new_hash_reads += 1
        if (code == 13 and self.wrong_old_hash) or (code == 14 and self.wrong_new_hash):
            return p.FileHash(expected.size, "0" * 64)
        return p.file_hash(self.downloads / "vecher.apk")


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vecher-publisher-test-")
        self.root = Path(self.temporary.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir(mode=0o755)
        self.stage = self.downloads / (".release-14-" + "1" * 32)
        self.stage.mkdir(mode=0o700)
        self.backup = self.root / "private-backups" / "app-before-14"
        self.old_bytes = b"PK\x03\x04old-signed-fixture" * 256
        self.new_bytes = b"PK\x03\x04new-signed-fixture" * 257
        self.old = p.Pin(13, "0.8.0", len(self.old_bytes), hashlib.sha256(self.old_bytes).hexdigest())
        self.new = p.Pin(14, "0.9.0", len(self.new_bytes), hashlib.sha256(self.new_bytes).hexdigest())
        self.old_json = raw_info(13, "0.8.0", "Старый выпуск")
        self.new_json = raw_info(14, "0.9.0", "Исправлены звонки, добавлена мелодия")
        self.write_current(self.old_bytes, self.old_json)
        (self.stage / "vecher-0.9.0-14.apk").write_bytes(self.new_bytes)
        (self.stage / "vecher-0.9.0-14.version.json").write_bytes(self.new_json)
        self.old_pair = p.pair_at(self.downloads)
        self.api = DiskApi(self.downloads)
        self.messages = []
        self.layout = p.Layout(self.downloads, self.backup, "http://127.0.0.1:8092")
        self.publisher = p.Publisher(self.layout, old=self.old, new=self.new,
                                     api=self.api, output=self.messages.append, sleep=lambda _: None,
                                     backend_check=lambda: None, declaration_check=lambda *_: None,
                                     old_metadata_sha256=hashlib.sha256(self.old_json).hexdigest())

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
        self.assertIn("FILES_ALREADY_PUBLISHED=0.9.0 (14)", self.messages)
        self.assertEqual(self.messages[-1], "VECHER_UPDATE_PUBLISHED_OK")

    def test_candidate_hash_mismatch_stops_before_backup(self):
        (self.stage / "vecher-0.9.0-14.apk").write_bytes(b"truncated APK")
        with self.assertRaisesRegex(p.ReleaseError, "APK_HASH_OR_SIZE"):
            self.run_publisher()
        self.assert_failed_without_publication()
        self.assertFalse(self.backup.exists())

    def test_metadata_boolean_is_not_integer_version(self):
        invalid = json.loads(self.new_json)
        invalid["version_code"] = True
        (self.stage / "vecher-0.9.0-14.version.json").write_text(json.dumps(invalid))
        with self.assertRaisesRegex(p.ReleaseError, "METADATA_VERSION_OR_TYPES"):
            self.run_publisher()
        self.assert_failed_without_publication()

    def test_duplicate_json_keys_rejected(self):
        (self.stage / "vecher-0.9.0-14.version.json").write_bytes(
            self.new_json.rstrip()[:-1] + b',"version_code":14}')
        with self.assertRaisesRegex(p.ReleaseError, "JSON_DUPLICATE_KEY"):
            self.run_publisher()
        self.assert_failed_without_publication()

    def test_newer_current_release_is_not_overwritten(self):
        self.write_current(b"newer APK", raw_info(15, "0.10.0"))
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
        self.write_current(self.new_bytes, raw_info(14, "0.9.0", "Другие заметки"))
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
        candidate = self.stage / "vecher-0.9.0-14.apk"
        candidate.unlink()
        candidate.symlink_to(victim)
        with self.assertRaises((OSError, p.ReleaseError)):
            self.run_publisher()
        self.assertEqual(victim.read_bytes(), b"must stay intact")
        self.assert_failed_without_publication()

    def test_fifo_candidate_is_rejected_without_blocking(self):
        candidate = self.stage / "vecher-0.9.0-14.apk"
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
        self.assertIn("ROLLBACK_NOT_NEEDED", self.messages)

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
        foreign_json = raw_info(15, "0.10.0")

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
        foreign_json = raw_info(15, "0.10.0")
        self.api.on_new = lambda: self.write_current(b"foreign 7", foreign_json)
        with self.assertRaisesRegex(p.ReleaseError, "ROLLBACK_REQUIRES_REVIEW"):
            self.run_publisher()
        self.assertEqual((self.downloads / "vecher.apk").read_bytes(), b"foreign 7")
        self.assertEqual((self.downloads / "version.json").read_bytes(), foreign_json)
        self.assertIn("ROLLBACK_FAILED_OR_BLOCKED=ROLLBACK_BLOCKED_CURRENT_RELEASE_CHANGED", self.messages)

    def test_foreign_publication_during_rollback_preparation_is_preserved(self):
        self.api.wrong_new_hash = True
        real_copy = p.durable_copy
        foreign_json = raw_info(15, "0.10.0")

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

    def test_old_release_also_requires_all_six_integrity_fields(self):
        value = json.loads(self.old_json)
        del value["sha256"]
        del value["bytes"]
        self.write_current(self.old_bytes, json.dumps(value).encode())
        before = p.pair_at(self.downloads)
        with self.assertRaisesRegex(p.ReleaseError, "METADATA_FIELDS_MISMATCH"):
            self.run_publisher()
        self.assertEqual(p.pair_at(self.downloads), before)
        self.assertFalse(self.backup.exists())

    def test_metadata_types_integrity_and_unknown_fields_are_strict(self):
        variants = (
            {"required": 0}, {"required": True}, {"bytes": str(self.new.size)},
            {"bytes": True}, {"sha256": "0" * 64}, {"notes": " "},
            {"notes": "я" * 2049}, {"version_name": "0.8.1"}, {"extra": "ignored?"},
        )
        target = self.stage / "vecher-0.9.0-14.version.json"
        for changes in variants:
            with self.subTest(changes=changes):
                target.write_bytes(json.dumps({**json.loads(self.new_json), **changes}).encode())
                with self.assertRaises(p.ReleaseError):
                    self.run_publisher()
                self.assert_old()
                self.assertFalse(self.backup.exists())

    def test_backend_failure_precedes_any_backup_or_replacement(self):
        self.publisher.backend_check = lambda: p.fail("LIVE_RUNTIME_SOURCE_MISMATCH")
        with self.assertRaisesRegex(p.ReleaseError, "LIVE_RUNTIME_SOURCE_MISMATCH"):
            self.run_publisher()
        self.assert_failed_without_publication()
        self.assertFalse(self.backup.exists())
        self.assertNotIn("UPLOAD_HASH_OK", self.messages)

    def test_identical_retry_from_another_stage_never_replaces_any_file(self):
        self.run_publisher()
        other = self.downloads / (".release-14-" + "2" * 32)
        other.mkdir(mode=0o700)
        (other / "vecher-0.9.0-14.apk").write_bytes(self.new_bytes)
        (other / "vecher-0.9.0-14.version.json").write_bytes(self.new_json)
        with mock.patch.object(p.os, "replace", side_effect=AssertionError("must not replace")):
            self.publisher.run(other)
        self.assertIn("FILES_ALREADY_PUBLISHED=0.9.0 (14)", self.messages)
        self.assertEqual(p.pair_at(self.backup), self.old_pair)

    def test_observed_partial_pairs_require_review_without_automatic_recovery(self):
        for apk, manifest in ((self.old_bytes, self.new_json), (self.new_bytes, self.old_json)):
            with self.subTest(apk=apk[:6]):
                self.write_current(apk, manifest)
                before = p.pair_at(self.downloads)
                with self.assertRaisesRegex(p.ReleaseError, "PARTIAL_RELEASE_REQUIRES_REVIEW"):
                    self.run_publisher()
                self.assertEqual(p.pair_at(self.downloads), before)
                self.assertFalse(self.backup.exists())

    def test_failed_second_rename_restores_the_original_hash_bound_pair(self):
        real_replace = os.replace
        failed = False

        def injected(source, destination):
            nonlocal failed
            if not failed and source.name.startswith(".ready-") and source.suffix == ".apk":
                failed = True
                # The reviewed protocol exposes a hash mismatch, not a wrong
                # advertised APK, while the two renames are incomplete.
                self.assertEqual(p.pair_at(self.downloads), p.Pair(self.old_pair.apk, self.new_json))
                raise OSError("simulated second-rename failure")
            return real_replace(source, destination)

        with mock.patch.object(p.os, "replace", side_effect=injected):
            with self.assertRaisesRegex(p.ReleaseError, "OLD_RELEASE_RESTORED"):
                self.run_publisher()
        self.assert_old()
        self.assertIn("ROLLBACK_API_OK", self.messages)

    def test_signal_interruption_between_renames_rolls_back(self):
        real_replace = os.replace
        interrupted = False

        def injected(source, destination):
            nonlocal interrupted
            if not interrupted and source.name.startswith(".ready-") and source.suffix == ".apk":
                interrupted = True
                p.fail("INTERRUPTED")
            return real_replace(source, destination)

        with mock.patch.object(p.os, "replace", side_effect=injected):
            with self.assertRaisesRegex(p.ReleaseError, "OLD_RELEASE_RESTORED"):
                self.run_publisher()
        self.assert_old()
        self.assertIn("PUBLISH_FAILED=INTERRUPTED", self.messages)

    def test_closed_output_does_not_abort_publication_or_rollback(self):
        class ClosedOutput:
            def write(self, _value):
                raise BrokenPipeError("SSH channel closed")

            def flush(self):
                raise BrokenPipeError("SSH channel closed")

        self.publisher.output = p.safe_output
        with redirect_stdout(ClosedOutput()):
            self.run_publisher()
        self.assertEqual((self.downloads / "vecher.apk").read_bytes(), self.new_bytes)
        self.write_current(self.old_bytes, self.old_json)
        self.api.wrong_new_hash = True
        with redirect_stdout(ClosedOutput()):
            with self.assertRaisesRegex(p.ReleaseError, "OLD_RELEASE_RESTORED"):
                self.run_publisher()
        self.assert_old()

    def test_hardlinked_candidate_is_not_copied(self):
        candidate = self.stage / "vecher-0.9.0-14.apk"
        os.link(candidate, self.root / "another-link")
        with self.assertRaisesRegex(p.ReleaseError, "HARDLINKS"):
            self.run_publisher()
        self.assert_failed_without_publication()

    def test_old_stage_name_and_unsafe_stage_paths_are_refused(self):
        invalid = (
            self.downloads / (".release-13-" + "1" * 32),
            self.downloads / (".release-14-" + "A" * 32),
            Path(".release-14-" + "1" * 32),
            self.downloads / "child" / ".." / self.stage.name,
        )
        for stage in invalid:
            with self.subTest(stage=stage):
                with self.assertRaisesRegex(p.ReleaseError, "INVALID_STAGE_PATH"):
                    self.publisher.run(stage)
        self.assert_old()

    def test_changed_predecessor_metadata_stops_before_backup(self):
        self.write_current(self.old_bytes, raw_info(13, "0.8.0", "Unexpected replacement notes"))
        unchanged = p.pair_at(self.downloads)
        with self.assertRaisesRegex(p.ReleaseError, "PREVIOUS_METADATA_HASH_MISMATCH"):
            self.run_publisher()
        self.assertEqual(p.pair_at(self.downloads), unchanged)
        self.assertFalse(self.backup.exists())

    def test_physical_acceptance_gate_precedes_backup_and_also_guards_idempotent_retry(self):
        self.publisher.declaration_check = lambda *_: p.fail("PHYSICAL_DEVICE_ACCEPTANCE_INCOMPLETE")
        with self.assertRaisesRegex(p.ReleaseError, "PHYSICAL_DEVICE_ACCEPTANCE_INCOMPLETE"):
            self.run_publisher()
        self.assert_failed_without_publication()
        self.assertFalse(self.backup.exists())
        self.publisher.declaration_check = lambda *_: None
        self.run_publisher()
        published = p.pair_at(self.downloads)
        self.publisher.declaration_check = lambda *_: p.fail("PHONE_RECEIPT_HASH_MISMATCH")
        with self.assertRaisesRegex(p.ReleaseError, "PHONE_RECEIPT_HASH_MISMATCH"):
            self.run_publisher()
        self.assertEqual(p.pair_at(self.downloads), published)


class ArgumentAndPinTests(unittest.TestCase):
    def test_release_identity_and_default_backup_are_bound_to_candidate(self):
        self.assertEqual(p.OLD, p.Pin(
            13, "0.8.0", 36591005,
            "a87d14f11e874f81509e4c6964f1c7355103a61e24d4e279ff61e454102bc1bc"))
        self.assertEqual((p.NEW_CODE, p.NEW_NAME), (14, "0.9.0"))
        pin = p.Pin(14, "0.9.0", 1234, "a" * 64)
        other = p.Pin(14, "0.9.0", 1234, "a" * 63 + "b")
        first = p.Publisher(new=pin, backend_check=lambda: None)
        second = p.Publisher(new=other, backend_check=lambda: None)
        self.assertEqual(first.layout.backup.name, "app-before-0.9.0-14-" + pin.sha256)
        self.assertNotEqual(first.layout.backup, second.layout.backup)

    def test_hash_is_exact_lowercase_hex_and_size_is_bounded_decimal(self):
        self.assertEqual(p.expected_sha256("a" * 64), "a" * 64)
        for value in ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 64, "a" * 64 + "\n"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    p.expected_sha256(value)
        for value in ("2", "36591005", "209715200"):
            self.assertEqual(p.expected_size(value), int(value))
        for value in ("", "0", "1", "-1", "+2", "02", "2.0", "true", "209715201", "2\n", "9" * 100):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    p.expected_size(value)

    def test_cli_requires_both_explicit_candidate_pins_before_environment_checks(self):
        argv = ["publisher", "--stage", "/opt/vecher-test/downloads/.release-14-" + "1" * 32]
        for extra in ([], ["--expected-sha256", "a" * 64], ["--expected-size", "100"]):
            with self.subTest(extra=extra), mock.patch.object(sys, "argv", argv + extra), \
                    mock.patch.object(p, "Publisher") as publisher, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    p.main()
                self.assertEqual(error.exception.code, 2)
                publisher.assert_not_called()

    def test_main_uses_supplied_hash_size_fixed_version_and_all_three_signals(self):
        stage = "/opt/vecher-test/downloads/.release-14-" + "1" * 32
        argv = ["publisher", "--stage", stage, "--expected-sha256", "a" * 64,
                "--expected-size", "36591005", "--expected-metadata-sha256", "b" * 64,
                "--expected-source-receipt-sha256", "c" * 64,
                "--expected-phone-receipt-sha256", "d" * 64]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(p.os, "geteuid", return_value=0), \
                mock.patch.object(p.os, "umask"), \
                mock.patch.object(p.socket, "gethostname", return_value="elegant-red"), \
                mock.patch.object(p.signal, "signal") as handler, \
                mock.patch.object(p, "Publisher") as publisher:
            self.assertEqual(p.main(), 0)
            publisher.assert_called_once_with(new=p.Pin(14, "0.9.0", 36591005, "a" * 64),
                                              metadata_sha256="b" * 64,
                                              source_receipt_sha256="c" * 64,
                                              phone_receipt_sha256="d" * 64)
            publisher.return_value.run.assert_called_once_with(Path(stage))
            self.assertEqual({call.args[0] for call in handler.call_args_list},
                             {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
            with self.assertRaisesRegex(p.ReleaseError, "INTERRUPTED"):
                handler.call_args_list[-1].args[1](signal.SIGHUP, None)

    def test_unexpected_command_errors_never_include_captured_private_output(self):
        result = SimpleNamespace(returncode=1, stdout="private", stderr="secret settings")
        with mock.patch.object(p.subprocess, "run", return_value=result) as command:
            with self.assertRaisesRegex(p.ReleaseError, "^SAFE_COMMAND_FAILED$"):
                p.command_output(["docker", "exec", "vecher-test-api-1"], "SAFE_COMMAND_FAILED")
            self.assertEqual(command.call_args.kwargs["stdin"], p.subprocess.DEVNULL)
            self.assertFalse(command.call_args.kwargs.get("shell", False))

    def test_check_backend_mode_is_read_only_and_cannot_include_publication_arguments(self):
        with mock.patch.object(sys, "argv", ["publisher", "--check-backend"]), \
                mock.patch.object(p.os, "geteuid", return_value=0), \
                mock.patch.object(p.os, "umask"), \
                mock.patch.object(p.socket, "gethostname", return_value="elegant-red"), \
                mock.patch.object(p.signal, "signal"), \
                mock.patch.object(p, "check_backend") as gate, \
                mock.patch.object(p, "Publisher") as publisher, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(p.main(), 0)
            publisher.assert_not_called()
            gate.assert_called_once()
            self.assertIn("BLOGS_BACKEND_READY=0.9.0 (14)", output.getvalue())
        with mock.patch.object(sys, "argv", ["publisher", "--check-backend", "--expected-size", "123"]), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            p.main()
        self.assertEqual(error.exception.code, 2)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vecher-apk14-receipts-")
        self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name)
        self.candidate = p.Pair(p.FileHash(len(NEW_PAYLOAD), hashlib.sha256(NEW_PAYLOAD).hexdigest()), raw_info(14, "0.9.0"))
        common = {
            "format": 1, "source_sha": p.SOURCE_SHA, "version_code": 14, "version_name": "0.9.0",
            "apk_sha256": self.candidate.apk.sha256, "apk_bytes": self.candidate.apk.size,
            "metadata_sha256": self.candidate.json_hash.sha256,
            "signing_certificate_sha256": p.CERTIFICATE_SHA256,
            "publisher_sha256": p.file_hash(MODULE_PATH, p.MAX_OPENAPI).sha256,
        }
        self.source = common | {
            "previous_source_sha": p.PREVIOUS_SOURCE_SHA, "builder_sha256": "b" * 64,
            "builder_apk_sha256": self.candidate.apk.sha256, "created_utc": "2026-10-23 21:10:00.1234567 UTC",
        }
        self.phone = common | {
            "acceptance_version": p.ACCEPTANCE_VERSION, "source_receipt_sha256": "",
            "backend_source_sha": p.SOURCE_SHA, "backend_runtime_sha256": p.RUNTIME_SHA,
            "backend_schema": "0006_public_blogs", "backend_api_version": p.API_VERSION,
            "operator_label": "Test operator", "devices": {"partner_a": "Android A", "partner_b": "Android B", "reader": "Reader test device"},
            "checks": {key: True for key in p.PHONE_CHECKS}, "accepted_utc": "2026-10-23 22:10:00.1234567 UTC",
        }
        self.save()

    def save(self):
        source_raw = json.dumps(self.source, ensure_ascii=False).encode()
        self.source_sha = hashlib.sha256(source_raw).hexdigest()
        if isinstance(self.phone, dict):
            self.phone["source_receipt_sha256"] = self.source_sha
        phone_raw = json.dumps(self.phone, ensure_ascii=False).encode()
        self.phone_sha = hashlib.sha256(phone_raw).hexdigest()
        (self.stage / "vecher-0.9.0-14.source.json").write_bytes(source_raw)
        (self.stage / "vecher-0.9.0-14.phone.json").write_bytes(phone_raw)

    def check(self):
        p.verify_release_declarations(self.stage, self.candidate,
                                      metadata_sha256=self.candidate.json_hash.sha256,
                                      source_receipt_sha256=self.source_sha, phone_receipt_sha256=self.phone_sha)

    def test_exact_operator_records_are_accepted(self):
        self.check()

    def test_missing_records_and_missing_explicit_pins_are_rejected(self):
        with self.assertRaisesRegex(p.ReleaseError, "RELEASE_DECLARATION_PINS_REQUIRED"):
            p.verify_release_declarations(self.stage, self.candidate, metadata_sha256=None,
                                          source_receipt_sha256=self.source_sha, phone_receipt_sha256=self.phone_sha)
        (self.stage / "vecher-0.9.0-14.phone.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.check()

    def test_raw_hash_pins_cover_metadata_and_both_receipts(self):
        for name, code in (("vecher-0.9.0-14.source.json", "SOURCE_RECEIPT_HASH_MISMATCH"),
                           ("vecher-0.9.0-14.phone.json", "PHONE_RECEIPT_HASH_MISMATCH")):
            with self.subTest(name=name):
                path = self.stage / name
                raw = path.read_bytes()
                path.write_bytes(raw + b" ")
                with self.assertRaisesRegex(p.ReleaseError, code):
                    self.check()
                path.write_bytes(raw)
        with self.assertRaisesRegex(p.ReleaseError, "CANDIDATE_METADATA_HASH_MISMATCH"):
            p.verify_release_declarations(self.stage, self.candidate, metadata_sha256="0" * 64,
                                          source_receipt_sha256=self.source_sha, phone_receipt_sha256=self.phone_sha)

    def test_wrong_source_signer_version_builder_and_publisher_records_refuse(self):
        for key, value in (("source_sha", p.PREVIOUS_SOURCE_SHA), ("signing_certificate_sha256", "f" * 64),
                           ("version_code", True), ("publisher_sha256", "f" * 64),
                           ("builder_apk_sha256", "f" * 64), ("builder_sha256", "unknown")):
            with self.subTest(key=key):
                previous = self.source[key]
                self.source[key] = value
                self.save()
                with self.assertRaises(p.ReleaseError):
                    self.check()
                self.source[key] = previous
        self.save()
        self.check()

    def test_physical_checks_are_all_explicit_boolean_true(self):
        key = p.PHONE_CHECKS[0]
        for value in (False, "true", 1, None):
            with self.subTest(value=value):
                self.phone["checks"][key] = value
                self.save()
                with self.assertRaisesRegex(p.ReleaseError, "PHYSICAL_DEVICE_ACCEPTANCE_INCOMPLETE"):
                    self.check()
        self.phone["checks"].pop(key)
        self.save()
        with self.assertRaisesRegex(p.ReleaseError, "PHYSICAL_DEVICE_ACCEPTANCE_INCOMPLETE"):
            self.check()

    def test_phone_context_array_and_identity_cannot_be_substituted(self):
        for key, value in (("apk_sha256", "e" * 64), ("backend_schema", "0005_content"),
                           ("backend_runtime_sha256", "f" * 64), ("accepted_utc", "2026-02-30 22:10:00.1234567 UTC"),
                           ("accepted_utc", "2026-10-23 20:10:00.1234567 UTC"), ("operator_label", "")):
            with self.subTest(key=key, value=value):
                previous = self.phone[key]
                self.phone[key] = value
                self.save()
                with self.assertRaises(p.ReleaseError):
                    self.check()
                self.phone[key] = previous
        self.phone = [self.phone]
        self.save()
        with self.assertRaisesRegex(p.ReleaseError, "PHONE_RECEIPT_FIELDS_MISMATCH"):
            self.check()


class ServerJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vecher-apk14-journal-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkpoint = self.root / ("release-14-" + "a" * 32)
        self.checkpoint.mkdir(mode=0o700)
        self.pointer = {"schema": 1, "release": 14, "checkpoint": str(self.checkpoint)}
        self.journal = {
            "schema": 1, "release": 14, "phase": "blogs_ready", "source_sha": p.SOURCE_SHA,
            "previous_source_sha": p.PREVIOUS_SOURCE_SHA, "source_tree": p.SOURCE_TREE, "runtime_sha256": p.RUNTIME_SHA,
            "api_image_id": "sha256:" + "a" * 64, "worker_image_id": "sha256:" + "b" * 64,
            "rehearsal_ok": True, "rollback_rehearsal_ok": True, "media_rehearsal_ok": True,
            "traffic_may_have_opened": True,
        }
        self.layout = p.Layout(self.root / "downloads", self.root / "backup", journal_root=self.root)
        self.save()

    def save(self):
        for path, value in ((self.root / "release14-current.json", self.pointer), (self.checkpoint / "journal.json", self.journal)):
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def test_exact_private_ready_journal_is_accepted(self):
        self.assertEqual(p.check_server_journal(self.layout), self.journal)

    def test_only_blogs_ready_with_completed_rehearsals_can_publish(self):
        for phase in ("core_ready", "blogs_disabled", "enabling", "needs_forward_recovery"):
            self.journal["phase"] = phase
            self.save()
            with self.subTest(phase=phase), self.assertRaisesRegex(p.ReleaseError, "SERVER_JOURNAL_NOT_READY"):
                p.check_server_journal(self.layout)
        self.journal["phase"] = "blogs_ready"
        for key in ("rehearsal_ok", "rollback_rehearsal_ok", "media_rehearsal_ok", "traffic_may_have_opened"):
            self.journal[key] = 1
            self.save()
            with self.subTest(key=key), self.assertRaisesRegex(p.ReleaseError, "SERVER_JOURNAL_NOT_READY"):
                p.check_server_journal(self.layout)
            self.journal[key] = True

    def test_wrong_release_source_runtime_or_image_is_rejected(self):
        for key, value in (("release", True), ("source_sha", p.PREVIOUS_SOURCE_SHA),
                           ("source_tree", "f" * 40), ("runtime_sha256", "f" * 64),
                           ("worker_image_id", "latest")):
            previous = self.journal[key]
            self.journal[key] = value
            self.save()
            with self.subTest(key=key), self.assertRaises(p.ReleaseError):
                p.check_server_journal(self.layout)
            self.journal[key] = previous

    def test_journal_permissions_symlinks_and_checkpoint_escape_are_refused(self):
        journal_path = self.checkpoint / "journal.json"
        journal_path.chmod(0o644)
        with self.assertRaisesRegex(p.ReleaseError, "SERVER_JOURNAL_NOT_PRIVATE"):
            p.check_server_journal(self.layout)
        journal_path.chmod(0o600)
        raw = journal_path.read_bytes()
        journal_path.unlink()
        target = self.root / "outside.json"
        target.write_bytes(raw)
        target.chmod(0o600)
        journal_path.symlink_to(target)
        with self.assertRaises(OSError):
            p.check_server_journal(self.layout)
        self.pointer["checkpoint"] = str(self.root / ".." / self.checkpoint.name)
        (self.root / "release14-current.json").write_text(json.dumps(self.pointer))
        with self.assertRaisesRegex(p.ReleaseError, "SERVER_JOURNAL_CHECKPOINT_INVALID"):
            p.check_server_journal(self.layout)


def public_contract():
    paths = {route: {} for route, _method in (
        p.LEGACY_ROUTES + p.EVENINGS_ROUTES + p.VIDEOS_ROUTES + p.CONTENT_ROUTES + p.BLOG_ROUTES)}
    for route, method in p.LEGACY_ROUTES + p.EVENINGS_ROUTES + p.CONTENT_ROUTES + p.BLOG_ROUTES:
        paths[route][method] = {}
    for route, method in p.VIDEOS_ROUTES:
        paths[route][method] = {"parameters": [{
            "name": "playback", "in": "query", "required": False,
            "schema": {"type": "string", "default": "legacy", "enum": ["legacy", "platform-v1"]}}]}
    paths["/content/feed"]["get"]["parameters"] = [
        {"name": "format", "in": "query", "required": False,
         "schema": {"type": "string", "default": "all", "enum": ["all", "video", "article"]}},
        {"name": "discover", "in": "query", "required": False,
         "schema": {"type": "boolean", "default": False}},
    ]
    for route, method in (("/content/feed", "get"), ("/content/{content_id}", "get"),
                          ("/content/{content_id}/state", "patch")):
        paths[route][method].setdefault("parameters", []).append({
            "name": "hosted_video", "in": "query", "required": False,
            "schema": {"type": "boolean", "default": False}})
    return {"info": {"version": "0.9.0"}, "paths": paths, "components": {"schemas": {
        name: {"properties": {"core_plans_unlimited": {"default": True},
                              "free_plans_left": {"default": 1}}}
        for name in ("PlanOut", "SubscriptionOut")
    } | {"VersionOut": {"properties": {
        "sha256": {"type": "string"}, "bytes": {"type": "integer"},
        "download_path": {"type": "string"}},
        "required": ["sha256", "bytes", "download_path"]}}}}


class BackendGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vecher-publisher-backend-mock-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.layout = p.Layout(root / "downloads", root / "backup", repo=root / "repo", content=root / "content",
                               media=root / "blog-media", journal_root=root / "journal")
        for directory in (self.layout.downloads, self.layout.repo, self.layout.content, self.layout.media):
            directory.mkdir()
        self.journal = {"api_image_id": "sha256:" + "a" * 64, "worker_image_id": "sha256:" + "b" * 64}
        self.document = public_contract()
        self.inspection = {
            "running": True,
            "image": self.journal["api_image_id"],
            "labels": {"com.docker.compose.project": "vecher-test", "com.docker.compose.service": "api"},
            "ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8092"}]},
            "mounts": [
                {"Type": "bind", "Source": str(self.layout.downloads), "Destination": "/srv/downloads", "RW": True},
                {"Type": "bind", "Source": str(self.layout.content), "Destination": "/srv/public-content", "RW": False},
                {"Type": "bind", "Source": str(self.layout.media), "Destination": "/srv/blog-media", "RW": True},
            ],
        }
        self.worker = {
            "running": True, "restarting": False, "oom_killed": False, "restart_count": 0,
            "read_only": True, "image": self.journal["worker_image_id"],
            "command": ["python", "-m", "app.blog_media_worker"],
            "labels": {"com.docker.compose.project": "vecher-test", "com.docker.compose.service": "public-video-worker"},
            "mounts": [{"Type": "bind", "Source": str(self.layout.media), "Destination": "/srv/blog-media", "RW": True}],
        }
        self.responses = {
            "SERVER_SOURCE_CHECK_FAILED": p.SOURCE_SHA,
            "SERVER_WORKTREE_CHECK_FAILED": "",
            "LIVE_RUNTIME_CHECK_FAILED": p.RUNTIME_SHA,
            "LIVE_SCHEMA_CHECK_FAILED": p.SCHEMA_OK,
            "LIVE_BLOG_SETTINGS_CHECK_FAILED": "BLOGS_ENABLED_AND_MEDIA_PATH_OK",
            "LIVE_WORKER_RUNTIME_CHECK_FAILED": p.RUNTIME_SHA,
            "LIVE_WORKER_TOOLS_CHECK_FAILED": "MEDIA_WORKER_TOOLS_AND_FLAG_OK",
            "LIVE_WORKER_DATABASE_CHECK_FAILED": "WORKER_SCHEMA_HEAD_OK=0006_public_blogs",
            "LIVE_VIDEO_CATALOGUE_CHECK_FAILED": p.CATALOGUE_OK,
            "LIVE_PLATFORM_CATALOGUE_CHECK_FAILED": p.PLATFORM_CATALOGUE_OK,
            "LIVE_CONTENT_SNAPSHOT_CHECK_FAILED": "CONTENT_SNAPSHOT_OK=" + "a" * 64 + ";ITEMS=18",
        }
        self.commands = []
        self.api = mock.Mock()
        self.api.get_json.side_effect = lambda endpoint, **_kwargs: json.dumps(
            {"ok": True, "env": "staging", "version": p.API_VERSION}
            if endpoint == "/health" else self.document).encode()

    def command(self, args, code, **kwargs):
        self.commands.append(args)
        if code == "LIVE_CONTAINER_CHECK_FAILED":
            return json.dumps(self.inspection)
        if code == "LIVE_WORKER_CHECK_FAILED":
            return json.dumps(self.worker)
        return self.responses[code]

    def check(self):
        with mock.patch.object(p, "command_output", side_effect=self.command), \
                mock.patch.object(p, "check_server_journal", return_value=self.journal):
            p.check_backend(self.layout, self.api)

    def test_valid_backend_runs_only_read_only_commands_and_anonymous_get_probes(self):
        self.check()
        self.assertEqual(p.SOURCE_SHA, "40e43d85a2e434d4ee2090884a872a507f60fc93")
        self.assertEqual(p.RUNTIME_SHA, "520fb35ae85b4778f584fd4023d048fb69ad7b9afffcaba3a6d750b9fb1b0618")
        self.assertTrue(all(command[0] in {"git", "docker"} for command in self.commands))
        self.assertFalse(any(value in {"upgrade", "restart", "stop", "up", "down", "build", "pull"}
                             for command in self.commands for value in command))
        self.assertIn(mock.call("/content/feed?format=article&discover=true"),
                      self.api.require_unauthorized.call_args_list)

    def test_wrong_source_dirty_tree_runtime_schema_or_catalogue_refuses(self):
        bad = (
            ("SERVER_SOURCE_CHECK_FAILED", "other", "SERVER_SOURCE_MISMATCH"),
            ("SERVER_WORKTREE_CHECK_FAILED", " M backend/app/main.py", "SERVER_WORKTREE_CHANGED"),
            ("LIVE_RUNTIME_CHECK_FAILED", "0" * 64, "LIVE_RUNTIME_SOURCE_MISMATCH"),
            ("LIVE_SCHEMA_CHECK_FAILED", "SCHEMA_HEAD_OK=0004_videos", "LIVE_SCHEMA_VERSION_MISMATCH"),
            ("LIVE_PLATFORM_CATALOGUE_CHECK_FAILED", "wrong", "LIVE_PLATFORM_CATALOGUE_MISMATCH"),
            ("LIVE_CONTENT_SNAPSHOT_CHECK_FAILED", "CONTENT_SNAPSHOT_OK=" + "a" * 64 + ";ITEMS=5001",
             "LIVE_CONTENT_SNAPSHOT_INVALID"),
        )
        for key, value, code in bad:
            with self.subTest(key=key), mock.patch.dict(self.responses, {key: value}):
                with self.assertRaisesRegex(p.ReleaseError, code):
                    self.check()

    def test_wrong_container_public_mounts_or_port_refuses(self):
        for change, code in (
            ({"running": False}, "LIVE_CONTAINER_IDENTITY_MISMATCH"),
            ({"ports": {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8092"}]}}, "LIVE_API_PORT_MISMATCH"),
            ({"mounts": []}, "LIVE_PUBLIC_MOUNT_MISMATCH"),
        ):
            with self.subTest(change=change), mock.patch.dict(self.inspection, change):
                with self.assertRaisesRegex(p.ReleaseError, code):
                    self.check()

    def test_new_feed_format_and_discovery_contract_is_strict(self):
        wrong = (
            [],
            [{"name": "format", "in": "query", "required": False, "schema": {
                "type": "string", "default": "all", "enum": ["all", "video"]}}],
            [{"name": "format", "in": "query", "required": False, "schema": {
                "type": "string", "default": "all", "enum": ["all", "video", "article"]}},
             {"name": "discover", "in": "query", "required": False, "schema": {
                "type": "boolean", "default": 0}}],
        )
        for parameters in wrong:
            with self.subTest(parameters=parameters):
                self.document["paths"]["/content/feed"]["get"]["parameters"] = parameters
                with self.assertRaisesRegex(p.ReleaseError, "UNIFIED_FEED_FILTER_CONTRACT_MISMATCH"):
                    self.check()

    def test_legacy_playback_and_protected_routes_remain_guarded(self):
        self.document["paths"]["/videos/feed"]["get"]["parameters"][0]["schema"]["default"] = "platform-v1"
        with self.assertRaisesRegex(p.ReleaseError, "PLATFORM_PLAYBACK_CONTRACT_MISMATCH"):
            self.check()
        self.document = public_contract()
        self.api.require_unauthorized.side_effect = p.ReleaseError("CONTENT_AUTH_GUARD_MISMATCH")
        with self.assertRaisesRegex(p.ReleaseError, "CONTENT_AUTH_GUARD_MISMATCH"):
            self.check()

    def test_blog_flag_and_worker_runtime_tools_must_be_ready(self):
        for key, value, code in (
            ("LIVE_BLOG_SETTINGS_CHECK_FAILED", "disabled", "LIVE_BLOGS_NOT_ENABLED"),
            ("LIVE_WORKER_RUNTIME_CHECK_FAILED", "f" * 64, "LIVE_WORKER_RUNTIME_SOURCE_MISMATCH"),
            ("LIVE_WORKER_TOOLS_CHECK_FAILED", "missing", "LIVE_WORKER_TOOLS_OR_FLAG_MISMATCH"),
            ("LIVE_WORKER_DATABASE_CHECK_FAILED", "WORKER_SCHEMA_HEAD_OK=0005_content", "LIVE_WORKER_DATABASE_SCHEMA_MISMATCH"),
        ):
            with self.subTest(key=key), mock.patch.dict(self.responses, {key: value}):
                with self.assertRaisesRegex(p.ReleaseError, code):
                    self.check()

    def test_worker_restart_oom_identity_and_journal_image_are_checked_live(self):
        for key, value, code in (
            ("running", False, "LIVE_WORKER_NOT_READY"),
            ("restart_count", 1, "LIVE_WORKER_NOT_READY"),
            ("oom_killed", True, "LIVE_WORKER_NOT_READY"),
            ("command", ["sleep", "infinity"], "LIVE_WORKER_NOT_READY"),
            ("image", "sha256:" + "f" * 64, "LIVE_WORKER_IMAGE_MISMATCH"),
            ("mounts", [], "LIVE_WORKER_MEDIA_MOUNT_MISMATCH"),
        ):
            with self.subTest(key=key), mock.patch.dict(self.worker, {key: value}):
                with self.assertRaisesRegex(p.ReleaseError, code):
                    self.check()
        with mock.patch.dict(self.inspection, {"image": "sha256:" + "f" * 64}):
            with self.assertRaisesRegex(p.ReleaseError, "LIVE_API_IMAGE_MISMATCH"):
                self.check()

    def test_old_clients_never_opt_in_to_hosted_video_implicitly(self):
        for route, method in (("/content/feed", "get"), ("/content/{content_id}", "get"),
                              ("/content/{content_id}/state", "patch")):
            for default in (True, 0, None):
                self.document = public_contract()
                hosted = next(item for item in self.document["paths"][route][method]["parameters"]
                              if item["name"] == "hosted_video")
                hosted["schema"]["default"] = default
                with self.subTest(route=route, default=default), self.assertRaisesRegex(p.ReleaseError, "OLD_CLIENT_HOSTED_VIDEO_GUARD"):
                    self.check()
        self.document = public_contract()
        del self.document["paths"]["/blogs/{blog_id}/media"]
        with self.assertRaisesRegex(p.ReleaseError, "BLOGS_API_CONTRACT_MISMATCH"):
            self.check()


class HttpClientMockTests(unittest.TestCase):
    class Response(io.BytesIO):
        status = 200

        def __init__(self, payload, **headers):
            super().__init__(payload)
            self.headers = headers

    def client_with(self, response):
        client = p.ApiClient(p.API_BASE)
        client.opener = mock.Mock()
        client.opener.open.return_value = response
        return client

    def test_stream_is_hash_bound_and_bounded_without_network(self):
        payload = b"PK\x03\x04test apk" * 10
        expected = p.FileHash(len(payload), hashlib.sha256(payload).hexdigest())
        client = self.client_with(self.Response(payload, **{"Content-Length": str(len(payload))}))
        self.assertEqual(client.apk_hash(expected), expected)
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, p.API_BASE + "/download/vecher.apk?sha256=" + expected.sha256)
        client = self.client_with(self.Response(payload, **{"Content-Length": str(len(payload) + 1)}))
        with self.assertRaisesRegex(p.ReleaseError, "API_APK_LENGTH_MISMATCH"):
            client.apk_hash(expected)

    def test_manifest_cache_policy_size_and_unauthenticated_status_are_checked(self):
        client = self.client_with(self.Response(b"{}"))
        with self.assertRaisesRegex(p.ReleaseError, "API_MANIFEST_CACHE_POLICY_MISMATCH"):
            client.get_json("/app/version")
        client = self.client_with(self.Response(b"1234", **{"Cache-Control": "private, no-store"}))
        with self.assertRaisesRegex(p.ReleaseError, "API_JSON_TOO_LARGE"):
            client.get_json("/app/version", limit=3)
        client = self.client_with(self.Response(b"{}"))
        with self.assertRaisesRegex(p.ReleaseError, "CONTENT_AUTH_GUARD_MISMATCH"):
            client.require_unauthorized("/content/feed")
        for status in (401, 403):
            client = self.client_with(self.Response(b"{}"))
            client.opener.open.side_effect = HTTPError(p.API_BASE, status, "", {}, None)
            if status == 401:
                client.require_unauthorized("/content/feed")
            else:
                with self.assertRaisesRegex(p.ReleaseError, "CONTENT_AUTH_GUARD_MISMATCH"):
                    client.require_unauthorized("/content/feed")

    def test_proxy_is_disabled_and_redirects_are_never_followed(self):
        with mock.patch.object(p.urllib.request, "build_opener") as build:
            p.ApiClient(p.API_BASE)
        proxy, redirects = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(redirects, p.NoRedirect)
        self.assertIsNone(redirects.redirect_request(None, None, 302, "", {}, "https://foreign.invalid"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

