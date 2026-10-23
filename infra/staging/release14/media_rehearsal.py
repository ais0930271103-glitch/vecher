"""Release 14's destructive-to-synthetic-data-only media acceptance exercise.

Run inside the isolated candidate API container after migrating a NEW database:
  python /srv/release14_media_rehearsal.py --base-url http://127.0.0.1:8000 \
    --fixture /srv/blog-media/rehearsal-fixture.mp4 \
    --expected-database vecher_release14_smoke_012345abcdef

The orchestrator must start a separate real worker with no DATA_KEYS/JWT/SMTP,
no live mounts, and no external network; this helper cannot attest its container
environment. It refuses ordinary database names, nonempty schemas, external HTTP,
and enabled delivery services before seeding three synthetic users. It never uses
OTP, sends mail, sets a publication state in SQL, or migrates a live database.
The synthetic database and media directory are discarded by the orchestrator.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit

SCHEMA_HEAD = "0006_public_blogs"
VERSION = "0.9.0"
DATABASE_NAME = re.compile(r"vecher_release14_smoke_[0-9a-f]{12}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FIXTURE_NAME = "rehearsal-fixture.mp4"
SOURCE_MARKER = b"source-metadata-must-be-removed"
MAX_FIXTURE_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_OPERATOR_BYTES = 64 * 1024


class RehearsalStopped(Exception):
    """A constant diagnostic code, never database URLs, tokens or response text."""

    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z0-9_]{1,100}", code):
            code = "invalid_diagnostic"
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RehearsalStopped(code)


def validate_environment(environ: Mapping[str, str], expected_database: str, base_url: str) -> None:
    """Validate before importing app.db, connecting, or creating any rows."""
    require(environ.get("VECHER_RELEASE14_SYNTHETIC_REHEARSAL") == "1", "synthetic_guard_missing")
    require(bool(DATABASE_NAME.fullmatch(expected_database)), "database_name_not_synthetic")
    require(environ.get("VECHER_ENV") == "staging", "staging_required")
    require(environ.get("VECHER_BLOGS_ENABLED", "").lower() in {"1", "true"}, "blogs_must_be_enabled")
    for name in ("SCHEDULER_ENABLED", "OUTBOX_ENABLED", "REALTIME_ENABLED"):
        require(environ.get("VECHER_" + name, "").lower() in {"0", "false"}, "delivery_must_be_disabled")
    require(environ.get("VECHER_SMTP_HOST") == "", "smtp_must_be_disabled")
    require(not any(environ.get("VECHER_" + key) for key in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")),
            "smtp_credentials_not_allowed")
    require(bool(environ.get("VECHER_DATA_KEYS")), "synthetic_encryption_key_required")
    require(len(environ.get("VECHER_JWT_SECRET", "")) >= 32, "synthetic_jwt_required")
    firebase = environ.get("VECHER_FIREBASE_CREDENTIALS", "")
    require(bool(firebase) and Path(firebase).is_absolute()
            and Path(firebase).name != "firebase-key.json"
            and not os.path.lexists(firebase), "firebase_must_be_unconfigured")
    try:
        database = urlsplit(environ.get("VECHER_DATABASE_URL", ""))
        db_port = database.port
        base = urlsplit(base_url)
        base_port = base.port
    except ValueError:
        raise RehearsalStopped("invalid_connection_url") from None
    require(database.scheme == "postgresql+asyncpg" and bool(database.hostname)
            and db_port in (None, 5432) and not database.query and not database.fragment
            and unquote(database.path) == "/" + expected_database, "database_url_not_synthetic")
    require(base.scheme == "http" and base.hostname in {"127.0.0.1", "::1"}
            and base_port == 8000 and base.path in {"", "/"}
            and not base.username and not base.password and not base.query and not base.fragment,
            "loopback_api_required")


@dataclass(frozen=True)
class Fixture:
    data: bytes
    sha256: str


def read_fixture(path: Path, media_root: Path) -> Fixture:
    """Read exactly the isolated fixed fixture, without following symlinks."""
    root = Path(os.path.abspath(media_root))
    candidate = Path(os.path.abspath(path))
    require(root == root.resolve() and root.is_dir(), "unsafe_media_directory")
    require(candidate == root / FIXTURE_NAME, "fixture_path_not_isolated")
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and 1 <= info.st_size <= MAX_FIXTURE_BYTES, "fixture_size_or_type_invalid")
            data = bytearray()
            while chunk := os.read(descriptor, min(65536, MAX_FIXTURE_BYTES + 1 - len(data))):
                data.extend(chunk)
                require(len(data) <= MAX_FIXTURE_BYTES, "fixture_size_or_type_invalid")
            require(len(data) == info.st_size, "fixture_changed")
        finally:
            os.close(descriptor)
    except OSError:
        raise RehearsalStopped("fixture_unavailable") from None
    raw = bytes(data)
    require(SOURCE_MARKER in raw and b"ftyp" in raw[:64], "fixture_marker_missing")
    return Fixture(raw, hashlib.sha256(raw).hexdigest())


async def require_empty_database(engine, *, expected_database: str | None = None) -> None:
    """No application row may predate the exercise, including tokens/outbox."""
    from sqlalchemy import inspect, select, text
    from app import models  # noqa: F401 -- register complete metadata
    from app.db import Base

    async with engine.connect() as connection:
        if expected_database is not None:
            require(connection.dialect.name == "postgresql", "postgresql_required")
            require(await connection.scalar(text("SELECT current_database()")) == expected_database,
                    "connected_database_mismatch")

        def check_schema(sync):
            inspector = inspect(sync)
            tables = set(inspector.get_table_names())
            require(tables == set(Base.metadata.tables) | {"alembic_version"}, "schema_table_set_mismatch")
            require(not inspector.get_view_names(), "schema_views_not_allowed")
            if sync.dialect.name == "postgresql":
                require(set(inspector.get_schema_names()) <= {"public", "information_schema"},
                        "schema_namespace_not_isolated")

        await connection.run_sync(check_schema)
        heads = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
        require(heads == [SCHEMA_HEAD], "schema_head_mismatch")
        for table in Base.metadata.tables.values():
            require((await connection.execute(select(1).select_from(table).limit(1))).first() is None,
                    "database_not_empty")


def object_json(raw: bytes, code: str) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RehearsalStopped(code) from None
    require(isinstance(value, dict), code)
    return value


def identifier(value) -> str:
    try:
        require(isinstance(value, str) and str(uuid.UUID(value)) == value, "invalid_api_identifier")
    except (ValueError, AttributeError):
        raise RehearsalStopped("invalid_api_identifier") from None
    return value


class Api:
    def __init__(self, client):
        self.client = client
        self.requests = 0

    async def request(self, stage: str, method: str, path: str, *, expected: int = 200,
                      limit: int = MAX_JSON_BYTES, **kwargs):
        import httpx

        require(path.startswith("/") and not path.startswith("//"), "unsafe_api_path")
        self.requests += 1
        async with self.client.stream(method, path, **kwargs) as response:
            require(response.status_code == expected, f"{stage}_http_{response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                require(len(body) <= limit, "api_response_limit")
            return httpx.Response(response.status_code, headers=response.headers, content=bytes(body),
                                  request=response.request)

    async def json(self, stage: str, method: str, path: str, **kwargs) -> dict:
        response = await self.request(stage, method, path, **kwargs)
        return object_json(response.content, stage + "_json")


async def operator(*args: str) -> dict:
    """Run the real exact-revision review CLI; bound time and both output pipes."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.blog_review", *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)

    async def read(stream):
        result = bytearray()
        while chunk := await stream.read(4096):
            result.extend(chunk)
            require(len(result) <= MAX_OPERATOR_BYTES, "operator_output_limit")
        return bytes(result)

    tasks = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr)),
             asyncio.create_task(process.wait())]
    try:
        async with asyncio.timeout(30):
            stdout, _, code = await asyncio.gather(*tasks)
        require(code == 0, "operator_failed")
        return object_json(stdout, "operator_json_invalid")
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def private_headers(response) -> None:
    require("no-store" in response.headers.get("cache-control", "")
            and "private" in response.headers.get("cache-control", "")
            and "authorization" in response.headers.get("vary", "").lower(), "private_cache_headers_missing")


async def exercise(client, fixture: Fixture, *, ready_timeout: float = 90) -> dict:
    """Core used by the guarded CLI and by an isolated process integration test.

    The caller must first validate the environment and the empty migrated DB.
    There is intentionally no CLI bypass for test/SQLite or existing databases.
    """
    from sqlalchemy import text
    from app.config import get_settings
    from app.consent import CONSENT_VERSION
    from app.db import SessionLocal
    from app.models import BlogMediaAsset, Consent, User
    from app.security import create_token

    api = Api(client)
    health = await api.json("health", "GET", "/health")
    require(health == {"ok": True, "env": "staging", "version": VERSION}, "api_version_mismatch")
    users = [User(id=str(uuid.uuid4()), email=f"release14-{uuid.uuid4().hex}@synthetic.invalid",
                  display_name=f"Synthetic {role}") for role in ("author", "partner", "reader")]
    async with SessionLocal() as session:
        session.add_all(users)
        await session.flush()
        session.add_all(Consent(user_id=user.id, kind="personal", version=CONSENT_VERSION) for user in users)
        await session.commit()
    headers = [{"Authorization": "Bearer " + create_token(user.id)} for user in users]
    author, partner, reader = headers
    for user, auth in zip(users, headers):
        me = await api.json("identity", "GET", "/auth/me", headers=auth)
        require(me.get("id") == user.id, "api_database_identity_mismatch")

    invite = await api.json("pair_invite", "POST", "/pairs/invite", headers=author)
    joined = await api.json("pair_join", "POST", "/pairs/join", headers=partner,
                            json={"code": invite["invite_code"]})
    require(joined.get("status") == "active", "pair_not_active")
    name, bio = "Synthetic release 14 public profile", "Synthetic couple media acceptance fixture"
    blog = await api.json("create_blog", "POST", "/blogs", expected=201, headers=author, json={
        "client_id": str(uuid.uuid4()), "kind": "couple", "name": name, "bio": bio,
        "public_consent": True, "consent_version": "public-blog-v1"})
    blog_id = identifier(blog.get("id"))
    blog_path = f"/blogs/{blog_id}"
    require(blog.get("status") == "pending_consent" and blog.get("revision") == 1
            and blog.get("author_id") == "blog-" + blog_id, "profile_consent_not_separate")
    async with SessionLocal() as session:
        raw = (await session.execute(text("SELECT name,bio FROM public_blogs WHERE id=:id"),
                                     {"id": blog_id})).one()
        require(all(isinstance(value, str) and value.startswith("enc1:") for value in raw)
                and name not in raw[0] and bio not in raw[1], "profile_not_encrypted")

    upload_headers = {**author, "Content-Type": "video/mp4", "Content-Length": str(len(fixture.data)),
                      "Idempotency-Key": uuid.uuid4().hex}
    upload = await api.request("upload", "POST", blog_path + "/media", expected=202,
                               headers=upload_headers, content=fixture.data)
    private_headers(upload)
    uploaded = object_json(upload.content, "upload_json")
    media_id = identifier(uploaded.get("id"))
    media_path = f"/blogs/media/{media_id}"
    repeat = await api.json("upload_retry", "POST", blog_path + "/media", expected=202,
                            headers=upload_headers, content=fixture.data)
    require(repeat.get("id") == media_id, "upload_not_idempotent")
    deadline = asyncio.get_running_loop().time() + ready_timeout
    while True:
        status = await api.request("media_status", "GET", media_path, headers=author)
        private_headers(status)
        ready = object_json(status.content, "media_status_json")
        if ready.get("status") == "ready":
            break
        require(ready.get("status") in {"uploading", "queued", "processing"}, "worker_processing_failed")
        require(asyncio.get_running_loop().time() < deadline, "worker_ready_timeout")
        await asyncio.sleep(0.25)
    media = ready.get("media")
    require(isinstance(media, dict) and media.get("id") == media_id
            and media.get("url") == media_path + "/video"
            and media.get("thumbnail_url") == media_path + "/thumbnail"
            and media.get("width") == 64 and media.get("height") == 48, "normalized_media_contract")
    async with SessionLocal() as session:
        asset = await session.get(BlogMediaAsset, media_id)
        require(asset is not None and asset.status == "ready" and asset.attempts >= 1
                and asset.source_sha256 == fixture.sha256 and asset.source_bytes == len(fixture.data)
                and asset.last_error is None and asset.lease_token is None, "worker_result_not_verified")
        require(bool(SHA256.fullmatch(asset.sha256 or ""))
                and 1 <= asset.output_bytes <= MAX_FIXTURE_BYTES, "normalized_output_invalid")
        output_hash, output_bytes = asset.sha256, asset.output_bytes
        source_path = Path(get_settings().blog_media_dir) / (asset.source_key + ".source")
    # The worker commits ready before unlinking its source. Do not mistake that
    # short, intentional ordering window for a failed cleanup.
    cleanup_deadline = asyncio.get_running_loop().time() + 5
    while os.path.lexists(source_path) and asyncio.get_running_loop().time() < cleanup_deadline:
        await asyncio.sleep(0.05)
    require(not os.path.lexists(source_path), "source_not_removed")

    post = await api.json("create_post", "POST", blog_path + "/posts", expected=201, headers=author, json={
        "client_id": str(uuid.uuid4()), "title": "Как сохранить фотографии прогулки",
        "description": "Сохраним фотографии и резервную копию вместе. Синтетическая проверка выпуска.",
        "topics": ["digital"], "language": "ru", "media_id": media_id})
    post_id = identifier(post.get("id"))
    content_id = "post-" + post_id
    post_path = blog_path + "/posts/" + post_id
    content_path = "/content/" + content_id
    require(post.get("status") == "draft" and post.get("revision") == 1
            and post.get("content_id") == content_id, "post_not_draft")
    for path in (content_path + "?hosted_video=true", media_path + "/video", media_path + "/thumbnail"):
        await api.request("draft_private", "GET", path, headers=reader, expected=404)
    submit = {"revision": 1, "public_consent": True, "consent_version": "public-post-v1"}
    await api.request("profile_approval_required", "POST", post_path + "/submit", headers=author,
                      json=submit, expected=409)
    agreed = await api.json("partner_profile_consent", "POST", blog_path + "/consent", headers=partner,
                            json={"revision": 1, "accepted": True, "consent_version": "public-blog-v1"})
    require(agreed.get("status") == "active" and agreed.get("revision") == 1, "partner_profile_consent_failed")
    first = await api.json("author_submit", "POST", post_path + "/submit", headers=author, json=submit)
    require(first.get("status") == "pending_partner", "partner_post_consent_not_required")
    second = await api.json("partner_submit", "POST", post_path + "/submit", headers=partner, json=submit)
    require(second.get("status") == "pending_review", "operator_review_not_required")
    await api.request("pending_private", "GET", media_path + "/video", headers=reader, expected=404)

    shown = await operator("show", post_id)
    require(shown.get("id") == post_id and shown.get("revision") == 1
            and shown.get("status") == "pending_review" and shown.get("sha256") == output_hash
            and shown.get("owner_approvals") == 2
            and shown.get("author", {}).get("id") == "blog-" + blog_id
            and shown.get("author", {}).get("revision") == 1, "operator_pins_mismatch")
    approved = await operator("approve", post_id, "--revision", "1", "--blog-revision", "1",
                              "--sha256", output_hash)
    require(approved.get("status") == "published" and approved.get("id") == post_id
            and approved.get("revision") == 1 and approved.get("sha256") == output_hash,
            "operator_approval_not_confirmed")

    detail = await api.json("reader_detail", "GET", content_path + "?hosted_video=true", headers=reader)
    require(detail.get("id") == content_id and detail.get("playback_kind") == "hosted"
            and detail.get("video_id") is None
            and detail.get("media", {}).get("url") == media_path + "/video", "reader_hosted_contract")
    await api.request("old_client_detail", "GET", content_path, headers=reader, expected=404)
    old_feed = await api.json("old_client_feed", "GET", "/content/feed?discover=true", headers=reader)
    require(content_id not in {item["id"] for item in old_feed["items"]}, "old_client_exposed_hosted_video")
    full = await api.request("reader_video", "GET", media_path + "/video", headers=reader,
                            limit=MAX_FIXTURE_BYTES)
    private_headers(full)
    require(full.headers.get("content-type") == "video/mp4" and len(full.content) == output_bytes
            and hashlib.sha256(full.content).hexdigest() == output_hash
            and SOURCE_MARKER not in full.content, "reader_bytes_or_metadata_mismatch")
    partial = await api.request("reader_range", "GET", media_path + "/video", expected=206,
                               headers={**reader, "Range": "bytes=0-31"})
    private_headers(partial)
    require(partial.content == full.content[:32]
            and partial.headers.get("content-range") == f"bytes 0-31/{output_bytes}", "range_contract_mismatch")
    head = await api.request("reader_head", "HEAD", media_path + "/video", headers=reader)
    require(not head.content and head.headers.get("content-length") == str(output_bytes), "head_contract_mismatch")
    thumb = await api.request("reader_thumbnail", "GET", media_path + "/thumbnail", headers=reader)
    private_headers(thumb)
    require(thumb.headers.get("content-type") == "image/jpeg" and thumb.content.startswith(b"\xff\xd8"),
            "thumbnail_contract_mismatch")
    await api.request("anonymous_media", "GET", media_path + "/video", expected=401)

    await api.json("follow", "PATCH", "/content/authors/blog-" + blog_id + "/state", headers=reader,
                   json={"followed": True})
    feed_path = "/content/feed?mode=following&hosted_video=true&discover=false"
    followed = await api.json("following_feed", "GET", feed_path, headers=reader)
    require(content_id in {item["id"] for item in followed["items"]}, "followed_post_missing")
    for auth in (author, partner):
        settings = await api.json("private_follows", "GET", "/content/settings", headers=auth)
        require(settings.get("followed_author_ids") == [], "follow_state_leaked")

    withdrawn = await api.json("partner_withdraw", "POST", blog_path + "/withdraw", headers=partner,
                               json={"revision": 1})
    require(withdrawn.get("status") == "withdrawn", "withdraw_not_confirmed")
    for path in (content_path + "?hosted_video=true", media_path + "/video", media_path + "/thumbnail"):
        await api.request("withdrawn_private", "GET", path, headers=reader, expected=404)
    await api.request("withdrawn_range_private", "GET", media_path + "/video", expected=404,
                      headers={**reader, "Range": "bytes=0-31"})
    after = await api.json("withdrawn_feed", "GET", feed_path, headers=reader)
    require(content_id not in {item["id"] for item in after["items"]}, "withdrawn_post_still_in_feed")
    await api.request("owner_preview_preserved", "HEAD", media_path + "/video", headers=author)

    return {"format": 1, "version": VERSION, "schema": SCHEMA_HEAD, "synthetic_users": 3,
            "http_requests": api.requests, "profile_encrypted": True, "worker_completed": True,
            "upload_idempotent": True, "joint_consent_verified": True, "operator_exact_pins": True,
            "old_client_compatible": True, "reader_range_verified": True, "private_follows_verified": True,
            "withdrawal_verified": True, "source_sha256": fixture.sha256,
            "normalized_sha256": output_hash, "normalized_bytes": output_bytes}


async def run(args) -> dict:
    validate_environment(os.environ, args.expected_database, args.base_url)
    import httpx
    from app.config import get_settings
    from app.db import engine

    settings = get_settings()
    require(settings.database_url == os.environ["VECHER_DATABASE_URL"] and settings.env == "staging"
            and settings.blogs_enabled and not settings.scheduler_enabled and not settings.outbox_enabled
            and not settings.realtime_enabled and not settings.smtp_host, "loaded_settings_mismatch")
    fixture = read_fixture(Path(args.fixture), Path(settings.blog_media_dir))
    try:
        await require_empty_database(engine, expected_database=args.expected_database)
        async with httpx.AsyncClient(base_url=args.base_url, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(15, connect=5)) as client:
            async with asyncio.timeout(180):
                return await exercise(client, fixture)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--expected-database", required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except RehearsalStopped as error:
        print("RELEASE14_MEDIA_REHEARSAL_STOPPED=" + error.code, file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("RELEASE14_MEDIA_REHEARSAL_STOPPED=unexpected_failure", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    print("RELEASE14_MEDIA_REHEARSAL_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
