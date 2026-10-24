#!/usr/bin/env python3
"""Publish the already verified Vecher 0.9.0 (14) APK on its staging server.

The stage and mandatory verified APK hash/size are supplied at the command line.
Version 14 and its predecessor remain fixed. This does not build, sign,
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
import subprocess
import sys
import time
import urllib.request
from urllib.error import HTTPError
import uuid


@dataclass(frozen=True)
class Pin:
    code: int
    name: str
    size: int
    sha256: str


OLD = Pin(13, "0.8.0", 36591005,
          "a87d14f11e874f81509e4c6964f1c7355103a61e24d4e279ff61e454102bc1bc")
NEW_CODE = 14
NEW_NAME = "0.9.0"
OLD_METADATA_SHA256 = "9b1e1ad5c86f865e0bc3498874c28b62b44805092d357d1b4fa82dcc0e4e660e"
CERTIFICATE_SHA256 = "bf3d541231287a7e37629088f470d82079d6b1577d0692cceeb8597779d520c1"
MAX_JSON = 65536
MAX_OPENAPI = 1024 * 1024
CHUNK = 1024 * 1024
MIN_APK = 2
MAX_APK = 200 * CHUNK
BASE_FIELDS = {"version_code", "version_name", "notes", "required"}
INTEGRITY_FIELDS = {"sha256", "bytes"}
FIELDS = BASE_FIELDS | INTEGRITY_FIELDS
SOURCE_SHA = "40e43d85a2e434d4ee2090884a872a507f60fc93"
PREVIOUS_SOURCE_SHA = "713b6b34819a6df382da03d5213135986d7c0c0c"
SOURCE_TREE = "eb38addc70e020c698e7663b8f3fd0ce39def646"
SCHEMA_OK = "SCHEMA_HEAD_OK=0006_public_blogs"
API_VERSION = "0.9.0"
API_BASE = "http://127.0.0.1:8092"
CATALOGUE_OK = "VIDEO_CATALOGUE_OK=24;REVISION=18513eb1e9ccb698"
PLATFORM_CATALOGUE_OK = "PLATFORM_VIDEO_CATALOGUE_OK=7;REVISION=606eea15797ae24b"
RUNTIME_SHA = "520fb35ae85b4778f584fd4023d048fb69ad7b9afffcaba3a6d750b9fb1b0618"
CONTENT_FILE = "/srv/public-content/publications.json"
JOURNAL_ROOT = Path("/root/backups/vecher-test")
ACCEPTANCE_VERSION = "apk14-device-v1"
PHONE_CHECKS = (
    "upgrade_preserves_accounts", "personal_video_sound_and_seek",
    "couple_exact_revision_consent", "draft_hidden_from_reader",
    "reviewed_post_reader_and_follow", "interrupted_upload_retry",
    "picker_background_pin_call", "withdrawal_closes_new_requests",
    "old_apk13_compatibility",
)
SOURCE_RECEIPT_FIELDS = {
    "format", "source_sha", "previous_source_sha", "version_code", "version_name",
    "builder_sha256", "builder_apk_sha256", "apk_sha256", "apk_bytes",
    "metadata_sha256", "signing_certificate_sha256", "publisher_sha256", "created_utc",
}
PHONE_RECEIPT_FIELDS = {
    "format", "acceptance_version", "source_sha", "version_code", "version_name",
    "apk_sha256", "apk_bytes", "metadata_sha256", "source_receipt_sha256",
    "signing_certificate_sha256", "publisher_sha256", "backend_source_sha",
    "backend_runtime_sha256", "backend_schema", "backend_api_version",
    "operator_label", "devices", "checks", "accepted_utc",
}
LEGACY_ROUTES = (
    ("/subscription", "get"),
    ("/plans/{card_id}", "get"),
    ("/plans/{card_id}/open", "post"),
    ("/plans/{card_id}/vote", "post"),
    ("/plans/{card_id}/schedule", "post"),
    ("/chat", "get"),
    ("/chat", "post"),
    ("/app/version", "get"),
    ("/download/vecher.apk", "get"),
)
EVENINGS_ROUTES = (
    ("/plans/{card_id}/start", "post"),
    ("/evenings/current", "get"),
    ("/evenings/history", "get"),
    ("/evenings/{evening_id}", "get"),
    ("/evenings/{evening_id}/step", "post"),
    ("/evenings/{evening_id}/pause", "post"),
    ("/evenings/{evening_id}/resume", "post"),
    ("/evenings/{evening_id}/complete", "post"),
    ("/evenings/{evening_id}/cancel", "post"),
    ("/evenings/{evening_id}/feedback", "post"),
)
VIDEOS_ROUTES = (
    ("/videos/settings", "get"),
    ("/videos/settings/personal", "put"),
    ("/videos/settings/shared", "put"),
    ("/videos/feed", "get"),
    ("/videos/shared", "get"),
    ("/videos/{video_id}", "get"),
    ("/videos/{video_id}/state", "patch"),
    ("/videos/{video_id}/viewed", "post"),
)
CONTENT_ROUTES = (
    ("/content/settings", "get"),
    ("/content/settings/personal", "put"),
    ("/content/settings/shared", "put"),
    ("/content/feed", "get"),
    ("/content/authors", "get"),
    ("/content/authors/{author_id}", "get"),
    ("/content/authors/{author_id}/state", "patch"),
    ("/content/{content_id}", "get"),
    ("/content/{content_id}/state", "patch"),
)
BLOG_ROUTES = (
    ("/blogs/mine", "get"), ("/blogs", "post"),
    ("/blogs/{blog_id}", "get"), ("/blogs/{blog_id}", "patch"),
    ("/blogs/{blog_id}/consent", "post"), ("/blogs/{blog_id}/withdraw", "post"),
    ("/blogs/{blog_id}/posts", "get"), ("/blogs/{blog_id}/posts", "post"),
    ("/blogs/{blog_id}/posts/{post_id}", "get"),
    ("/blogs/{blog_id}/posts/{post_id}", "patch"),
    ("/blogs/{blog_id}/posts/{post_id}/submit", "post"),
    ("/blogs/{blog_id}/posts/{post_id}/withdraw", "post"),
    ("/blogs/{blog_id}/media", "post"),
    ("/blogs/media/{asset_id}", "get"), ("/blogs/media/{asset_id}", "delete"),
    ("/blogs/media/{asset_id}/video", "get"),
    ("/blogs/media/{asset_id}/thumbnail", "get"),
    ("/content/{content_id}/report", "post"),
)
AUTH_PROBES = (
    "/content/settings", "/content/feed", "/content/authors",
    "/content/feed?format=video", "/content/feed?format=article&discover=true",
    "/videos/settings", "/videos/feed", "/videos/shared",
    "/videos/settings?playback=platform-v1",
    "/videos/feed?playback=platform-v1",
    "/videos/shared?playback=platform-v1",
    "/content/feed?hosted_video=true", "/blogs/mine",
)

# Hash the running container's source files using the same file set and
# canonical manifest as the reviewed release13 recovery verifier, recomputed
# for the complete approved release14 backend tree (80 source files). No settings,
# database rows, private files or remote URLs are read or printed here.
RUNTIME_PROBE = '''import hashlib, json, stat
from pathlib import Path
root = Path("/srv")
entries = [root / "requirements.txt", root / "alembic.ini"]
for folder in ("app", "alembic"):
    base = root / folder
    assert stat.S_ISDIR(base.lstat().st_mode)
    for path in base.rglob("*"):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if stat.S_ISDIR(path.lstat().st_mode):
            continue
        entries.append(path)
manifest = {}
for path in entries:
    before = path.lstat()
    assert stat.S_ISREG(before.st_mode) and before.st_nlink == 1
    raw = path.read_bytes()
    after = path.lstat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    manifest[str(path.relative_to(root))] = hashlib.sha256(raw).hexdigest()
print(hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
'''

# Validate the currently selected, editable public snapshot and linked videos,
# rather than pinning the seed revision or requiring its original item count.
CONTENT_PROBE = '''import os
from pathlib import Path
from app.config import get_settings
from app.publish_content import validate_source
s = get_settings()
assert s.env == "staging" and s.downloads_dir == "/srv/downloads"
assert os.environ.get("VECHER_CONTENT_CATALOGUE") == "/srv/public-content/publications.json"
c = validate_source(Path(os.environ["VECHER_CONTENT_CATALOGUE"]))
from app.routers.content import _snapshot, _available
snapshot = _snapshot()
items = _available(snapshot)
keys = [("video", item.video_id) if item.video_id else ("item", item.id) for item in items]
assert len(keys) == len(set(keys))
print("CONTENT_SNAPSHOT_OK=" + c.revision + ";ITEMS=" + str(len(c.items)))
'''

INSPECT_FORMAT = ('{"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}},'
                  '"ports":{{json .NetworkSettings.Ports}},'
                  '"running":{{json .State.Running}},"image":{{json .Image}}}')
WORKER_INSPECT_FORMAT = ('{"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}},'
                         '"running":{{json .State.Running}},"image":{{json .Image}},'
                         '"restarting":{{json .State.Restarting}},'
                         '"oom_killed":{{json .State.OOMKilled}},'
                         '"restart_count":{{json .RestartCount}},'
                         '"command":{{json .Config.Cmd}},'
                         '"read_only":{{json .HostConfig.ReadonlyRootfs}}}')
BLOG_SETTINGS_PROBE = '''from app.config import get_settings
s = get_settings()
assert s.env == "staging" and s.blogs_enabled is True
assert s.blog_media_dir == "/srv/blog-media"
print("BLOGS_ENABLED_AND_MEDIA_PATH_OK")
'''
WORKER_TOOLS_PROBE = '''import os, secrets, shutil, subprocess
assert not os.environ.get("VECHER_DATA_KEYS") and not os.environ.get("VECHER_JWT_SECRET")
os.environ["VECHER_JWT_SECRET"] = secrets.token_urlsafe(48)
from app.config import get_settings
s = get_settings()
assert s.env == "staging" and s.blogs_enabled is True
assert s.blog_media_dir == "/srv/blog-media"
for tool in (s.blog_media_ffmpeg, s.blog_media_ffprobe):
    assert shutil.which(tool)
    subprocess.run([tool, "-version"], check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("MEDIA_WORKER_TOOLS_AND_FLAG_OK")
'''
WORKER_DATABASE_PROBE = '''import asyncio, os, secrets
assert not os.environ.get("VECHER_DATA_KEYS") and not os.environ.get("VECHER_JWT_SECRET")
os.environ["VECHER_JWT_SECRET"] = secrets.token_urlsafe(48)
from app.config import get_settings
from app.db import engine
from app.migrate import check
async def main():
    assert get_settings().blogs_enabled is True
    try:
        head = await asyncio.wait_for(check(engine), timeout=10)
        assert head == "0006_public_blogs"
        print("WORKER_SCHEMA_HEAD_OK=" + head)
    finally:
        await engine.dispose()
asyncio.run(main())
'''


@dataclass(frozen=True)
class Layout:
    downloads: Path
    backup: Path
    api: str = API_BASE
    repo: Path = Path("/opt/vecher-test/repo")
    content: Path = Path("/opt/vecher-test/content")
    media: Path = Path("/opt/vecher-test/blog-media")
    journal_root: Path = JOURNAL_ROOT


class ReleaseError(RuntimeError):
    """A safe machine-readable error; never includes HTTP bodies or secrets."""


def fail(code: str) -> None:
    raise ReleaseError(code)


def safe_output(value: str, *, file=None) -> None:
    # A closed SSH output pipe must not interrupt a local rollback or leave a
    # completed publication unverified. An interrupted client can rerun safely.
    try:
        print(value, file=file, flush=True)
    except BrokenPipeError:
        pass


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


def file_hash(path: Path, limit: int = MAX_APK) -> FileHash:
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


def private_json(path: Path) -> dict:
    with regular_reader(path) as (stream, info):
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            fail("SERVER_JOURNAL_NOT_PRIVATE_OR_OWNED")
        raw = stream.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON or not stable_file(info, os.fstat(stream.fileno())):
            fail("SERVER_JOURNAL_UNSTABLE_OR_TOO_LARGE")
    value = decode_json(raw)
    if type(value) is not dict:
        fail("SERVER_JOURNAL_NOT_OBJECT")
    return value


def check_server_journal(layout: Layout) -> dict:
    real_directory(layout.journal_root)
    pointer = private_json(layout.journal_root / "release14-current.json")
    if (set(pointer) != {"schema", "release", "checkpoint"} or
            type(pointer["schema"]) is not int or pointer["schema"] != 1 or
            type(pointer["release"]) is not int or pointer["release"] != 14 or
            type(pointer["checkpoint"]) is not str):
        fail("SERVER_JOURNAL_POINTER_INVALID")
    checkpoint = Path(pointer["checkpoint"])
    if (checkpoint.parent != layout.journal_root or
            re.fullmatch(r"release-14-[0-9a-f]{32}", checkpoint.name) is None):
        fail("SERVER_JOURNAL_CHECKPOINT_INVALID")
    real_directory(checkpoint)
    info = checkpoint.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        fail("SERVER_JOURNAL_CHECKPOINT_NOT_PRIVATE")
    journal = private_json(checkpoint / "journal.json")
    if (type(journal.get("schema")) is not int or journal["schema"] != 1 or
            type(journal.get("release")) is not int or journal["release"] != 14 or
            journal.get("phase") != "blogs_ready" or
            journal.get("source_sha") != SOURCE_SHA or
            journal.get("previous_source_sha") != PREVIOUS_SOURCE_SHA or
            journal.get("source_tree") != SOURCE_TREE or
            journal.get("runtime_sha256") != RUNTIME_SHA or
            any(journal.get(key) is not True for key in (
                "rehearsal_ok", "rollback_rehearsal_ok", "media_rehearsal_ok", "traffic_may_have_opened"))):
        fail("SERVER_JOURNAL_NOT_READY_FOR_APK14")
    for key in ("api_image_id", "worker_image_id"):
        if type(journal.get(key)) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", journal[key]) is None:
            fail("SERVER_JOURNAL_IMAGE_INVALID")
    return journal


def valid_utc(value) -> bool:
    if type(value) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{7} UTC", value) is None:
        return False
    try:
        datetime.strptime(value[:26], "%Y-%m-%d %H:%M:%S.%f")
        return True
    except ValueError:
        return False


def is_sha256(value) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def verify_release_declarations(stage: Path, candidate: Pair, *, metadata_sha256: str,
                                source_receipt_sha256: str, phone_receipt_sha256: str) -> None:
    """Require operator-recorded device acceptance for these exact uploaded bytes.

    These local receipts describe provenance and human observations; they are
    not a claim that a script or a mocked test has performed physical testing.
    """
    if not all(is_sha256(value) for value in (metadata_sha256, source_receipt_sha256, phone_receipt_sha256)):
        fail("RELEASE_DECLARATION_PINS_REQUIRED")
    if candidate.json_hash.sha256 != metadata_sha256:
        fail("CANDIDATE_METADATA_HASH_MISMATCH")
    source_raw = read_bytes(stage / "vecher-0.9.0-14.source.json")
    phone_raw = read_bytes(stage / "vecher-0.9.0-14.phone.json")
    if hashlib.sha256(source_raw).hexdigest() != source_receipt_sha256:
        fail("SOURCE_RECEIPT_HASH_MISMATCH")
    if hashlib.sha256(phone_raw).hexdigest() != phone_receipt_sha256:
        fail("PHONE_RECEIPT_HASH_MISMATCH")
    source, phone = decode_json(source_raw), decode_json(phone_raw)
    if type(source) is not dict or set(source) != SOURCE_RECEIPT_FIELDS:
        fail("SOURCE_RECEIPT_FIELDS_MISMATCH")
    if type(phone) is not dict or set(phone) != PHONE_RECEIPT_FIELDS:
        fail("PHONE_RECEIPT_FIELDS_MISMATCH")
    publisher_sha = file_hash(Path(__file__).absolute(), MAX_OPENAPI).sha256
    common = {
        "format": 1, "source_sha": SOURCE_SHA, "version_code": NEW_CODE,
        "version_name": NEW_NAME, "apk_sha256": candidate.apk.sha256,
        "apk_bytes": candidate.apk.size, "metadata_sha256": metadata_sha256,
        "signing_certificate_sha256": CERTIFICATE_SHA256, "publisher_sha256": publisher_sha,
    }
    for receipt in (source, phone):
        if any(type(receipt.get(key)) is not type(value) or receipt.get(key) != value
               for key, value in common.items()):
            fail("RELEASE_RECEIPT_IDENTITY_MISMATCH")
    if (type(source["previous_source_sha"]) is not str or
            re.fullmatch(r"[0-9a-f]{40}", source["previous_source_sha"]) is None or
            not is_sha256(source["builder_sha256"]) or
            source["builder_apk_sha256"] != candidate.apk.sha256 or
            not valid_utc(source["created_utc"])):
        fail("SOURCE_RECEIPT_PROVENANCE_INVALID")
    if (phone["acceptance_version"] != ACCEPTANCE_VERSION or
            phone["source_receipt_sha256"] != source_receipt_sha256 or
            phone["backend_source_sha"] != SOURCE_SHA or
            phone["backend_runtime_sha256"] != RUNTIME_SHA or
            phone["backend_schema"] != "0006_public_blogs" or
            phone["backend_api_version"] != API_VERSION or
            not valid_utc(phone["accepted_utc"]) or phone["accepted_utc"] < source["created_utc"]):
        fail("PHONE_RECEIPT_CONTEXT_INVALID")
    if (type(phone["checks"]) is not dict or set(phone["checks"]) != set(PHONE_CHECKS) or
            any(value is not True for value in phone["checks"].values())):
        fail("PHYSICAL_DEVICE_ACCEPTANCE_INCOMPLETE")
    if (type(phone["devices"]) is not dict or set(phone["devices"]) != {"partner_a", "partner_b", "reader"} or
            any(type(value) is not str or not value.strip() or len(value) > 120
                for value in phone["devices"].values()) or
            type(phone["operator_label"]) is not str or not phone["operator_label"].strip() or
            len(phone["operator_label"]) > 120):
        fail("PHONE_RECEIPT_DEVICE_LABELS_INVALID")


def metadata(raw: bytes, pin: Pin, *, api: bool = False) -> dict:
    value = decode_json(raw)
    if not isinstance(value, dict):
        fail("METADATA_NOT_OBJECT")
    # Both the pinned APK13 and the candidate APK14 require hash-bound,
    # six-field files. API responses also provide their fixed download URLs.
    required = FIELDS | {"url", "download_path"} if api else FIELDS
    if (not api and set(value) != required) or not required.issubset(value):
        fail("METADATA_FIELDS_MISMATCH")
    if (type(value["version_code"]) is not int or
            value["version_code"] != pin.code or
            type(value["version_name"]) is not str or
            value["version_name"] != pin.name or
            value["required"] is not False or
            type(value["notes"]) is not str or
            not value["notes"].strip() or len(value["notes"].encode("utf-8")) > 4096):
        fail("METADATA_VERSION_OR_TYPES_MISMATCH")
    if (type(value["bytes"]) is not int or value["bytes"] != pin.size or
            type(value["sha256"]) is not str or value["sha256"] != pin.sha256):
        fail("METADATA_APK_INTEGRITY_MISMATCH")
    if api:
        if (type(value["url"]) is not str or
                value["url"] != API_BASE + "/download/vecher.apk" or
                value["download_path"] != "/download/vecher.apk?sha256=" + pin.sha256):
            fail("API_DOWNLOAD_URL_MISMATCH")
    return {**{key: value[key] for key in BASE_FIELDS}, "sha256": pin.sha256, "bytes": pin.size}


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

    def get_json(self, endpoint: str, *, limit: int = MAX_JSON) -> bytes:
        request = urllib.request.Request(self.base + endpoint,
                                         headers={"Cache-Control": "no-cache"})
        with self.opener.open(request, timeout=10) as response:
            if response.status != 200:
                fail("API_HTTP_STATUS")
            if endpoint == "/app/version" and "no-store" not in {
                    part.strip().lower() for part in response.headers.get("Cache-Control", "").split(",")}:
                fail("API_MANIFEST_CACHE_POLICY_MISMATCH")
            raw = response.read(limit + 1)
            if len(raw) > limit:
                fail("API_JSON_TOO_LARGE")
            return raw

    def require_unauthorized(self, endpoint: str) -> None:
        request = urllib.request.Request(self.base + endpoint,
                                         headers={"Cache-Control": "no-cache"})
        try:
            with self.opener.open(request, timeout=10):
                fail("CONTENT_AUTH_GUARD_MISMATCH")
        except HTTPError as error:
            try:
                if error.code != 401:
                    fail("CONTENT_AUTH_GUARD_MISMATCH")
            finally:
                error.close()

    def apk_hash(self, expected: FileHash) -> FileHash:
        digest = hashlib.sha256()
        count = 0
        deadline = time.monotonic() + 45
        request = urllib.request.Request(self.base + "/download/vecher.apk?sha256=" + expected.sha256,
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


def command_output(command: list[str], code: str, *, timeout: float = 45) -> str:
    # No shell, no service changes, and never echo captured stderr/configuration.
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        fail(code)
    if result.returncode != 0:
        fail(code)
    return result.stdout.strip()


def check_backend(layout: Layout, api) -> dict:
    """Read-only compatibility gate for this release, before any replacement."""
    journal = check_server_journal(layout)
    real_directory(layout.repo)
    real_directory(layout.content)
    real_directory(layout.media)
    head = command_output(["git", "-C", str(layout.repo), "rev-parse", "HEAD"],
                          "SERVER_SOURCE_CHECK_FAILED")
    if head != SOURCE_SHA:
        fail("SERVER_SOURCE_MISMATCH")
    if command_output(["git", "-C", str(layout.repo), "status", "--porcelain"],
                      "SERVER_WORKTREE_CHECK_FAILED"):
        fail("SERVER_WORKTREE_CHANGED")
    inspect = decode_json(command_output([
        "docker", "container", "inspect", "vecher-test-api-1", "--format", INSPECT_FORMAT],
        "LIVE_CONTAINER_CHECK_FAILED").encode())
    try:
        labels = inspect["labels"]
        if (inspect["running"] is not True or
                labels.get("com.docker.compose.project") != "vecher-test" or
                labels.get("com.docker.compose.service") != "api"):
            fail("LIVE_CONTAINER_IDENTITY_MISMATCH")
        if inspect.get("image") != journal["api_image_id"]:
            fail("LIVE_API_IMAGE_MISMATCH")
        ports = inspect["ports"].get("8000/tcp")
        if ports != [{"HostIp": "127.0.0.1", "HostPort": "8092"}]:
            fail("LIVE_API_PORT_MISMATCH")
        mounts = inspect["mounts"]
        for source, target, writable in (
                (layout.downloads, "/srv/downloads", True),
                (layout.content, "/srv/public-content", False),
                (layout.media, "/srv/blog-media", True)):
            matches = [mount for mount in mounts if mount.get("Destination") == target]
            if (len(matches) != 1 or matches[0].get("Type") != "bind" or
                    matches[0].get("Source") != str(source) or matches[0].get("RW") is not writable):
                fail("LIVE_PUBLIC_MOUNT_MISMATCH")
            if any(mount.get("Destination", "").startswith(target + "/") for mount in mounts):
                fail("LIVE_PUBLIC_MOUNT_SHADOWED")
    except (KeyError, TypeError, AttributeError):
        fail("LIVE_CONTAINER_CONTRACT_INVALID")
    runtime = command_output(["docker", "exec", "vecher-test-api-1", "python", "-c", RUNTIME_PROBE],
                             "LIVE_RUNTIME_CHECK_FAILED")
    if runtime != RUNTIME_SHA:
        fail("LIVE_RUNTIME_SOURCE_MISMATCH")
    schema = command_output(["docker", "exec", "vecher-test-api-1", "python",
                             "-m", "app.migrate", "check"],
                            "LIVE_SCHEMA_CHECK_FAILED")
    if schema != SCHEMA_OK:
        fail("LIVE_SCHEMA_VERSION_MISMATCH")
    settings = command_output(["docker", "exec", "vecher-test-api-1", "python", "-c", BLOG_SETTINGS_PROBE],
                              "LIVE_BLOG_SETTINGS_CHECK_FAILED")
    if settings != "BLOGS_ENABLED_AND_MEDIA_PATH_OK":
        fail("LIVE_BLOGS_NOT_ENABLED")
    worker = decode_json(command_output([
        "docker", "container", "inspect", "vecher-test-public-video-worker-1", "--format", WORKER_INSPECT_FORMAT],
        "LIVE_WORKER_CHECK_FAILED").encode())
    try:
        if (worker["running"] is not True or worker["restarting"] is not False or
                worker["oom_killed"] is not False or type(worker["restart_count"]) is not int or
                worker["restart_count"] != 0 or worker["read_only"] is not True or
                worker["command"] != ["python", "-m", "app.blog_media_worker"] or
                worker["labels"].get("com.docker.compose.project") != "vecher-test" or
                worker["labels"].get("com.docker.compose.service") != "public-video-worker"):
            fail("LIVE_WORKER_NOT_READY")
        if worker["image"] != journal["worker_image_id"]:
            fail("LIVE_WORKER_IMAGE_MISMATCH")
        media_mounts = [mount for mount in worker["mounts"] if mount.get("Destination") == "/srv/blog-media"]
        if (len(media_mounts) != 1 or media_mounts[0].get("Type") != "bind" or
                media_mounts[0].get("Source") != str(layout.media) or media_mounts[0].get("RW") is not True or
                any(mount.get("Destination", "").startswith(("/srv/blog-media/", "/srv/uploads", "/run/vecher"))
                    for mount in worker["mounts"])):
            fail("LIVE_WORKER_MEDIA_MOUNT_MISMATCH")
    except (KeyError, TypeError, AttributeError):
        fail("LIVE_WORKER_CONTRACT_INVALID")
    worker_runtime = command_output([
        "docker", "exec", "vecher-test-public-video-worker-1", "python", "-c", RUNTIME_PROBE],
        "LIVE_WORKER_RUNTIME_CHECK_FAILED")
    if worker_runtime != RUNTIME_SHA:
        fail("LIVE_WORKER_RUNTIME_SOURCE_MISMATCH")
    worker_tools = command_output([
        "docker", "exec", "vecher-test-public-video-worker-1", "python", "-c", WORKER_TOOLS_PROBE],
        "LIVE_WORKER_TOOLS_CHECK_FAILED")
    if worker_tools != "MEDIA_WORKER_TOOLS_AND_FLAG_OK":
        fail("LIVE_WORKER_TOOLS_OR_FLAG_MISMATCH")
    worker_schema = command_output([
        "docker", "exec", "vecher-test-public-video-worker-1", "python", "-c", WORKER_DATABASE_PROBE],
        "LIVE_WORKER_DATABASE_CHECK_FAILED", timeout=20)
    if worker_schema != "WORKER_SCHEMA_HEAD_OK=0006_public_blogs":
        fail("LIVE_WORKER_DATABASE_SCHEMA_MISMATCH")
    # This reads and validates the running image's catalogue, without network
    # requests, user data, authentication tokens or database mutations.
    catalogue = command_output(["docker", "exec", "vecher-test-api-1", "python",
                                "-m", "app.video_catalogue"],
                               "LIVE_VIDEO_CATALOGUE_CHECK_FAILED")
    if catalogue != CATALOGUE_OK:
        fail("LIVE_VIDEO_CATALOGUE_MISMATCH")
    platform = command_output(["docker", "exec", "vecher-test-api-1", "python",
                               "-m", "app.video_catalogue", "--platform"],
                              "LIVE_PLATFORM_CATALOGUE_CHECK_FAILED")
    if platform != PLATFORM_CATALOGUE_OK:
        fail("LIVE_PLATFORM_CATALOGUE_MISMATCH")
    content = command_output(["docker", "exec", "vecher-test-api-1", "python", "-c", CONTENT_PROBE],
                             "LIVE_CONTENT_SNAPSHOT_CHECK_FAILED")
    match = re.fullmatch(r"CONTENT_SNAPSHOT_OK=([0-9a-f]{64});ITEMS=(\d+)", content)
    if match is None or not 0 <= int(match[2]) <= 5000:
        fail("LIVE_CONTENT_SNAPSHOT_INVALID")
    try:
        health = decode_json(api.get_json("/health"))
        if (type(health) is not dict or health.get("ok") is not True or
                health.get("env") != "staging" or health.get("version") != API_VERSION):
            fail("BLOGS_API_NOT_HEALTHY")
        document = decode_json(api.get_json("/openapi.json", limit=MAX_OPENAPI))
    except ReleaseError:
        raise
    except Exception:
        fail("FREE_CORE_API_UNREACHABLE")
    try:
        for name in ("PlanOut", "SubscriptionOut"):
            properties = document["components"]["schemas"][name]["properties"]
            prop = properties["core_plans_unlimited"]
            if prop.get("default") is not True:
                fail("FREE_CORE_API_CONTRACT_MISMATCH")
            legacy = properties["free_plans_left"].get("default")
            if type(legacy) is not int or legacy != 1:
                fail("LEGACY_FREE_PLAN_CONTRACT_MISMATCH")
        paths = document["paths"]
        if not isinstance(paths, dict):
            fail("FREE_CORE_API_CONTRACT_MISMATCH")
        if "/subscription/dev-activate" in paths:
            fail("LEGACY_ACTIVATION_STILL_PRESENT")
        if document.get("info", {}).get("version") != API_VERSION:
            fail("UNIFIED_CONTENT_API_VERSION_MISMATCH")
        for path, method in LEGACY_ROUTES:
            if not isinstance(paths.get(path), dict) or not isinstance(
                    paths[path].get(method), dict):
                fail("LEGACY_API_CONTRACT_MISMATCH")
        for path, method in EVENINGS_ROUTES:
            if not isinstance(paths.get(path), dict) or not isinstance(
                    paths[path].get(method), dict):
                fail("EVENINGS_API_CONTRACT_MISMATCH")
        for path, method in VIDEOS_ROUTES:
            if not isinstance(paths.get(path), dict) or not isinstance(
                    paths[path].get(method), dict):
                fail("VIDEOS_API_CONTRACT_MISMATCH")
            parameters = paths[path][method].get("parameters", [])
            if not isinstance(parameters, list):
                fail("PLATFORM_PLAYBACK_CONTRACT_MISMATCH")
            playback = [parameter for parameter in parameters
                        if isinstance(parameter, dict) and parameter.get("name") == "playback"]
            if len(playback) != 1:
                fail("PLATFORM_PLAYBACK_CONTRACT_MISMATCH")
            parameter = playback[0]
            schema = parameter.get("schema", {})
            if (parameter.get("in") != "query" or parameter.get("required") is not False or
                    not isinstance(schema, dict) or schema.get("type") != "string" or
                    schema.get("default") != "legacy" or
                    schema.get("enum") != ["legacy", "platform-v1"]):
                fail("PLATFORM_PLAYBACK_CONTRACT_MISMATCH")
        for path, method in CONTENT_ROUTES:
            if not isinstance(paths.get(path), dict) or not isinstance(
                    paths[path].get(method), dict):
                fail("CONTENT_API_CONTRACT_MISMATCH")
        for path, method in BLOG_ROUTES:
            if not isinstance(paths.get(path), dict) or not isinstance(paths[path].get(method), dict):
                fail("BLOGS_API_CONTRACT_MISMATCH")
        parameters = paths["/content/feed"]["get"].get("parameters", [])
        if not isinstance(parameters, list):
            fail("UNIFIED_FEED_FILTER_CONTRACT_MISMATCH")
        for name, kind, default, choices in (
                ("format", "string", "all", ["all", "video", "article"]),
                ("discover", "boolean", False, None)):
            matches = [parameter for parameter in parameters
                       if isinstance(parameter, dict) and parameter.get("name") == name]
            if len(matches) != 1:
                fail("UNIFIED_FEED_FILTER_CONTRACT_MISMATCH")
            parameter = matches[0]
            schema = parameter.get("schema", {})
            if (parameter.get("in") != "query" or parameter.get("required") is not False or
                    not isinstance(schema, dict) or schema.get("type") != kind or
                    type(schema.get("default")) is not type(default) or
                    schema.get("default") != default or
                    (choices is not None and schema.get("enum") != choices)):
                fail("UNIFIED_FEED_FILTER_CONTRACT_MISMATCH")
        for path, method in (("/content/feed", "get"), ("/content/{content_id}", "get"),
                             ("/content/{content_id}/state", "patch")):
            hosted = [parameter for parameter in paths[path][method].get("parameters", [])
                      if isinstance(parameter, dict) and parameter.get("name") == "hosted_video"]
            if (len(hosted) != 1 or hosted[0].get("in") != "query" or
                    hosted[0].get("required") is not False or
                    hosted[0].get("schema", {}).get("type") != "boolean" or
                    hosted[0].get("schema", {}).get("default") is not False):
                fail("OLD_CLIENT_HOSTED_VIDEO_GUARD_MISMATCH")
        version = document["components"]["schemas"]["VersionOut"]
        for name, kind in (("sha256", "string"), ("bytes", "integer"), ("download_path", "string")):
            if (version["properties"].get(name, {}).get("type") != kind or
                    name not in version.get("required", [])):
                fail("UPDATE_INTEGRITY_CONTRACT_MISMATCH")
    except (KeyError, TypeError, AttributeError):
        fail("FREE_CORE_API_CONTRACT_MISMATCH")
    # Read-only anonymous probes: no accounts/tokens or preference writes.
    for endpoint in AUTH_PROBES:
        try:
            api.require_unauthorized(endpoint)
        except ReleaseError:
            raise
        except Exception:
            fail("CONTENT_AUTH_PROBE_UNREACHABLE")
    return journal


class Publisher:
    def __init__(self, layout: Layout | None = None, *, old: Pin = OLD,
                 new: Pin, api=None, output=safe_output, sleep=time.sleep,
                 backend_check=None, declaration_check=None,
                 old_metadata_sha256=OLD_METADATA_SHA256,
                 metadata_sha256=None, source_receipt_sha256=None, phone_receipt_sha256=None):
        self.layout = layout if layout is not None else Layout(
            downloads=Path("/opt/vecher-test/downloads"),
            backup=Path("/root/backups/vecher-test") /
            ("app-before-0.9.0-14-" + new.sha256))
        self.old, self.new = old, new
        self.api = api if api is not None else ApiClient(self.layout.api)
        self.output, self.sleep = output, sleep
        self.old_metadata_sha256 = old_metadata_sha256
        self.backend_check = (backend_check if backend_check is not None
                              else lambda: check_backend(self.layout, self.api))
        self.declaration_check = (declaration_check if declaration_check is not None else
                                  lambda stage, candidate: verify_release_declarations(
                                      stage, candidate, metadata_sha256=metadata_sha256,
                                      source_receipt_sha256=source_receipt_sha256,
                                      phone_receipt_sha256=phone_receipt_sha256))

    def verify_api(self, expected: Pair, pin: Pin) -> None:
        expected_info = validate_pair(expected, pin)
        last = None
        for attempt in range(3):
            try:
                health = decode_json(self.api.get_json("/health"))
                if (not isinstance(health, dict) or health.get("ok") is not True or
                        health.get("env") != "staging"):
                    fail("API_NOT_HEALTHY_STAGING")
                if health.get("version") != API_VERSION:
                    fail("UNIFIED_CONTENT_API_VERSION_MISMATCH")
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
        mixed = Pair(old_pair.apk, candidate.raw_json)
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
        # Keep the new hash-bound manifest while restoring the old APK. Until
        # the original hash-bound manifest is restored, /app/version returns 503;
        # it must never advertise old metadata with the new APK's bytes.
        os.replace(ready_apk, self.layout.downloads / "vecher.apk")
        sync_dir(self.layout.downloads)
        self.assert_current(mixed)
        os.replace(ready_json, self.layout.downloads / "version.json")
        sync_dir(self.layout.downloads)
        self.assert_current(old_pair)
        self.output(f"FILES_ROLLED_BACK={self.old.name} ({self.old.code})")
        self.verify_api(old_pair, self.old)
        self.assert_current(old_pair)
        self.output("ROLLBACK_API_OK")

    def publish_locked(self, stage: Path) -> None:
        candidate = pair_at(stage, "vecher-0.9.0-14.apk", "vecher-0.9.0-14.version.json")
        validate_pair(candidate, self.new)
        self.declaration_check(stage, candidate)
        self.output("UPLOAD_HASH_OK")
        self.output("PHONE_ACCEPTANCE_RECORD_OK")
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
                if (current.raw_json == candidate.raw_json and
                        current.apk == FileHash(self.old.size, self.old.sha256)):
                    fail("PARTIAL_RELEASE_REQUIRES_REVIEW")
                fail("SAME_VERSION_DIFFERENT_CONTENT")
            if observed["version_code"] == self.old.code and current.apk == candidate.apk:
                fail("PARTIAL_RELEASE_REQUIRES_REVIEW")
        validate_pair(current, self.old)
        if current.json_hash.sha256 != self.old_metadata_sha256:
            fail("PREVIOUS_METADATA_HASH_MISMATCH")
        self.verify_api(current, self.old)
        self.assert_current(current)
        self.prepare_backup(current, candidate)
        tag = uuid.uuid4().hex
        ready_apk = stage / (".ready-" + tag + ".apk")
        ready_json = stage / (".ready-" + tag + ".json")
        durable_copy(stage / "vecher-0.9.0-14.apk", ready_apk, candidate.apk, 0o644)
        durable_copy(stage / "vecher-0.9.0-14.version.json", ready_json, candidate.json_hash, 0o644)
        sync_dir(stage)
        self.assert_current(current)
        try:
            # Keep the reviewed manifest-first protocol. Each hash-bound manifest
            # makes the API return 503 until its matching APK rename completes.
            os.replace(ready_json, self.layout.downloads / "version.json")
            sync_dir(self.layout.downloads)
            self.assert_current(Pair(current.apk, candidate.raw_json))
            os.replace(ready_apk, self.layout.downloads / "vecher.apk")
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
                not re.fullmatch(r"\.release-14-[0-9a-f]{32}", stage.name)):
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
            self.backend_check()
            self.output("UNIFIED_CONTENT_BACKEND_OK")
            self.output("PUBLIC_BLOGS_BACKEND_OK")
            self.publish_locked(stage)
        finally:
            os.close(fd)


def expected_sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("Use exactly 64 lowercase hexadecimal SHA-256 characters")
    return value


def expected_size(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]{0,8}", value) is None:
        raise argparse.ArgumentTypeError("Use an integer APK size in bytes")
    size = int(value)
    if not MIN_APK <= size <= MAX_APK:
        raise argparse.ArgumentTypeError("APK size must be between 2 and 209715200 bytes")
    return size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-backend", action="store_true", help="Read-only backend/device-acceptance prerequisite check")
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--expected-sha256", type=expected_sha256)
    parser.add_argument("--expected-size", type=expected_size)
    parser.add_argument("--expected-metadata-sha256", type=expected_sha256)
    parser.add_argument("--expected-source-receipt-sha256", type=expected_sha256)
    parser.add_argument("--expected-phone-receipt-sha256", type=expected_sha256)
    args = parser.parse_args()
    publication_args = (args.stage, args.expected_sha256, args.expected_size,
                        args.expected_metadata_sha256, args.expected_source_receipt_sha256,
                        args.expected_phone_receipt_sha256)
    if args.check_backend:
        if any(value is not None for value in publication_args):
            parser.error("--check-backend cannot be combined with publication arguments")
    elif any(value is None for value in publication_args):
        parser.error("Publication requires --stage and all five --expected-* integrity arguments")
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
        signal.signal(signal.SIGHUP, interrupted)
        if args.check_backend:
            layout = Layout(Path("/opt/vecher-test/downloads"), JOURNAL_ROOT)
            check_backend(layout, ApiClient(layout.api))
            safe_output("BLOGS_BACKEND_READY=0.9.0 (14)")
            safe_output("BACKEND_SOURCE_SHA=" + SOURCE_SHA)
            safe_output("BACKEND_RUNTIME_SHA256=" + RUNTIME_SHA)
            safe_output("BACKEND_SCHEMA=0006_public_blogs")
            safe_output("BACKEND_API_VERSION=" + API_VERSION)
            return 0
        candidate = Pin(NEW_CODE, NEW_NAME, args.expected_size, args.expected_sha256)
        Publisher(new=candidate, metadata_sha256=args.expected_metadata_sha256,
                  source_receipt_sha256=args.expected_source_receipt_sha256,
                  phone_receipt_sha256=args.expected_phone_receipt_sha256).run(args.stage)
        return 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        safe_output("VECHER_PUBLISH_STOPPED=" + reason, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
