"""Обновление приложения файлом: версия и скачивание APK."""
import json

from app.config import get_settings


async def test_version_and_download(client, tmp_path):
    get_settings().downloads_dir = str(tmp_path)
    try:
        assert (await client.get("/app/version")).status_code == 404  # ещё ничего не опубликовано
        (tmp_path / "vecher.apk").write_bytes(b"PK-fake-apk")
        (tmp_path / "version.json").write_text(json.dumps(
            {"version_code": 3, "version_name": "0.1.2", "notes": "Чат и места"}), encoding="utf-8")
        v = (await client.get("/app/version")).json()
        assert v["version_code"] == 3 and v["notes"] == "Чат и места" and v["required"] is False
        assert v["url"].endswith("/download/vecher.apk")
        r = await client.get("/download/vecher.apk")
        assert r.status_code == 200 and r.content == b"PK-fake-apk"
        assert r.headers["content-type"] == "application/vnd.android.package-archive"
    finally:
        get_settings().downloads_dir = "downloads"
