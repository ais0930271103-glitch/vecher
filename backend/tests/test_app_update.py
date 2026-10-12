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


async def test_publish_needs_token_and_apk(client, tmp_path):
    s = get_settings()
    s.downloads_dir, s.release_token = str(tmp_path), "secret-token"
    try:
        q = "/app/release?version_code=7&version_name=0.2.0&notes=Новое"
        assert (await client.put(q, content=b"PK-apk")).status_code == 403
        assert (await client.put(q, content=b"PK-apk", headers={"Authorization": "Bearer wrong"})).status_code == 403
        auth = {"Authorization": "Bearer secret-token"}
        assert (await client.put(q, content=b"not a zip", headers=auth)).status_code == 422
        r = await client.put(q, content=b"PK-real-apk", headers=auth)
        assert r.status_code == 200 and r.json()["bytes"] == 11
        v = (await client.get("/app/version")).json()
        assert v["version_code"] == 7 and v["notes"] == "Новое"
        assert (await client.get("/download/vecher.apk")).content == b"PK-real-apk"
        s.release_token = ""  # без токена на сервере выкладка выключена совсем
        assert (await client.put(q, content=b"PK-x", headers=auth)).status_code == 403
    finally:
        s.downloads_dir, s.release_token = "downloads", ""
