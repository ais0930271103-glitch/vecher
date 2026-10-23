"""Bounded, authenticated public-blog media storage, separate from private chat.

The API only streams an upload to quarantine and reserves a durable SQL job.
FFmpeg runs in the separately restricted media-worker container. Only its ready
MP4/JPEG outputs can be read; access is checked afresh for every range request.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import uuid
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from weakref import WeakKeyDictionary

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from .blogs import (blog_media_write_lock, blog_write_lock, can_read_blog_media, media_out,
                    require_blog_owner, require_enabled)
from .config import get_settings
from .db import SessionLocal, get_session
from .deps import consented_user
from .models import BlogMediaAsset, PublicPost, User, utcnow

def _no_store(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"


router = APIRouter(tags=["blog-media"], dependencies=[Depends(_no_store)])
KEY = re.compile(r"^[0-9a-f]{32}$")
UPLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
KINDS = {"source": ".source", "video": ".mp4", "thumbnail": ".jpg"}
PENDING = ("uploading", "queued", "processing")
CHUNK_BYTES = 64 * 1024
_quota_locks: WeakKeyDictionary = WeakKeyDictionary()


async def file_io(function, *args, **kwargs):
    """Do not close/reuse a descriptor while a cancelled thread still owns it."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _filename(key: str, kind: str) -> str:
    if not isinstance(key, str) or not KEY.fullmatch(key) or kind not in KINDS:
        raise ValueError("invalid_media_key")
    return key + KINDS[kind]


def open_root() -> int:
    """Walk each directory with O_NOFOLLOW; never follow a configured symlink."""
    settings = get_settings()
    root = Path(os.path.abspath(settings.blog_media_dir))
    private = Path(os.path.abspath(settings.uploads_dir))
    if root == private or root in private.parents or private in root.parents:
        raise ValueError("media_storage_overlaps_private")
    if settings.is_dev:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root.anchor, flags)
    try:
        for component in root.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_blob(key: str, kind: str, *, create: bool = False) -> int:
    directory = open_root()
    flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL) if create else os.O_RDONLY
    try:
        fd = os.open(_filename(key, kind), flags | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("invalid_media_file")
        return fd
    finally:
        os.close(directory)


def unlink_blob(key: str | None, kind: str) -> None:
    if not key:
        return
    directory = open_root()
    try:
        try:
            os.unlink(_filename(key, kind), dir_fd=directory)
        except FileNotFoundError:
            pass
    finally:
        os.close(directory)


def sync_root() -> None:
    directory = open_root()
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view[:CHUNK_BYTES])
        if written <= 0:
            raise OSError("media_write_failed")
        view = view[written:]


def _hash_fd(fd: int) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _verify_ready_sync(asset: BlogMediaAsset) -> None:
    if asset.status != "ready" or not asset.sha256 or not asset.output_key or not asset.thumbnail_key:
        raise ValueError("media_not_ready")
    fd = open_blob(asset.output_key, "video")
    try:
        info = os.fstat(fd)
        if info.st_size != asset.output_bytes or info.st_size > get_settings().blog_media_output_max_bytes:
            raise ValueError("media_size_mismatch")
        digest, size = _hash_fd(fd)
        if digest != asset.sha256 or size != asset.output_bytes:
            raise ValueError("media_hash_mismatch")
    finally:
        os.close(fd)
    thumb = open_blob(asset.thumbnail_key, "thumbnail")
    try:
        if (os.fstat(thumb).st_size != asset.thumbnail_bytes or not asset.thumbnail_bytes
                or asset.thumbnail_bytes > get_settings().blog_media_thumbnail_max_bytes):
            raise ValueError("media_thumbnail_mismatch")
    finally:
        os.close(thumb)


async def verify_ready_asset(asset: BlogMediaAsset) -> None:
    """Operator approval gate: verify actual immutable output bytes, off-loop."""
    try:
        await file_io(_verify_ready_sync, asset)
    except (OSError, ValueError) as error:
        code = str(error) if isinstance(error, ValueError) else "media_file_unavailable"
        raise ValueError(code) from None


def status_out(asset: BlogMediaAsset) -> dict:
    return {"id": asset.id, "status": asset.status, "source_bytes": asset.source_bytes,
            "error_code": asset.last_error,
            "media": media_out(asset) if asset.status == "ready" else None}


@asynccontextmanager
async def _quota_lock(session: AsyncSession):
    lock = _quota_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        # The local lock supports the single-process API and SQLite tests. This
        # transaction-level lock also serializes reservations across PG workers.
        if session.bind.dialect.name == "postgresql":
            await session.execute(text("SELECT pg_advisory_xact_lock(17011467, 4)"))
        yield


def _available_storage(required: int) -> bool:
    fd = open_root()
    try:
        free = os.fstatvfs(fd)
        return free.f_bavail * free.f_frsize >= required
    finally:
        os.close(fd)


async def _reserve(session: AsyncSession, user: User, blog_id: str, upload_id: str,
                   expected: int, mime: str) -> tuple[BlogMediaAsset, bool]:
    settings = get_settings()
    async with blog_write_lock(session, blog_id):
        await require_blog_owner(session, user.id, blog_id, write=True)
        existing = await session.scalar(select(BlogMediaAsset).where(
            BlogMediaAsset.blog_id == blog_id, BlogMediaAsset.uploader_id == user.id,
            BlogMediaAsset.client_upload_id == upload_id))
        if existing:
            if existing.source_bytes != expected or existing.mime != mime:
                raise HTTPException(409, "upload_id_conflict")
            return existing, False
        async with _quota_lock(session):
            reserved = expected + settings.blog_media_output_max_bytes + settings.blog_media_thumbnail_max_bytes
            count, used = (await session.execute(select(
                func.count(), func.coalesce(func.sum(BlogMediaAsset.reserved_bytes), 0)
            ).where(BlogMediaAsset.blog_id == blog_id))).one()
            total_count, total, pending_bytes = (await session.execute(select(
                func.count(),
                func.coalesce(func.sum(BlogMediaAsset.reserved_bytes), 0),
                func.coalesce(func.sum(BlogMediaAsset.reserved_bytes).filter(BlogMediaAsset.status.in_(PENDING)), 0)
            ))).one()
            pending = await session.scalar(select(func.count()).select_from(BlogMediaAsset)
                                           .where(BlogMediaAsset.status.in_(PENDING)))
            own_pending = await session.scalar(select(func.count()).select_from(BlogMediaAsset).where(
                BlogMediaAsset.blog_id == blog_id, BlogMediaAsset.status.in_(PENDING)))
            if (count >= settings.blog_media_max_assets_per_blog
                    or total_count >= settings.blog_media_max_assets_global
                    or used + reserved > settings.blog_media_blog_quota_bytes
                    or total + reserved > settings.blog_media_global_quota_bytes):
                raise HTTPException(413, "media_quota_exceeded")
            if (pending >= settings.blog_media_max_pending_global
                    or own_pending >= settings.blog_media_max_pending_per_blog):
                raise HTTPException(429, "media_queue_full", headers={"Retry-After": "30"})
            try:
                enough = await file_io(_available_storage,
                    reserved + pending_bytes + settings.blog_media_min_free_bytes)
            except (OSError, ValueError):
                raise HTTPException(503, "media_storage_unavailable") from None
            if not enough:
                raise HTTPException(503, "media_storage_full", headers={"Retry-After": "60"})
            asset = BlogMediaAsset(id=str(uuid.uuid4()), blog_id=blog_id, uploader_id=user.id,
                client_upload_id=upload_id, status="uploading", source_key=uuid.uuid4().hex,
                source_bytes=expected, reserved_bytes=reserved, mime=mime,
                lease_until=utcnow() + timedelta(seconds=settings.blog_media_upload_timeout_seconds + 60))
            session.add(asset)
            await session.commit()
            return asset, True


async def _abort_upload(asset_id: str, key: str, code: str) -> None:
    # A lost HTTP response after a committed queue transition must not destroy
    # the accepted source. Read the durable state before touching its file.
    async with SessionLocal() as session:
        asset = await session.get(BlogMediaAsset, asset_id)
        if asset and asset.status not in {"uploading", "failed", "deleting"}:
            return
        if asset and asset.status == "uploading":
            asset.status, asset.last_error = "failed", code
            asset.reserved_bytes, asset.lease_until, asset.updated_at = 0, None, utcnow()
            await session.commit()
        elif asset and asset.status == "deleting" and not asset.lease_token:
            asset.reserved_bytes, asset.lease_until, asset.updated_at = 0, None, utcnow()
            await session.commit()
        await file_io(unlink_blob, key, "source")


@router.post("/blogs/{blog_id}/media", status_code=202)
async def upload_media(blog_id: str, request: Request,
                       user: User = Depends(consented_user),
                       session: AsyncSession = Depends(get_session)):
    require_enabled()
    # Authorization is deliberately ahead of any read from request.stream().
    await require_blog_owner(session, user.id, blog_id, write=True)
    settings = get_settings()
    if len(request.headers.getlist("content-length")) != 1 or "transfer-encoding" in request.headers:
        raise HTTPException(411, "content_length_required")
    raw_length = request.headers.get("content-length", "")
    if len(raw_length) > 12 or not raw_length.isascii() or not raw_length.isdecimal():
        raise HTTPException(400, "invalid_content_length")
    expected = int(raw_length)
    if expected < 1 or expected > settings.blog_media_max_bytes:
        raise HTTPException(413, "video_size_limit")
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime not in {"video/mp4", "video/quicktime"}:
        raise HTTPException(415, "mp4_or_mov_required")
    upload_id = request.headers.get("idempotency-key", "")
    if not UPLOAD_ID.fullmatch(upload_id):
        raise HTTPException(400, "idempotency_key_required")
    asset, created = await _reserve(session, user, blog_id, upload_id, expected, mime)
    if not created:
        return status_out(asset)
    asset_id, source_key = asset.id, asset.source_key
    fd = None
    try:
        fd = open_blob(source_key, "source", create=True)
        digest, received = hashlib.sha256(), 0
        async with asyncio.timeout(settings.blog_media_upload_timeout_seconds):
            async for chunk in request.stream():
                received += len(chunk)
                if received > expected or received > settings.blog_media_max_bytes:
                    raise HTTPException(413, "video_size_limit")
                if chunk:
                    await file_io(_write_all, fd, chunk)
                    digest.update(chunk)
        if received != expected:
            raise HTTPException(400, "upload_length_mismatch")
        await file_io(os.fsync, fd)
        await file_io(sync_root)
        os.close(fd)
        fd = None
        async with blog_write_lock(session, blog_id):
            await require_blog_owner(session, user.id, blog_id, write=True)
            changed = await session.execute(update(BlogMediaAsset).where(
                BlogMediaAsset.id == asset_id, BlogMediaAsset.status == "uploading"
            ).values(status="queued", source_sha256=digest.hexdigest(), lease_until=None, updated_at=utcnow()))
            if changed.rowcount != 1:
                raise HTTPException(409, "upload_cancelled")
            await session.commit()
            await session.refresh(asset)
        return status_out(asset)
    except BaseException as error:
        if fd is not None:
            os.close(fd)
        await session.rollback()
        code = "upload_timeout" if isinstance(error, TimeoutError) else "upload_interrupted"
        await asyncio.shield(_abort_upload(asset_id, source_key, code))
        if isinstance(error, TimeoutError):
            raise HTTPException(408, code) from None
        if isinstance(error, OSError):
            raise HTTPException(503, "media_storage_unavailable") from None
        raise


@router.get("/blogs/media/{asset_id}")
async def media_status(asset_id: str, user: User = Depends(consented_user),
                       session: AsyncSession = Depends(get_session)):
    require_enabled()
    asset = await session.get(BlogMediaAsset, asset_id)
    if not asset:
        raise HTTPException(404, "media_not_found")
    await require_blog_owner(session, user.id, asset.blog_id)
    return status_out(asset)


async def schedule_blog_media_deletion(session: AsyncSession, asset_ids: Iterable[str]) -> None:
    ids = list(asset_ids)
    if ids:
        await session.execute(update(BlogMediaAsset).where(BlogMediaAsset.id.in_(ids)).values(
            status="deleting", updated_at=utcnow()))


@router.delete("/blogs/media/{asset_id}", status_code=204)
async def discard_media(asset_id: str, user: User = Depends(consented_user),
                        session: AsyncSession = Depends(get_session)):
    require_enabled()
    asset = await session.get(BlogMediaAsset, asset_id)
    if not asset:
        raise HTTPException(404, "media_not_found")
    async with blog_write_lock(session, asset.blog_id):
        # Removing one's unreferenced draft also remains possible after consent
        # withdrawal; it cannot create or republish public material.
        await require_blog_owner(session, user.id, asset.blog_id)
        if await session.scalar(select(PublicPost.id).where(PublicPost.media_id == asset.id).limit(1)):
            raise HTTPException(409, "media_in_use")
        await schedule_blog_media_deletion(session, [asset.id])
        await session.commit()
    return Response(status_code=204)


def _byte_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if not value:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value.strip()) if len(value) <= 100 else None
    if not match or (not match[1] and not match[2]):
        raise HTTPException(416, "invalid_range", headers={"Content-Range": f"bytes */{size}"})
    if not match[1]:
        suffix = int(match[2])
        start, end = max(0, size - suffix), size - 1
        if suffix == 0:
            start = size
    else:
        start = int(match[1])
        end = min(int(match[2]), size - 1) if match[2] else size - 1
    if start >= size or end < start:
        raise HTTPException(416, "invalid_range", headers={"Content-Range": f"bytes */{size}"})
    return start, end, True


async def _serve(asset_id: str, kind: str, request: Request, user: User, session: AsyncSession):
    require_enabled()
    asset = await session.get(BlogMediaAsset, asset_id)
    if not asset or not await can_read_blog_media(session, user.id, asset):
        raise HTTPException(404, "media_not_found")
    key, expected = ((asset.output_key, asset.output_bytes) if kind == "video"
                     else (asset.thumbnail_key, asset.thumbnail_bytes))
    try:
        fd = open_blob(key, kind)
    except (OSError, ValueError, TypeError):
        raise HTTPException(404, "media_not_found") from None
    try:
        size = os.fstat(fd).st_size
        limit = (get_settings().blog_media_output_max_bytes if kind == "video"
                 else get_settings().blog_media_thumbnail_max_bytes)
        if size != expected or size <= 0 or size > limit:
            raise HTTPException(404, "media_not_found")
        start, end, partial = _byte_range(request.headers.get("range"), size)
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
                   "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                   "Vary": "Authorization",
                   "Content-Disposition": 'inline; filename="video.mp4"' if kind == "video" else 'inline; filename="thumbnail.jpg"'}
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        mime = "video/mp4" if kind == "video" else "image/jpeg"
        status_code = 206 if partial else 200
        if request.method == "HEAD":
            os.close(fd)
            return Response(status_code=status_code, headers=headers, media_type=mime)
    except BaseException:
        os.close(fd)
        raise

    closed = False

    def close():
        nonlocal closed
        if not closed:
            closed = True
            os.close(fd)

    async def chunks():
        remaining = end - start + 1
        try:
            os.lseek(fd, start, os.SEEK_SET)
            while remaining:
                chunk = await file_io(os.read, fd, min(CHUNK_BYTES, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk
        finally:
            close()

    return StreamingResponse(chunks(), status_code=status_code, headers=headers,
                             media_type=mime, background=BackgroundTask(close))


@router.api_route("/blogs/media/{asset_id}/video", methods=["GET", "HEAD"])
async def stream_video(asset_id: str, request: Request, user: User = Depends(consented_user),
                       session: AsyncSession = Depends(get_session)):
    return await _serve(asset_id, "video", request, user, session)


@router.api_route("/blogs/media/{asset_id}/thumbnail", methods=["GET", "HEAD"])
async def stream_thumbnail(asset_id: str, request: Request, user: User = Depends(consented_user),
                           session: AsyncSession = Depends(get_session)):
    return await _serve(asset_id, "thumbnail", request, user, session)


async def cleanup_blog_media(session_factory=SessionLocal, *, now=None) -> int:
    """Bounded durable cleanup, including files orphaned by FK account erasure."""
    now = now or utcnow()
    expiry = now - timedelta(hours=get_settings().blog_media_pending_ttl_hours)
    removed = 0
    async with session_factory() as session:
        # Expired upload leases release reservations even if the API died before
        # accepting the final bytes. Stale processing is reclaimed by the worker.
        expired_keys = (await session.execute(update(BlogMediaAsset).where(
            BlogMediaAsset.status == "uploading", BlogMediaAsset.lease_until < now
        ).values(status="failed", last_error="upload_interrupted", reserved_bytes=0,
                 lease_until=None, updated_at=now).returning(BlogMediaAsset.source_key))).scalars().all()
        await session.commit()
        for key in expired_keys:
            await file_io(unlink_blob, key, "source")
        # A crash between committing ready/failed and unlinking the original is
        # repaired here. Source keys stay in the receipt but never serve bytes.
        finished_sources = (await session.execute(select(BlogMediaAsset.source_key).where(
            BlogMediaAsset.status.in_(("ready", "failed"))
        ).limit(500))).scalars().all()
        for key in finished_sources:
            await file_io(unlink_blob, key, "source")
        unreferenced = ~select(PublicPost.id).where(PublicPost.media_id == BlogMediaAsset.id).exists()
        eligible = (
            or_(BlogMediaAsset.status == "deleting",
                and_(BlogMediaAsset.status.in_(("ready", "failed", "queued")),
                     BlogMediaAsset.updated_at < expiry)),
            or_(BlogMediaAsset.lease_until.is_(None), BlogMediaAsset.lease_until < now),
            unreferenced,
        )
        candidates = list((await session.execute(select(BlogMediaAsset).where(
            *eligible
        ).order_by(BlogMediaAsset.created_at).limit(50))).scalars())
        for asset in candidates:
            # A post may have been attached after the candidate read. Use the same
            # blog lock as authoring, and recheck under that lock.
            try:
                async with blog_media_write_lock(session, asset.blog_id):
                    current = await session.get(BlogMediaAsset, asset.id, populate_existing=True)
                    if not current or await session.scalar(select(PublicPost.id).where(
                            PublicPost.media_id == current.id).limit(1)):
                        continue
                    # Claims do not take the authoring lock. Re-evaluate every
                    # eligibility condition in the DELETE itself: a concurrent
                    # queued -> processing lease must survive this GC snapshot.
                    deleted = (await session.execute(delete(BlogMediaAsset).where(
                        BlogMediaAsset.id == current.id, *eligible
                    ).returning(BlogMediaAsset.id))).first()
                    await session.commit()
                    if not deleted:
                        continue
            except HTTPException:
                await session.rollback()
                continue
            for key, kind in ((asset.source_key, "source"), (asset.output_key, "video"),
                              (asset.thumbnail_key, "thumbnail")):
                await file_io(unlink_blob, key, kind)
            removed += 1
        await session.commit()

    def old_files():
        directory = open_root()
        try:
            found = []
            # Scan at most 2000 entries per pass; no recursive traversal. The
            # 500-asset pilot cap bounds live files below this scan budget.
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 2000:
                        break
                    name = entry.name
                    if len(name) > 40 or not entry.is_file(follow_symlinks=False):
                        continue
                    stem, suffix = os.path.splitext(name)
                    if KEY.fullmatch(stem) and suffix in KINDS.values():
                        info = entry.stat(follow_symlinks=False)
                        if info.st_mtime < expiry.timestamp():
                            found.append((stem, suffix))
            return found
        finally:
            os.close(directory)

    for key, suffix in await file_io(old_files):
        kind = next(kind for kind, extension in KINDS.items() if extension == suffix)
        column = {"source": BlogMediaAsset.source_key, "video": BlogMediaAsset.output_key,
                  "thumbnail": BlogMediaAsset.thumbnail_key}[kind]
        async with session_factory() as session:
            exists = await session.scalar(select(BlogMediaAsset.id).where(column == key))
            if not exists:
                await file_io(unlink_blob, key, kind)
                removed += 1
    return removed
