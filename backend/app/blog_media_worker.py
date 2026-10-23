"""Single-process, restartable public-video worker; no HTTP server or private files.

Run in Dockerfile.media-worker via the public-video compose profile. The database
lease survives restarts; a stale process cannot mark a newer lease's output ready.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import math
import os
import secrets
import shutil
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from fractions import Fraction
from pathlib import Path

# Shared DB settings validate an API JWT secret. This process neither validates
# nor mints tokens. Do not mount/pass the API secret just to satisfy that check.
if __name__ == "__main__":
    os.environ["VECHER_JWT_SECRET"] = secrets.token_urlsafe(48)

from sqlalchemy import and_, or_, select, update

from .blog_media import (_hash_fd, cleanup_blog_media, file_io, open_blob,
                         open_root, sync_root, unlink_blob)
from .blogs import blog_media_write_lock, require_blog_media_owner
from .config import get_settings
from .db import SessionLocal
from .models import BlogMediaAsset, utcnow

log = logging.getLogger("vecher.blog_media")
MAX_PROBE_JSON = 64 * 1024
MAX_STDERR = 16 * 1024
MAX_INPUT_PIXELS = 3840 * 2160
VIDEO_CODECS = {"h264", "hevc", "mpeg4", "mjpeg", "prores", "av1", "vp9"}
AUDIO_CODECS = {"aac", "mp3", "alac", "opus", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"}


class MediaError(Exception):
    """Only a stable code, never tool output, filenames or author metadata."""


@dataclass(frozen=True)
class Claim:
    id: str
    token: str


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration_ms: int
    has_audio: bool


def _eligible(now):
    return or_(BlogMediaAsset.status == "queued",
               and_(BlogMediaAsset.status == "processing", BlogMediaAsset.lease_until <= now))


def _owned(claim: Claim):
    return (BlogMediaAsset.id == claim.id, BlogMediaAsset.status == "processing",
            BlogMediaAsset.lease_token == claim.token)


async def claim_next(session_factory=SessionLocal, *, now=None) -> Claim | None:
    now = now or utcnow()
    settings = get_settings()
    async with session_factory() as session:
        query = select(BlogMediaAsset.id).where(_eligible(now)).order_by(BlogMediaAsset.created_at).limit(16)
        if session.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        for asset_id in (await session.execute(query)).scalars():
            token = str(uuid.uuid4())
            row = (await session.execute(update(BlogMediaAsset).where(
                BlogMediaAsset.id == asset_id, _eligible(now),
                BlogMediaAsset.attempts < settings.blog_media_max_attempts
            ).values(status="processing", lease_token=token,
                     lease_until=now + timedelta(seconds=settings.blog_media_lease_seconds),
                     attempts=BlogMediaAsset.attempts + 1, updated_at=now)
              .returning(BlogMediaAsset.id))).first()
            if row:
                await session.commit()
                return Claim(asset_id, token)
            await session.execute(update(BlogMediaAsset).where(
                BlogMediaAsset.id == asset_id, _eligible(now),
                BlogMediaAsset.attempts >= settings.blog_media_max_attempts
            ).values(status="failed", lease_token=None, lease_until=None, reserved_bytes=0,
                     last_error="processing_attempts_exhausted", updated_at=now))
        await session.commit()
    return None


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path or not os.path.isfile(path):
        raise MediaError("media_tools_unavailable")
    return os.path.abspath(path)


async def _run(executable: str, args: list[str], *, fds: tuple[int, ...], timeout: int,
               output_limit: int, error_code: str) -> bytes:
    command = [sys.executable, "-m", "app.blog_media_subprocess", str(timeout),
               str(output_limit), _tool(executable), *args]
    process = await asyncio.create_subprocess_exec(*command,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, pass_fds=fds, start_new_session=True,
        cwd=Path(__file__).resolve().parent.parent,
        env={"PATH": os.defpath, "LANG": "C.UTF-8", "AV_LOG_FORCE_NOCOLOR": "1"})

    async def read(stream, limit):
        collected = bytearray()
        while chunk := await stream.read(4096):
            collected.extend(chunk)
            if len(collected) > limit:
                raise MediaError("processing_output_limit")
        return bytes(collected)

    tasks = [asyncio.create_task(read(process.stdout, MAX_PROBE_JSON)),
             asyncio.create_task(read(process.stderr, MAX_STDERR)),
             asyncio.create_task(process.wait())]
    try:
        async with asyncio.timeout(timeout):
            stdout, _, code = await asyncio.gather(*tasks)
        if code != 0:
            raise MediaError(error_code)
        return stdout
    except BaseException as error:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if isinstance(error, TimeoutError):
            raise MediaError("processing_timeout") from None
        raise


def _input_args(fd: int) -> list[str]:
    return ["-protocol_whitelist", "file", "-format_whitelist", "mov",
            "-f", "mov", "-enable_drefs", "0", "-use_absolute_path", "0",
            "-threads", "1", "-i", f"/proc/self/fd/{fd}"]


async def probe(fd: int, *, final: bool = False) -> VideoInfo:
    settings = get_settings()
    raw = await _run(settings.blog_media_ffprobe,
        ["-v", "error", "-max_alloc", str(64 * 1024 * 1024), *_input_args(fd),
         "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,duration,avg_frame_rate,r_frame_rate,channels",
         "-of", "json"], fds=(fd,), timeout=settings.blog_media_probe_timeout_seconds,
        output_limit=1024 * 1024, error_code="invalid_video")
    try:
        value = json.loads(raw)
        streams = value["streams"]
        if not isinstance(streams, list) or not 1 <= len(streams) <= 8:
            raise ValueError
        video = [s for s in streams if s.get("codec_type") == "video"]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        # Album covers, extra video tracks, subtitles and arbitrary data tracks
        # are not accepted in this small first upload contract.
        if len(video) != 1 or len(audio) > 1 or len(video) + len(audio) != len(streams):
            raise ValueError
        v = video[0]
        width, height = int(v["width"]), int(v["height"])
        if (v["codec_name"] not in ({"h264"} if final else VIDEO_CODECS)
                or width < 2 or height < 2 or width > 4096 or height > 4096
                or width * height > MAX_INPUT_PIXELS):
            raise ValueError
        for a in audio:
            if a["codec_name"] not in ({"aac"} if final else AUDIO_CODECS) or not 1 <= int(a.get("channels", 0)) <= 8:
                raise ValueError
        fps = float(Fraction(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0"))
        if not math.isfinite(fps) or fps <= 0 or fps > 60.1:
            raise ValueError
        durations = [float(value["format"]["duration"])]
        for stream in streams:
            if "duration" in stream:
                durations.append(float(stream["duration"]))
        if any(not math.isfinite(d) or d <= 0 for d in durations):
            raise ValueError
        duration = max(durations)
        if duration > settings.blog_media_max_seconds + (0.05 if final else 0):
            raise MediaError("video_too_long")
        if final and (max(width, height) > 1280 or min(width, height) > 720 or width % 2 or height % 2):
            raise ValueError
        return VideoInfo(width, height, math.ceil(duration * 1000), bool(audio))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        raise MediaError("unsupported_video") from None


async def normalize(source_fd: int, output_fd: int, thumbnail_fd: int) -> VideoInfo:
    settings = get_settings()
    original = await probe(source_fd)
    scale = ("scale=w='if(gte(iw,ih),min(1280,iw),min(720,iw))':"
             "h='if(gte(iw,ih),min(720,ih),min(1280,ih))':"
             "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=30")
    await _run(settings.blog_media_ffmpeg,
        ["-nostdin", "-v", "error", "-y", "-max_alloc", str(64 * 1024 * 1024),
         "-filter_threads", "1", "-filter_complex_threads", "1", *_input_args(source_fd),
         "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1",
         "-map_metadata:s", "-1", "-map_chapters", "-1", "-sn", "-dn",
         "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
         "-maxrate", "2200k", "-bufsize", "4400k", "-pix_fmt", "yuv420p", "-threads", "1",
         "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-ar", "48000",
         "-t", str(settings.blog_media_max_seconds + 1),
         "-fs", str(settings.blog_media_output_max_bytes), "-movflags", "+faststart",
         "-f", "mp4", f"/proc/self/fd/{output_fd}"],
        fds=(source_fd, output_fd), timeout=settings.blog_media_process_timeout_seconds,
        output_limit=settings.blog_media_output_max_bytes, error_code="processing_failed")
    result = await probe(output_fd, final=True)
    if result.duration_ms < original.duration_ms - 500 or result.has_audio != original.has_audio:
        raise MediaError("incomplete_video")
    await _run(settings.blog_media_ffmpeg,
        ["-nostdin", "-v", "error", "-y", "-max_alloc", str(64 * 1024 * 1024),
         "-filter_threads", "1", *_input_args(output_fd), "-map", "0:v:0",
         "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
         "-frames:v", "1", "-vf", "scale=480:480:force_original_aspect_ratio=decrease",
         "-threads", "1", "-q:v", "4", "-f", "image2", "-update", "1",
         f"/proc/self/fd/{thumbnail_fd}"], fds=(output_fd, thumbnail_fd),
        timeout=settings.blog_media_probe_timeout_seconds,
        output_limit=settings.blog_media_thumbnail_max_bytes, error_code="thumbnail_failed")
    return result


async def _fail(claim: Claim, code: str, session_factory) -> bool:
    async with session_factory() as session:
        result = await session.execute(update(BlogMediaAsset).where(*_owned(claim)).values(
            status="failed", reserved_bytes=0, lease_token=None, lease_until=None,
            last_error=code, updated_at=utcnow()))
        await session.commit()
        return result.rowcount == 1


async def process_claim(claim: Claim, session_factory=SessionLocal) -> bool:
    """Process one immutable source. False means failed, deleted or stale lease."""
    fds, outputs = [], []
    source_key, published = None, False
    try:
        async with session_factory() as session:
            asset = await session.scalar(select(BlogMediaAsset).where(*_owned(claim)))
            if not asset:
                return False
            source_key = asset.source_key
            await require_blog_media_owner(session, asset.uploader_id, asset.blog_id)
        source_fd = open_blob(source_key, "source")
        fds.append(source_fd)
        if (os.fstat(source_fd).st_size != asset.source_bytes
                or not 0 < asset.source_bytes <= get_settings().blog_media_max_bytes):
            raise MediaError("source_size_mismatch")
        digest, size = await file_io(_hash_fd, source_fd)
        if size != asset.source_bytes or digest != asset.source_sha256:
            raise MediaError("source_hash_mismatch")
        output_key, thumbnail_key = uuid.uuid4().hex, uuid.uuid4().hex
        output_fd = open_blob(output_key, "video", create=True)
        fds.append(output_fd)
        outputs.append((output_key, "video"))
        thumbnail_fd = open_blob(thumbnail_key, "thumbnail", create=True)
        fds.append(thumbnail_fd)
        outputs.append((thumbnail_key, "thumbnail"))
        result = await normalize(source_fd, output_fd, thumbnail_fd)
        await file_io(os.fsync, output_fd)
        await file_io(os.fsync, thumbnail_fd)
        os.fchmod(output_fd, 0o400)
        os.fchmod(thumbnail_fd, 0o400)
        await file_io(sync_root)
        digest, size = await file_io(_hash_fd, output_fd)
        thumb_size = os.fstat(thumbnail_fd).st_size
        if (not 0 < size <= get_settings().blog_media_output_max_bytes
                or not 0 < thumb_size <= get_settings().blog_media_thumbnail_max_bytes):
            raise MediaError("processed_size_limit")
        async with session_factory() as session:
            async with blog_media_write_lock(session, asset.blog_id):
                await require_blog_media_owner(session, asset.uploader_id, asset.blog_id)
                changed = await session.execute(update(BlogMediaAsset).where(
                    *_owned(claim), BlogMediaAsset.lease_until > utcnow()
                ).values(status="ready", output_key=output_key, thumbnail_key=thumbnail_key,
                         output_bytes=size, thumbnail_bytes=thumb_size, reserved_bytes=size + thumb_size,
                         sha256=digest, width=result.width, height=result.height, duration_ms=result.duration_ms,
                         lease_token=None, lease_until=None, last_error=None, updated_at=utcnow()))
                # Protect complete output files before an ambiguously failed
                # commit. If it rolled back, orphan GC collects them later;
                # deleting an output of an actually committed ready row is unsafe.
                published = changed.rowcount == 1
                await session.commit()
        if published:
            await file_io(unlink_blob, source_key, "source")
        return published
    except asyncio.CancelledError:
        # Keep the lease and immutable source. A restart reclaims after expiry.
        raise
    except Exception as error:
        from fastapi import HTTPException
        code = (str(error) if isinstance(error, MediaError) else
                "processing_cancelled" if isinstance(error, HTTPException) else "processing_failed")
        if await _fail(claim, code, session_factory):
            # The CAS proves the ready transaction did not commit. Complete
            # temporary outputs can now be removed without an ambiguity window.
            published = False
            if source_key:
                await file_io(unlink_blob, source_key, "source")
        log.warning("blog_media_failed id=%s code=%s", claim.id, code)
        return False
    finally:
        for fd in fds:
            os.close(fd)
        if not published:
            for key, kind in outputs:
                await file_io(unlink_blob, key, kind)
            if source_key:
                async with session_factory() as session:
                    cancelled = await session.execute(update(BlogMediaAsset).where(
                        BlogMediaAsset.id == claim.id, BlogMediaAsset.status == "deleting",
                        BlogMediaAsset.lease_token == claim.token
                    ).values(reserved_bytes=0, lease_token=None, lease_until=None, updated_at=utcnow()))
                    await session.commit()
                    if cancelled.rowcount:
                        await file_io(unlink_blob, source_key, "source")


async def process_one(session_factory=SessionLocal) -> bool:
    claim = await claim_next(session_factory)
    if not claim:
        return False
    await process_claim(claim, session_factory)
    return True


async def _main(once: bool, cleanup_only: bool) -> None:
    from .db import engine
    from .migrate import check
    settings = get_settings()
    if not settings.blogs_enabled and not cleanup_only:
        raise SystemExit("blogs_disabled")
    await check(engine)
    directory = open_root()
    try:
        lock = os.open(".worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("media_worker_already_running") from None
        if not cleanup_only:
            _tool(settings.blog_media_ffmpeg)
            _tool(settings.blog_media_ffprobe)
        last_cleanup = 0.0
        while True:
            now = asyncio.get_running_loop().time()
            if now - last_cleanup >= 60 or cleanup_only:
                await cleanup_blog_media()
                last_cleanup = now
            if cleanup_only:
                return
            worked = await process_one()
            if once:
                return
            if not worked:
                await asyncio.sleep(settings.blog_media_worker_poll_seconds)
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_main(args.once, args.cleanup_only))


if __name__ == "__main__":
    main()
