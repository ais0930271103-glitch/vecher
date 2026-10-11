"""Обновление приложения без магазина (пока «Вечер» раздаётся файлом).

На сервере в папке downloads лежат vecher.apk и version.json:
{"version_code": 3, "version_name": "0.1.2", "notes": "Что нового", "required": false}
Приложение при запуске спрашивает /app/version и, если сборка новее, предлагает обновиться.
Файлы кладёт скрипт app/release.ps1 с ПК.
"""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
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
