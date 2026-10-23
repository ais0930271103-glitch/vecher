"""Real normalization, immutable receipts, access control and durable media jobs."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import Text, delete, select, type_coerce, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Delete

from app import blog_media as media, blog_media_worker as worker, crypto
from app.config import get_settings
from app.db import SessionLocal
from app.models import BlogMediaAsset, PublicBlog, PublicPost, utcnow
from conftest import login


@pytest.fixture(autouse=True)
def public_media_directory(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "blogs_enabled", True)
    monkeypatch.setattr(settings, "blog_media_dir", str(tmp_path / "blog-media"))
    monkeypatch.setattr(settings, "blog_media_min_free_bytes", 0)
    yield
    crypto._keys.cache_clear()


@pytest.fixture(scope="module")
def real_video(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        if os.environ.get("VECHER_REQUIRE_MEDIA_TOOLS") == "1":
            pytest.fail("CI requires ffmpeg and ffprobe for actual media normalization")
        pytest.skip("ffmpeg/ffprobe not installed locally")
    root = tmp_path_factory.mktemp("public-media-fixture")
    path = root / "source.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1:r=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-threads", "1", "-c:a", "aac", "-shortest",
        "-metadata", "comment=PRIVATE_FORMAT_MARKER", "-metadata", "location=+55.75+037.61/",
        "-metadata:s:v:0", "handler_name=PRIVATE_VIDEO_MARKER",
        "-metadata:s:a:0", "handler_name=PRIVATE_AUDIO_MARKER", str(path)],
        check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return path.read_bytes()


async def author(client, email="author@example.ru"):
    headers = await login(client, email)
    result = await client.post("/blogs", headers=headers, json={
        "client_id": uuid.uuid4().hex, "kind": "individual", "name": "Public name",
        "bio": "Only explicitly public words", "public_consent": True,
        "consent_version": "public-blog-v1"})
    assert result.status_code == 201, result.text
    return headers, result.json()["id"]


async def upload(client, headers, blog_id, data=b"test", key=None, **extra_headers):
    return await client.post(f"/blogs/{blog_id}/media", content=data, headers={
        **headers, "Content-Type": "video/mp4", "Idempotency-Key": key or uuid.uuid4().hex,
        **extra_headers})


async def asset_row(asset_id):
    async with SessionLocal() as session:
        return await session.get(BlogMediaAsset, asset_id)


async def prepared(client, real_video):
    headers, blog_id = await author(client)
    response = await upload(client, headers, blog_id, real_video)
    assert response.status_code == 202, response.text
    asset_id = response.json()["id"]
    assert await worker.process_one()
    asset = await asset_row(asset_id)
    assert asset.status == "ready", asset.last_error
    return headers, blog_id, asset


async def test_upload_owner_before_body_idempotency_and_headers(client):
    headers, blog_id = await author(client)
    outsider = await login(client, "outsider@example.ru")
    consumed = False

    async def body():
        nonlocal consumed
        consumed = True
        yield b"test"

    response = await client.post(f"/blogs/{blog_id}/media", content=body(), headers={
        **outsider, "Content-Length": "4", "Content-Type": "video/mp4",
        "Idempotency-Key": uuid.uuid4().hex})
    assert response.status_code == 404 and not consumed
    key = uuid.uuid4().hex
    response = await upload(client, headers, blog_id, key=key)
    assert response.status_code == 202, response.text
    item = response.json()
    assert item["status"] == "queued" and item["error_code"] is None and item["media"] is None
    assert response.headers["cache-control"] == "private, no-store"
    assert "Authorization" in response.headers["vary"].split(", ")
    again = await upload(client, headers, blog_id, b"else", key=key)
    assert again.json()["id"] == item["id"]
    row = await asset_row(item["id"])
    assert row.source_sha256 == hashlib.sha256(b"test").hexdigest()
    assert Path(get_settings().blog_media_dir, row.source_key + ".source").read_bytes() == b"test"
    assert (await upload(client, headers, blog_id, b"other-length", key=key)).status_code == 409
    assert (await client.get(f"/blogs/media/{item['id']}", headers=outsider)).status_code == 404
    assert (await client.get(f"/blogs/media/{item['id']}" )).status_code == 401


async def test_header_and_actual_byte_bounds_release_reservation(client, monkeypatch):
    headers, blog_id = await author(client)
    assert (await upload(client, headers, blog_id, **{"Content-Type": "text/plain"})).status_code == 415
    assert (await upload(client, headers, blog_id, **{"Idempotency-Key": "bad"})).status_code == 400
    assert (await upload(client, headers, blog_id, b"123", **{"Content-Length": "2"})).status_code == 413
    assert (await upload(client, headers, blog_id, b"12", **{"Content-Length": "3"})).status_code == 400
    async with SessionLocal() as session:
        assets = (await session.execute(select(BlogMediaAsset))).scalars().all()
        assert len(assets) == 2
        assert all(a.status == "failed" and a.reserved_bytes == 0 for a in assets)
    assert not list(Path(get_settings().blog_media_dir).glob("*.source"))
    monkeypatch.setattr(get_settings(), "blog_media_max_bytes", 4)
    assert (await upload(client, headers, blog_id, b"12345")).status_code == 413

    async def no_length():
        yield b"123"

    missing = await upload(client, headers, blog_id, no_length())
    assert missing.status_code == 411


async def test_reservation_quota_and_pending_concurrency(client, monkeypatch):
    headers, blog_id = await author(client)
    responses = await asyncio.gather(*[upload(client, headers, blog_id) for _ in range(3)])
    assert sorted(r.status_code for r in responses) == [202, 202, 429]
    assert next(r for r in responses if r.status_code == 429).headers["retry-after"] == "30"
    monkeypatch.setattr(get_settings(), "blog_media_blog_quota_bytes", 1)
    assert (await upload(client, headers, blog_id)).status_code == 413


async def test_real_normalization_ready_ranges_metadata_and_own_draft(client, real_video):
    headers, _, asset = await prepared(client, real_video)
    outsider = await login(client, "reader@example.ru")
    url = f"/blogs/media/{asset.id}/video"
    status = await client.get(f"/blogs/media/{asset.id}", headers=headers)
    assert status.json()["media"]["url"] == url
    assert status.json()["media"]["width"] == 64
    assert status.headers["cache-control"] == "private, no-store"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=outsider)).status_code == 404
    full = await client.get(url, headers=headers)
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
    assert len(full.content) == asset.output_bytes
    assert hashlib.sha256(full.content).hexdigest() == asset.sha256
    assert full.headers["cache-control"] == "private, no-store"
    assert "Authorization" in full.headers["vary"].split(", ")
    part = await client.get(url, headers={**headers, "Range": "bytes=7-16"})
    assert part.status_code == 206 and part.content == full.content[7:17]
    assert part.headers["content-range"] == f"bytes 7-16/{asset.output_bytes}"
    suffix = await client.get(url, headers={**headers, "Range": "bytes=-10"})
    assert suffix.content == full.content[-10:]
    head = await client.head(url, headers=headers)
    assert head.status_code == 200 and not head.content
    assert head.headers["content-length"] == str(asset.output_bytes)
    for invalid in ("bytes=9999999-", "bytes=1-0", "bytes=-0", "bytes=0-1,2-3"):
        response = await client.get(url, headers={**headers, "Range": invalid})
        assert response.status_code == 416 and response.headers["content-range"] == f"bytes */{asset.output_bytes}"
    thumb = await client.get(f"/blogs/media/{asset.id}/thumbnail", headers=headers)
    assert thumb.status_code == 200 and thumb.content.startswith(b"\xff\xd8")
    assert (await client.get(f"/blogs/media/{asset.id}/source", headers=headers)).status_code == 404
    assert not Path(get_settings().blog_media_dir, asset.source_key + ".source").exists()
    await media.verify_ready_asset(asset)
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags",
        "-of", "json", str(Path(get_settings().blog_media_dir, asset.output_key + ".mp4"))],
        check=True, timeout=10, stdout=subprocess.PIPE, text=True).stdout
    assert "PRIVATE_" not in info and "55.75" not in info and "location" not in info.lower()


async def test_published_access_revoked_immediately_and_cannot_delete_referenced(client, real_video):
    headers, blog_id, asset = await prepared(client, real_video)
    reader = await login(client, "reader@example.ru")
    post = await client.post(f"/blogs/{blog_id}/posts", headers=headers, json={
        "client_id": uuid.uuid4().hex, "title": "An evening", "description": "Cook something together",
        "topics": ["food"], "language": "en", "media_id": asset.id})
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]
    submitted = await client.post(f"/blogs/{blog_id}/posts/{post_id}/submit", headers=headers,
        json={"revision": 1, "public_consent": True, "consent_version": "public-post-v1"})
    assert submitted.status_code == 200, submitted.text
    async with SessionLocal() as session:
        await session.execute(update(PublicPost).where(PublicPost.id == post_id).values(
            status="published", reviewed_revision=1, reviewed_media_sha256=asset.sha256,
            reviewed_at=utcnow(), published_at=utcnow()))
        await session.commit()
    url = f"/blogs/media/{asset.id}/video"
    assert (await client.get(url, headers=reader)).status_code == 200
    assert (await client.delete(f"/blogs/media/{asset.id}", headers=headers)).status_code == 409
    withdrawn = await client.post(f"/blogs/{blog_id}/withdraw", headers=headers, json={"revision": 1})
    assert withdrawn.status_code == 200, withdrawn.text
    assert (await client.get(url, headers=reader)).status_code == 404
    assert (await client.get(f"/blogs/media/{asset.id}/thumbnail", headers=reader)).status_code == 404
    assert (await client.get(url, headers=headers)).status_code == 200


async def test_invalid_video_fails_safely_and_cleans_source(client, real_video):
    headers, blog_id = await author(client)
    response = await upload(client, headers, blog_id, b"not a video; secret text")
    assert await worker.process_one()
    asset = await asset_row(response.json()["id"])
    assert asset.status == "failed" and asset.reserved_bytes == 0
    assert asset.last_error == "invalid_video"
    assert not list(Path(get_settings().blog_media_dir).iterdir())
    assert "secret" not in json.dumps((await client.get(f"/blogs/media/{asset.id}", headers=headers)).json())


async def test_duration_limit_is_actual_probe_not_upload_claim(client, real_video, monkeypatch):
    headers, blog_id = await author(client)
    response = await upload(client, headers, blog_id, real_video)
    monkeypatch.setattr(get_settings(), "blog_media_max_seconds", 0.5)
    assert await worker.process_one()
    asset = await asset_row(response.json()["id"])
    assert asset.status == "failed" and asset.last_error == "video_too_long"


async def test_worker_does_not_need_api_data_keys_or_decrypt_profiles(client, real_video, monkeypatch):
    headers, blog_id = await author(client)
    response = await upload(client, headers, blog_id, real_video)
    async with SessionLocal() as session:
        raw = await session.scalar(select(type_coerce(PublicBlog.name, Text)).where(PublicBlog.id == blog_id))
        assert raw.startswith("enc1:")

    def forbidden(_):
        raise AssertionError("worker must not decrypt public profile/draft text")

    with monkeypatch.context() as restricted:
        restricted.setattr(get_settings(), "env", "staging")
        restricted.setattr(get_settings(), "data_keys", "")
        restricted.setattr(crypto, "decrypt", forbidden)
        crypto._keys.cache_clear()
        assert await worker.process_one()
        assert (await asset_row(response.json()["id"])).status == "ready"
        assert await media.cleanup_blog_media() == 0
    crypto._keys.cache_clear()


async def test_leases_reclaim_and_stale_claim_cannot_process_or_publish(client):
    headers, blog_id = await author(client)
    response = await upload(client, headers, blog_id)
    first = await worker.claim_next()
    assert first.id == response.json()["id"]
    assert await worker.claim_next() is None
    async with SessionLocal() as session:
        await session.execute(update(BlogMediaAsset).where(BlogMediaAsset.id == first.id)
                              .values(lease_until=utcnow() - timedelta(seconds=1)))
        await session.commit()
    second = await worker.claim_next()
    assert second.id == first.id and second.token != first.token
    assert not await worker.process_claim(first)
    row = await asset_row(second.id)
    assert row.lease_token == second.token and row.attempts == 2


async def test_cancel_during_processing_never_marks_ready(client, real_video, monkeypatch):
    headers, blog_id = await author(client)
    response = await upload(client, headers, blog_id, real_video)
    asset_id = response.json()["id"]

    async def cancel_while_processing(source_fd, output_fd, thumbnail_fd):
        os.write(output_fd, b"not published")
        os.write(thumbnail_fd, b"not published")
        cancelled = await client.delete(f"/blogs/media/{asset_id}", headers=headers)
        assert cancelled.status_code == 204
        return worker.VideoInfo(64, 48, 1000, True)

    monkeypatch.setattr(worker, "normalize", cancel_while_processing)
    assert await worker.process_one()
    asset = await asset_row(asset_id)
    assert asset.status == "deleting" and asset.sha256 is None
    assert not list(Path(get_settings().blog_media_dir).glob("*.mp4"))
    await media.cleanup_blog_media(now=utcnow() + timedelta(seconds=get_settings().blog_media_lease_seconds + 1))
    assert await asset_row(asset_id) is None
    assert not list(Path(get_settings().blog_media_dir).iterdir())


async def test_cleanup_expired_upload_and_cascade_orphans(client):
    headers, blog_id = await author(client)
    response = await upload(client, headers, blog_id)
    asset = await asset_row(response.json()["id"])
    source = Path(get_settings().blog_media_dir, asset.source_key + ".source")
    async with SessionLocal() as session:
        await session.execute(update(BlogMediaAsset).where(BlogMediaAsset.id == asset.id).values(
            status="uploading", lease_until=utcnow() - timedelta(seconds=1)))
        await session.commit()
    await media.cleanup_blog_media()
    assert (await asset_row(asset.id)).status == "failed" and not source.exists()
    response = await upload(client, headers, blog_id)
    asset = await asset_row(response.json()["id"])
    source = Path(get_settings().blog_media_dir, asset.source_key + ".source")
    async with SessionLocal() as session:
        await session.execute(delete(BlogMediaAsset).where(BlogMediaAsset.id == asset.id))
        await session.commit()
    old = (utcnow() - timedelta(days=2)).timestamp()
    os.utime(source, (old, old))
    assert await media.cleanup_blog_media() == 1
    assert not source.exists()


async def test_gc_compare_and_swap_preserves_a_new_worker_lease(client):
    from app.db import engine

    headers, blog_id = await author(client)
    response = await upload(client, headers, blog_id)
    asset_id = response.json()["id"]
    async with SessionLocal() as session:
        await session.execute(update(BlogMediaAsset).where(BlogMediaAsset.id == asset_id).values(
            updated_at=utcnow() - timedelta(days=2)))
        await session.commit()
    claim = None

    class ClaimBeforeDelete(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            nonlocal claim
            if isinstance(statement, Delete) and statement.table.name == "blog_media_assets" and claim is None:
                # After GC's locked re-read but immediately before its SQL
                # DELETE, another worker claims the same old queued candidate.
                claim = await worker.claim_next()
                assert claim is not None
            return await super().execute(statement, *args, **kwargs)

    factory = async_sessionmaker(engine, class_=ClaimBeforeDelete, expire_on_commit=False)
    assert await media.cleanup_blog_media(factory) == 0
    row = await asset_row(asset_id)
    assert row.status == "processing" and row.lease_token == claim.token
    assert Path(get_settings().blog_media_dir, row.source_key + ".source").exists()


async def test_storage_rejects_symlinks_overlap_and_same_size_tamper(client, real_video, monkeypatch):
    headers, blog_id, asset = await prepared(client, real_video)
    path = Path(get_settings().blog_media_dir, asset.output_key + ".mp4")
    original = path.read_bytes()
    path.chmod(0o600)
    path.write_bytes(b"X" + original[1:])
    with pytest.raises(ValueError, match="media_hash_mismatch"):
        await media.verify_ready_asset(asset)
    path.unlink()
    target = Path(get_settings().uploads_dir)
    target.mkdir(parents=True)
    secret = target / "private.txt"
    secret.write_text("private contents")
    path.symlink_to(secret)
    assert (await client.get(f"/blogs/media/{asset.id}/video", headers=headers)).status_code == 404
    monkeypatch.setattr(get_settings(), "blog_media_dir", str(target))
    with pytest.raises(ValueError, match="media_storage_overlaps_private"):
        media.open_root()


async def test_tool_timeout_and_output_are_bounded():
    with pytest.raises(worker.MediaError, match="processing_timeout"):
        await worker._run(sys.executable, ["-c", "import time; time.sleep(3)"],
            fds=(), timeout=1, output_limit=1024, error_code="failed")
    with pytest.raises(worker.MediaError, match="processing_output_limit"):
        await worker._run(sys.executable, ["-c", "print('x' * 100000)"],
            fds=(), timeout=5, output_limit=1024, error_code="failed")


@pytest.mark.parametrize("actually_committed", [False, True])
async def test_ready_commit_failure_never_erases_a_committed_output(client, real_video, actually_committed):
    from app.db import engine

    headers, blog_id = await author(client)
    response = await upload(client, headers, blog_id, real_video)
    asset_id = response.json()["id"]
    injected = False

    class FailingCommit(AsyncSession):
        async def commit(self):
            nonlocal injected
            state = await self.scalar(select(BlogMediaAsset.status).where(BlogMediaAsset.id == asset_id))
            if state == "ready" and not injected:
                injected = True
                if actually_committed:
                    await super().commit()
                raise ConnectionError("simulated acknowledgement loss")
            await super().commit()

    factory = async_sessionmaker(engine, class_=FailingCommit, expire_on_commit=False)
    assert await worker.process_one(factory)
    assert injected
    row = await asset_row(asset_id)
    if actually_committed:
        assert row.status == "ready"
        await media.verify_ready_asset(row)
        assert (await client.get(f"/blogs/media/{asset_id}/video", headers=headers)).status_code == 200
    else:
        assert row.status == "failed" and row.reserved_bytes == 0
        assert not list(Path(get_settings().blog_media_dir).iterdir())


async def test_blog_and_report_json_limits_and_feature_off(client, monkeypatch):
    headers, blog_id = await author(client)
    large = b'{"description":"' + b"x" * (40 * 1024) + b'"}'
    for url in (f"/blogs/{blog_id}/posts", "/content/post-example/report"):
        response = await client.post(url, headers={**headers, "Content-Type": "application/json"}, content=large)
        assert response.status_code == 413
    monkeypatch.setattr(get_settings(), "blogs_enabled", False)
    assert (await upload(client, headers, blog_id)).status_code == 404
    assert (await client.get("/blogs/media/unknown", headers=headers)).status_code == 404
