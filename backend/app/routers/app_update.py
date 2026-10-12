"""Обновление приложения без магазина (пока «Вечер» раздаётся файлом).

На сервере в папке downloads лежат vecher.apk и version.json:
{"version_code": 3, "version_name": "0.1.2", "notes": "Что нового", "required": false}
Приложение при запуске спрашивает /app/version и, если сборка новее, предлагает обновиться.
Файлы кладёт автосборка GitHub (PUT /app/release с токеном) или скрипт app/release.ps1 с ПК.
"""
import hmac
import json
import os
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import get_settings

router = APIRouter(tags=["app"])


class VersionOut(BaseModel):
    version_code: int
    version_name: str
    notes: str = ""
    required: bool = False
    url: str


def _dir() -> Path:
    return Path(get_settings().downloads_dir)


@router.get("/app/version", response_model=VersionOut)
async def version(request: Request):
    path = _dir() / "version.json"
    if not path.exists() or not (_dir() / "vecher.apk").exists():
        raise HTTPException(404, "Версия для скачивания пока не опубликована")
    info = json.loads(path.read_text(encoding="utf-8-sig"))
    return VersionOut(
        version_code=int(info["version_code"]),
        version_name=str(info.get("version_name", "")),
        notes=str(info.get("notes", "")),
        required=bool(info.get("required", False)),
        url=str(request.url_for("download_apk")),
    )


@router.get("/download/vecher.apk", name="download_apk")
async def download_apk():
    path = _dir() / "vecher.apk"
    if not path.exists():
        raise HTTPException(404, "Файл пока не опубликован")
    return FileResponse(path, media_type="application/vnd.android.package-archive", filename="vecher.apk")


MAX_APK = 200 * 1024 * 1024


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
    tmp_apk, tmp_json = d / "vecher.apk.upload", d / "version.json.upload"
    size = 0
    with tmp_apk.open("wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_APK:
                f.close()
                tmp_apk.unlink(missing_ok=True)
                raise HTTPException(413, "Файл слишком большой")
            f.write(chunk)
    with tmp_apk.open("rb") as f:
        if f.read(2) != b"PK":  # APK — это zip
            tmp_apk.unlink(missing_ok=True)
            raise HTTPException(422, "Это не APK")
    tmp_json.write_text(json.dumps({"version_code": version_code, "version_name": version_name,
                                    "notes": notes, "required": required}, ensure_ascii=False), encoding="utf-8")
    # Сначала APK, потом version.json — никто не увидит «новую версию» раньше, чем файл готов.
    os.replace(tmp_apk, d / "vecher.apk")
    os.replace(tmp_json, d / "version.json")
    return {"ok": True, "version_code": version_code, "bytes": size}
