"""Обновление приложения без магазина (пока «Вечер» раздаётся файлом).

На сервере в папке downloads лежат vecher.apk и version.json:
{"version_code": 3, "version_name": "0.1.2", "notes": "Что нового", "required": false}
Приложение при запуске спрашивает /app/version и, если сборка новее, предлагает обновиться.
Файлы кладёт автосборка GitHub (PUT /app/release с токеном) или скрипт app/release.ps1 с ПК.
"""
import hashlib
import hmac
import json
import os
import stat
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..config import get_settings

router = APIRouter(tags=["app"])


class VersionOut(BaseModel):
    version_code: int
    version_name: str
    notes: str = ""
    required: bool = False
    url: str
    # Old APKs keep using url; new APKs pin and verify the exact published bytes.
    download_path: str
    sha256: str
    bytes: int


def _dir() -> Path:
    return Path(get_settings().downloads_dir)


MAX_APK = 200 * 1024 * 1024
_hash_cache: OrderedDict[tuple, tuple[str, int]] = OrderedDict()
_hash_lock = threading.Lock()
_publish_lock = threading.Lock()


def _identity(value: os.stat_result) -> tuple:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _apk_details(path: Path) -> tuple[str, int]:
    """Hash an open, regular file. Never cache across replacement or mutation."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 2 <= before.st_size <= MAX_APK:
                raise HTTPException(503, "Файл обновления временно недоступен")
            key = _identity(before)
            with _hash_lock:
                cached = _hash_cache.get(key)
            if cached is None:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                cached = (digest.hexdigest(), before.st_size)
            if _identity(os.fstat(stream.fileno())) != key or _identity(path.stat()) != key:
                raise HTTPException(503, "Обновление публикуется, попробуй чуть позже")
            with _hash_lock:
                _hash_cache[key] = cached
                _hash_cache.move_to_end(key)
                while len(_hash_cache) > 4:
                    _hash_cache.popitem(last=False)
            return cached
    except FileNotFoundError as exc:
        raise HTTPException(404, "Файл пока не опубликован") from exc
    except OSError as exc:
        raise HTTPException(503, "Файл обновления временно недоступен") from exc


def _release_details(directory: Path) -> tuple[dict, str, int]:
    path = directory / "version.json"
    try:
        before = path.stat()
        if before.st_size > 64 * 1024:
            raise ValueError("Oversized manifest")
        info = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(info, dict) or type(info.get("version_code")) is not int:
            raise ValueError("Invalid version code")
        if info["version_code"] < 1 or not isinstance(info.get("version_name"), str):
            raise ValueError("Invalid version")
        if not info["version_name"] or len(info["version_name"]) > 32:
            raise ValueError("Invalid version name")
        if not isinstance(info.get("notes", ""), str) or type(info.get("required", False)) is not bool:
            raise ValueError("Invalid release metadata")
        sha256, size = _apk_details(directory / "vecher.apk")
        if _identity(path.stat()) != _identity(before):
            raise ValueError("Manifest changed")
        # New publishers bind the pair. During a replacement window return 503
        # rather than advertising a version with another release's bytes.
        if "sha256" in info and info["sha256"] != sha256:
            raise ValueError("APK and manifest differ")
        if "bytes" in info and (type(info["bytes"]) is not int or info["bytes"] != size):
            raise ValueError("APK size differs")
        return info, sha256, size
    except FileNotFoundError as exc:
        raise HTTPException(404, "Версия для скачивания пока не опубликована") from exc
    except (ValueError, OSError, UnicodeError) as exc:
        raise HTTPException(503, "Обновление публикуется, попробуй чуть позже") from exc


@router.get("/app/version", response_model=VersionOut)
async def version(request: Request, response: Response):
    info, sha256, size = await run_in_threadpool(_release_details, _dir())
    response.headers["Cache-Control"] = "no-store"
    return VersionOut(
        version_code=int(info["version_code"]),
        version_name=str(info.get("version_name", "")),
        notes=str(info.get("notes", "")),
        required=bool(info.get("required", False)),
        url=str(request.url_for("download_apk")),
        download_path=f"/download/vecher.apk?sha256={sha256}",
        sha256=sha256,
        bytes=size,
    )


@router.get("/download/vecher.apk", name="download_apk")
async def download_apk(sha256: str | None = Query(None, pattern=r"^[0-9a-f]{64}$")):
    path = _dir() / "vecher.apk"
    actual_hash, _ = await run_in_threadpool(_apk_details, path)
    if sha256 is not None and not hmac.compare_digest(actual_hash, sha256):
        raise HTTPException(409, "Версия изменилась, проверь обновления ещё раз")
    # A replacement after this check is still detected by the client's hash.
    return FileResponse(path, media_type="application/vnd.android.package-archive", filename="vecher.apk",
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


@router.put("/app/release", include_in_schema=False)
async def publish(
    request: Request,
    version_code: int = Query(ge=1),
    version_name: str = Query(min_length=1, max_length=32),
    notes: str = Query("", max_length=500),
    required: bool = False,
    authorization: str = Header(""),
):
    """Выложить новую сборку: тело запроса — сам APK. Только с токеном автосборки."""
    token = get_settings().release_token
    if not token or not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(403, "Нет доступа")
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    apk_fd, apk_name = tempfile.mkstemp(prefix=".vecher-apk-", dir=d)
    tmp_apk = Path(apk_name)
    tmp_json: Path | None = None
    size = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(apk_fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_APK:
                    raise HTTPException(413, "Файл слишком большой")
                digest.update(chunk)
                f.write(chunk)
        with tmp_apk.open("rb") as f:
            if f.read(2) != b"PK":  # APK — это zip
                raise HTTPException(422, "Это не APK")
        json_fd, json_name = tempfile.mkstemp(prefix=".vecher-version-", dir=d)
        tmp_json = Path(json_name)
        with os.fdopen(json_fd, "w", encoding="utf-8") as f:
            json.dump({"version_code": version_code, "version_name": version_name,
                       "notes": notes, "required": required,
                       "sha256": digest.hexdigest(), "bytes": size}, f, ensure_ascii=False)

        def replace_release():
            # Two uploads in this worker cannot interleave their file pairs.
            # Digest validation also detects interrupted/multi-worker publication.
            with _publish_lock:
                os.replace(tmp_apk, d / "vecher.apk")
                os.replace(tmp_json, d / "version.json")

        await run_in_threadpool(replace_release)
        return {"ok": True, "version_code": version_code, "bytes": size, "sha256": digest.hexdigest()}
    finally:
        tmp_apk.unlink(missing_ok=True)
        if tmp_json is not None:
            tmp_json.unlink(missing_ok=True)
