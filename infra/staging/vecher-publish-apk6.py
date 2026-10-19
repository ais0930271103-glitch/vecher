#!/usr/bin/env python3
"""Publish the already verified Vecher 0.2.1 (6) APK on its staging server.

Only --stage is configurable at the command line. This does not build, sign,
restart services, edit configuration, or delete uploaded files/backups.

The .release.lock serializes cooperating file publishers. The legacy API upload
endpoint does not take this lock and must not be run concurrently. Identity
checks refuse an observed foreign release before replacements and rollback;
they do not make two files one atomic transaction against an uncooperative
writer. A hard kill/power loss between renames leaves an identifiable partial
state; a rerun stops for review instead of guessing which release to restore.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import time
import urllib.request
from urllib.parse import urlsplit
import uuid


@dataclass(frozen=True)
class Pin:
    code: int
    name: str
    size: int
    sha256: str


OLD = Pin(5, "0.2.0", 34233600,
          "8b2f6157c094fbd4955f90a741836a237c5fcc41caabfeb5502a8f2b0c768124")
NEW = Pin(6, "0.2.1", 34299136,
          "73c3f84dd7c16a4d463206957ab9dfa8eb7919237a8ace1ced39b4d1d3f11f75")
MAX_JSON = 65536
CHUNK = 1024 * 1024
FIELDS = {"version_code", "version_name", "notes", "required"}


@dataclass(frozen=True)
class Layout:
    downloads: Path = Path("/opt/vecher-test/downloads")
    backup: Path = Path("/root/backups/vecher-test/app-before-0.2.1-6-73c3f84d")
    api: str = "http://127.0.0.1:8092"


class ReleaseError(RuntimeError):
    """A safe machine-readable error; never includes HTTP bodies or secrets."""


def fail(code: str) -> None:
    raise ReleaseError(code)


def real_directory(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        fail("INVALID_ABSOLUTE_DIRECTORY")
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            fail("DIRECTORY_MISSING")
        if not stat.S_ISDIR(info.st_mode):
            fail("DIRECTORY_SYMLINK_OR_NOT_DIRECTORY")


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make_private_parents(path: Path) -> None:
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            os.mkdir(part, 0o700)
            sync_dir(part.parent)
            info = part.lstat()
        if not stat.S_ISDIR(info.st_mode):
            fail("BACKUP_PARENT_SYMLINK_OR_NOT_DIRECTORY")


@contextmanager
def regular_reader(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            fail("FILE_NOT_REGULAR_OR_HAS_HARDLINKS")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream, info
    finally:
        os.close(fd)


def stable_file(before, after) -> bool:
    return (before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns) == (
                after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns)


@dataclass(frozen=True)
class FileHash:
    size: int
    sha256: str


def file_hash(path: Path, limit: int = 200 * CHUNK) -> FileHash:
    digest = hashlib.sha256()
    count = 0
    with regular_reader(path) as (stream, before):
        if before.st_size > limit:
            fail("FILE_TOO_LARGE")
        while data := stream.read(CHUNK):
            count += len(data)
            if count > limit:
                fail("FILE_TOO_LARGE")
            digest.update(data)
        if not stable_file(before, os.fstat(stream.fileno())):
            fail("FILE_CHANGED_DURING_READ")
    if count != before.st_size:
        fail("FILE_CHANGED_DURING_READ")
    return FileHash(count, digest.hexdigest())


def read_bytes(path: Path, limit: int = MAX_JSON) -> bytes:
    with regular_reader(path) as (stream, before):
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            fail("JSON_TOO_LARGE")
        if len(raw) != before.st_size or not stable_file(
                before, os.fstat(stream.fileno())):
            fail("FILE_CHANGED_DURING_READ")
    return raw


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def decode_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, ValueError):
        fail("INVALID_JSON")


def metadata(raw: bytes, pin: Pin, *, api: bool = False) -> dict:
    value = decode_json(raw)
    if not isinstance(value, dict):
        fail("METADATA_NOT_OBJECT")
    if (set(value) != FIELDS and not api) or not FIELDS.issubset(value):
        fail("METADATA_FIELDS_MISMATCH")
    if (type(value["version_code"]) is not int or
            value["version_code"] != pin.code or
            type(value["version_name"]) is not str or
            value["version_name"] != pin.name or
            value["required"] is not False or
            type(value["notes"]) is not str or
            not value["notes"].strip()):
        fail("METADATA_VERSION_OR_TYPES_MISMATCH")
    if api:
        if (type(value.get("url")) is not str or
                urlsplit(value["url"]).path != "/download/vecher.apk"):
            fail("API_DOWNLOAD_URL_MISMATCH")
    return {key: value[key] for key in FIELDS}


@dataclass(frozen=True)
class Pair:
    apk: FileHash
    raw_json: bytes

    @property
    def json_hash(self) -> FileHash:
        return FileHash(len(self.raw_json), hashlib.sha256(self.raw_json).hexdigest())


def pair_at(directory: Path, apk_name: str = "vecher.apk",
            json_name: str = "version.json") -> Pair:
    return Pair(file_hash(directory / apk_name), read_bytes(directory / json_name))


def validate_pair(pair: Pair, pin: Pin) -> dict:
    value = metadata(pair.raw_json, pin)
    if pair.apk != FileHash(pin.size, pin.sha256):
        fail("APK_HASH_OR_SIZE_MISMATCH")
    return value


def durable_copy(source: Path, destination: Path, expected: FileHash, mode: int) -> None:
    digest = hashlib.sha256()
    count = 0
    with regular_reader(source) as (src, before):
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, mode)
        try:
            with os.fdopen(fd, "wb", closefd=False) as out:
                while data := src.read(CHUNK):
                    count += len(data)
                    if count > expected.size:
                        fail("COPY_SOURCE_CHANGED")
                    digest.update(data)
                    out.write(data)
                out.flush()
                os.fchmod(fd, mode)
                os.fsync(fd)
        finally:
            os.close(fd)
        if not stable_file(before, os.fstat(src.fileno())):
            fail("COPY_SOURCE_CHANGED")
    if FileHash(count, digest.hexdigest()) != expected or file_hash(
            destination) != expected:
        fail("COPY_HASH_MISMATCH")


def durable_bytes(path: Path, raw: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as out:
            out.write(raw)
            out.flush()
            os.fchmod(fd, mode)
            os.fsync(fd)
    finally:
        os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiClient:
    def __init__(self, base: str):
        self.base = base
        # Loopback must never inherit a user's HTTP proxy configuration.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())

    def get_json(self, endpoint: str) -> bytes:
        request = urllib.request.Request(self.base + endpoint,
                                         headers={"Cache-Control": "no-cache"})
        with self.opener.open(request, timeout=10) as response:
            if response.status != 200:
                fail("API_HTTP_STATUS")
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                fail("API_JSON_TOO_LARGE")
            return raw

    def apk_hash(self, expected: FileHash) -> FileHash:
        digest = hashlib.sha256()
        count = 0
        deadline = time.monotonic() + 45
        request = urllib.request.Request(self.base + "/download/vecher.apk",
                                         headers={"Cache-Control": "no-cache"})
        with self.opener.open(request, timeout=10) as response:
            if response.status != 200:
                fail("API_HTTP_STATUS")
            length = response.headers.get("Content-Length")
            if length is not None and length != str(expected.size):
                fail("API_APK_LENGTH_MISMATCH")
            while data := response.read(CHUNK):
                count += len(data)
                if count > expected.size or time.monotonic() > deadline:
                    fail("API_APK_LENGTH_OR_TIME_LIMIT")
                digest.update(data)
        return FileHash(count, digest.hexdigest())


class Publisher:
    def __init__(self, layout: Layout = Layout(), *, old: Pin = OLD,
                 new: Pin = NEW, api=None, output=print, sleep=time.sleep):
        self.layout = layout
        self.old, self.new = old, new
        self.api = api if api is not None else ApiClient(layout.api)
        self.output, self.sleep = output, sleep

    def verify_api(self, expected: Pair, pin: Pin) -> None:
        expected_info = validate_pair(expected, pin)
        last = None
        for attempt in range(3):
            try:
                health = decode_json(self.api.get_json("/health"))
                if (not isinstance(health, dict) or health.get("ok") is not True or
                        health.get("env") != "staging"):
                    fail("API_NOT_HEALTHY_STAGING")
                info = metadata(self.api.get_json("/app/version"), pin, api=True)
                if info != expected_info:
                    fail("API_METADATA_MISMATCH")
                if self.api.apk_hash(expected.apk) != expected.apk:
                    fail("API_APK_HASH_MISMATCH")
                return
            except Exception as exc:
                last = exc
                if attempt < 2:
                    self.sleep(1)
        if isinstance(last, ReleaseError):
            raise last
        fail("API_UNREACHABLE_OR_INVALID_RESPONSE")

    def assert_current(self, expected: Pair) -> None:
        if pair_at(self.layout.downloads) != expected:
            fail("CURRENT_RELEASE_CHANGED")

    def backup_manifest(self, old_pair: Pair, candidate: Pair) -> dict:
        return {
            "format": 1,
            "source_dir": str(self.layout.downloads),
            "old": {"version_code": self.old.code, "version_name": self.old.name,
                    "apk_bytes": old_pair.apk.size, "apk_sha256": old_pair.apk.sha256,
                    "json_bytes": old_pair.json_hash.size,
                    "json_sha256": old_pair.json_hash.sha256},
            "new": {"version_code": self.new.code, "version_name": self.new.name,
                    "apk_bytes": candidate.apk.size, "apk_sha256": candidate.apk.sha256,
                    "json_bytes": candidate.json_hash.size,
                    "json_sha256": candidate.json_hash.sha256},
            "files": {"apk": "vecher.apk", "metadata": "version.json"},
        }

    def verify_backup(self, old_pair: Pair, candidate: Pair) -> None:
        backup = self.layout.backup
        real_directory(backup)
        info = backup.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            fail("BACKUP_NOT_PRIVATE_OR_NOT_OWNED")
        for name in ("vecher.apk", "version.json", "manifest.json"):
            info = (backup / name).lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                    stat.S_IMODE(info.st_mode) != 0o600):
                fail("BACKUP_FILE_NOT_PRIVATE_OR_NOT_OWNED")
        if pair_at(backup) != old_pair:
            fail("BACKUP_CONTENT_MISMATCH")
        actual = decode_json(read_bytes(backup / "manifest.json"))
        if not isinstance(actual, dict) or not isinstance(actual.pop("created_utc", None), str):
            fail("BACKUP_MANIFEST_INVALID")
        if actual != self.backup_manifest(old_pair, candidate):
            fail("BACKUP_MANIFEST_MISMATCH")

    def prepare_backup(self, old_pair: Pair, candidate: Pair) -> None:
        backup = self.layout.backup
        make_private_parents(backup.parent)
        try:
            backup.lstat()
        except FileNotFoundError:
            pending = backup.parent / ("." + backup.name + ".preparing-" + uuid.uuid4().hex)
            pending.mkdir(mode=0o700)
            os.chmod(pending, 0o700)
            durable_copy(self.layout.downloads / "vecher.apk", pending / "vecher.apk",
                         old_pair.apk, 0o600)
            durable_copy(self.layout.downloads / "version.json", pending / "version.json",
                         old_pair.json_hash, 0o600)
            manifest = self.backup_manifest(old_pair, candidate)
            manifest["created_utc"] = datetime.now(timezone.utc).isoformat()
            durable_bytes(pending / "manifest.json",
                          (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
            sync_dir(pending)
            # Cooperating releases hold the same lock; an unexpected backup is never replaced.
            if backup.exists() or backup.is_symlink():
                fail("BACKUP_APPEARED_DURING_PREPARATION")
            os.rename(pending, backup)
            sync_dir(backup.parent)
        self.verify_backup(old_pair, candidate)
        self.output("BACKUP_OK=" + str(backup))

    def rollback(self, stage: Path, old_pair: Pair, candidate: Pair) -> None:
        mixed = Pair(candidate.apk, old_pair.raw_json)
        current = pair_at(self.layout.downloads)
        if current == old_pair:
            self.output("ROLLBACK_NOT_NEEDED")
            return
        if current not in (mixed, candidate):
            fail("ROLLBACK_BLOCKED_CURRENT_RELEASE_CHANGED")
        self.verify_backup(old_pair, candidate)
        tag = uuid.uuid4().hex
        ready_apk = stage / (".rollback-" + tag + ".apk")
        ready_json = stage / (".rollback-" + tag + ".json")
        durable_copy(self.layout.backup / "vecher.apk", ready_apk, old_pair.apk, 0o644)
        durable_copy(self.layout.backup / "version.json", ready_json, old_pair.json_hash, 0o644)
        sync_dir(stage)
        self.assert_current(current)
        # Stop advertising the failed new release before restoring the old APK.
        os.replace(ready_json, self.layout.downloads / "version.json")
        sync_dir(self.layout.downloads)
        self.assert_current(mixed)
        os.replace(ready_apk, self.layout.downloads / "vecher.apk")
        sync_dir(self.layout.downloads)
        self.assert_current(old_pair)
        self.output(f"FILES_ROLLED_BACK={self.old.name} ({self.old.code})")
        self.verify_api(old_pair, self.old)
        self.assert_current(old_pair)
        self.output("ROLLBACK_API_OK")

    def publish_locked(self, stage: Path) -> None:
        candidate = pair_at(stage, "vecher-0.2.1-6.apk", "vecher-0.2.1-6.version.json")
        validate_pair(candidate, self.new)
        self.output("UPLOAD_HASH_OK")
        current = pair_at(self.layout.downloads)
        if current == candidate:
            self.verify_api(candidate, self.new)
            self.assert_current(candidate)
            self.output(f"FILES_ALREADY_PUBLISHED={self.new.name} ({self.new.code})")
            self.success(candidate)
            return
        observed = decode_json(current.raw_json)
        if isinstance(observed, dict) and type(observed.get("version_code")) is int:
            if observed["version_code"] > self.new.code:
                fail("NEWER_RELEASE_ALREADY_PUBLISHED")
            if observed["version_code"] == self.new.code:
                fail("SAME_VERSION_DIFFERENT_CONTENT")
            if observed["version_code"] == self.old.code and current.apk == candidate.apk:
                fail("PARTIAL_RELEASE_REQUIRES_REVIEW")
        validate_pair(current, self.old)
        self.verify_api(current, self.old)
        self.assert_current(current)
        self.prepare_backup(current, candidate)
        tag = uuid.uuid4().hex
        ready_apk = stage / (".ready-" + tag + ".apk")
        ready_json = stage / (".ready-" + tag + ".json")
        durable_copy(stage / "vecher-0.2.1-6.apk", ready_apk, candidate.apk, 0o644)
        durable_copy(stage / "vecher-0.2.1-6.version.json", ready_json, candidate.json_hash, 0o644)
        sync_dir(stage)
        self.assert_current(current)
        try:
            os.replace(ready_apk, self.layout.downloads / "vecher.apk")
            sync_dir(self.layout.downloads)
            self.assert_current(Pair(candidate.apk, current.raw_json))
            os.replace(ready_json, self.layout.downloads / "version.json")
            sync_dir(self.layout.downloads)
            self.assert_current(candidate)
            self.output(f"FILES_PUBLISHED={self.new.name} ({self.new.code})")
            self.verify_api(candidate, self.new)
            self.assert_current(candidate)
        except (Exception, KeyboardInterrupt) as exc:
            reason = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
            self.output("PUBLISH_FAILED=" + reason)
            try:
                self.rollback(stage, current, candidate)
            except (Exception, KeyboardInterrupt) as rollback_error:
                reason = (str(rollback_error) if isinstance(rollback_error, ReleaseError)
                          else type(rollback_error).__name__)
                self.output("ROLLBACK_FAILED_OR_BLOCKED=" + reason)
                fail("PUBLISH_FAILED_ROLLBACK_REQUIRES_REVIEW")
            fail("PUBLISH_FAILED_OLD_RELEASE_RESTORED")
        self.success(candidate)

    def success(self, candidate: Pair) -> None:
        self.output(f"API_VERSION_OK={self.new.name} ({self.new.code})")
        self.output("API_APK_SHA256=" + candidate.apk.sha256)
        self.output("VECHER_UPDATE_PUBLISHED_OK")

    def run(self, stage: Path) -> None:
        if (not stage.is_absolute() or ".." in stage.parts or
                stage.parent != self.layout.downloads or
                not re.fullmatch(r"\.release-6-[0-9a-f]{32}", stage.name)):
            fail("INVALID_STAGE_PATH")
        real_directory(self.layout.downloads)
        real_directory(stage)
        info = stage.lstat()
        if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or
                info.st_dev != self.layout.downloads.stat().st_dev):
            fail("STAGE_NOT_PRIVATE_OWNED_OR_SAME_FILESYSTEM")
        lock = self.layout.downloads / ".release.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid != os.geteuid()):
                fail("RELEASE_LOCK_NOT_SAFE")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail("ANOTHER_RELEASE_IS_RUNNING")
            self.publish_locked(stage)
        finally:
            os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            fail("ROOT_REQUIRED")
        if socket.gethostname() not in {"elegant-red", "elegant-red.ptr.network"}:
            fail("WRONG_SERVER_HOSTNAME")
        os.umask(0o077)

        def interrupted(_number, _frame):
            fail("INTERRUPTED")

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        Publisher().run(args.stage)
        return 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        print("VECHER_PUBLISH_STOPPED=" + reason, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
