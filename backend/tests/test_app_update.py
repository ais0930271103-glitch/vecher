"""Обновление приложения файлом: версия и скачивание APK."""
import hashlib
import json

import pytest

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
        digest = hashlib.sha256(b"PK-fake-apk").hexdigest()
        assert v["sha256"] == digest and v["bytes"] == 11
        assert v["download_path"] == f"/download/vecher.apk?sha256={digest}"
        r = await client.get("/download/vecher.apk")
        assert r.status_code == 200 and r.content == b"PK-fake-apk"
        assert r.headers["content-type"] == "application/vnd.android.package-archive"
        assert (await client.get(v["download_path"])).content == r.content
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
        metadata = json.loads((tmp_path / "version.json").read_text())
        assert metadata["sha256"] == v["sha256"] == r.json()["sha256"]
        assert metadata["bytes"] == v["bytes"] == 11
        assert not list(tmp_path.glob(".vecher-*"))
        s.release_token = ""  # без токена на сервере выкладка выключена совсем
        assert (await client.put(q, content=b"PK-x", headers=auth)).status_code == 403
    finally:
        s.downloads_dir, s.release_token = "downloads", ""


async def test_update_pins_actual_bytes_and_detects_replaced_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "downloads_dir", str(tmp_path))
    (tmp_path / "version.json").write_text(json.dumps({"version_code": 11, "version_name": "0.6.0"}))
    (tmp_path / "vecher.apk").write_bytes(b"PK-first")
    response = await client.get("/app/version", headers={"Host": "untrusted.example"})
    assert response.headers["cache-control"] == "no-store"
    manifest = response.json()
    assert manifest["download_path"].startswith("/download/vecher.apk?")
    assert "untrusted" not in manifest["download_path"]
    replacement = tmp_path / "next.apk"
    replacement.write_bytes(b"PK-other")  # Same size, different inode/content.
    replacement.replace(tmp_path / "vecher.apk")
    assert (await client.get(manifest["download_path"])).status_code == 409
    next_manifest = (await client.get("/app/version")).json()
    assert next_manifest["sha256"] == hashlib.sha256(b"PK-other").hexdigest()
    assert next_manifest["sha256"] != manifest["sha256"]


@pytest.mark.parametrize("metadata", [
    "not json", "[]", '{"version_code":true,"version_name":"0.6.0"}',
    '{"version_code":11,"version_name":"0.6.0","required":"false"}',
    json.dumps({"version_code": 11, "version_name": "0.6.0", "sha256": "0" * 64}),
    json.dumps({"version_code": 11, "version_name": "0.6.0", "bytes": 999}),
])
async def test_invalid_or_mismatched_release_is_unavailable(client, tmp_path, monkeypatch, metadata):
    monkeypatch.setattr(get_settings(), "downloads_dir", str(tmp_path))
    (tmp_path / "vecher.apk").write_bytes(b"PK-real")
    (tmp_path / "version.json").write_text(metadata)
    assert (await client.get("/app/version")).status_code == 503


async def test_download_rejects_symlink_and_invalid_pin(client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "downloads_dir", str(tmp_path))
    private = tmp_path / "private.txt"
    private.write_text("not an APK")
    (tmp_path / "vecher.apk").symlink_to(private)
    assert (await client.get("/download/vecher.apk")).status_code == 503
    assert (await client.get("/download/vecher.apk?sha256=../private.txt")).status_code == 422
