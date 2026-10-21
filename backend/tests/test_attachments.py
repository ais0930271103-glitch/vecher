"""Файлы чата: принадлежность паре, реальные байты, квота, повторы и удаление."""
import asyncio
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Text, func, select, type_coerce

from app import attachments
from app.attachments import cleanup_attachments, delete_attachments
from app.config import get_settings
from app.db import SessionLocal
from app.models import ChatAttachment, ChatMessage, utcnow
from conftest import make_pair


async def upload(client, headers, name="файл.txt", data=b"private document"):
    return await client.post("/chat/attachments", headers=headers, files={"file": (name, data, "application/octet-stream")})


async def file_row(file_id):
    async with SessionLocal() as s:
        return await s.get(ChatAttachment, file_id)


async def test_file_flow_access_encryption_and_retry(client):
    ha, hb = await make_pair(client)
    hc, _ = await make_pair(client, "c@example.ru", "d@example.ru")
    assert (await upload(client, {}, data=b"noauth")).status_code == 401
    data = b"private document\x00\xff"
    r = await upload(client, ha, "../../report.txt", data)
    assert r.status_code == 201, r.text
    file = r.json()
    assert file == {"id": file["id"], "name": "report.txt", "mime": "text/plain", "size": len(data)}
    url = f"/chat/attachments/{file['id']}"
    assert (await client.get(url, headers=hb)).status_code == 404  # ещё не отправлено
    assert (await client.get(url, headers=hc)).status_code == 404
    assert (await client.post("/chat", headers=hb, json={"attachment_ids": [file["id"]]})).status_code == 404
    own = await client.get(url, headers=ha)
    assert own.content == data and own.headers["cache-control"] == "private, no-store"
    assert own.headers["x-content-type-options"] == "nosniff"
    assert own.headers["content-disposition"].startswith("attachment;")
    row = await file_row(file["id"])
    disk = Path(get_settings().uploads_dir) / (row.storage_key + ".bin")
    assert disk.exists() and data not in disk.read_bytes()
    async with SessionLocal() as s:
        raw_name = (await s.execute(select(type_coerce(ChatAttachment.name, Text)))).scalar_one()
        assert raw_name.startswith("enc1:") and "report" not in raw_name
    body = {"text": "держи", "attachment_ids": [file["id"]], "client_message_id": "send-123"}
    sent = await client.post("/chat", headers=ha, json=body)
    assert sent.status_code == 201, sent.text
    message = sent.json()
    assert message["attachments"] == [file] and message["client_message_id"] == "send-123"
    repeated = await client.post("/chat", headers=ha, json=body)
    assert repeated.json()["id"] == message["id"]
    assert (await client.get("/chat/unread", headers=hb)).json()["count"] == 1
    assert (await client.get(url, headers=hb)).content == data
    assert (await client.get(url, headers=hc)).status_code == 404
    assert (await client.delete(url, headers=ha)).status_code == 409
    assert (await client.get("/chat", headers=ha)).json()["messages"][0]["client_message_id"] == "send-123"
    assert (await client.get("/chat", headers=hb)).json()["messages"][0]["client_message_id"] is None
    assert (await client.delete(f"/chat/{message['id']}", headers=hb)).status_code == 403
    gone = await client.delete(f"/chat/{message['id']}", headers=ha)
    assert gone.status_code == 200 and gone.json()["attachments"] == []
    assert not disk.exists() and (await client.get(url, headers=hb)).status_code == 404
    # Повтор не восстанавливает уже удалённое сообщение.
    assert (await client.post("/chat", headers=ha, json=body)).json()["deleted"] is True


async def test_limits_quota_and_discard(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "attachment_max_bytes", 4)
    monkeypatch.setattr(get_settings(), "attachment_pair_quota_bytes", 6)
    ha, hb = await make_pair(client)
    assert (await upload(client, ha, data=b"12345")).status_code == 413
    f = (await upload(client, ha, data=b"1234")).json()
    assert (await upload(client, hb, data=b"123")).status_code == 413
    empty = await upload(client, ha, "empty.bin", b"")
    assert empty.status_code == 201 and empty.json()["size"] == 0
    assert (await client.delete(f"/chat/attachments/{f['id']}", headers=hb)).status_code == 404
    assert (await client.delete(f"/chat/attachments/{f['id']}", headers=ha)).status_code == 204
    assert (await upload(client, hb, data=b"123")).status_code == 201
    assert (await client.post("/chat", headers=ha, json={"attachment_ids": ["x"] * 6})).status_code == 422


async def test_chunked_oversize_is_bounded(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "attachment_max_bytes", 4)
    ha, _ = await make_pair(client)

    async def body():
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="x.bin"\r\n\r\n'
        yield b"x" * (70 * 1024)
        yield b"\r\n--b--\r\n"

    r = await client.post("/chat/attachments", headers={**ha, "Content-Type": "multipart/form-data; boundary=b"}, content=body())
    assert r.status_code == 413, r.text
    assert not list(Path(get_settings().uploads_dir).glob("*.bin"))


async def test_expiration_rollback_and_orphan_cleanup(client):
    ha, _ = await make_pair(client)
    f = (await upload(client, ha)).json()
    row = await file_row(f["id"])
    disk = Path(get_settings().uploads_dir) / (row.storage_key + ".bin")
    async with SessionLocal() as s:
        await delete_attachments(s, ChatAttachment.id == f["id"])
        await s.rollback()
    assert disk.exists() and (await client.get(f"/chat/attachments/{f['id']}", headers=ha)).status_code == 200
    async with SessionLocal() as s:
        row = await s.get(ChatAttachment, f["id"])
        row.created_at = utcnow() - timedelta(hours=25)
        await s.commit()
    assert (await client.get(f"/chat/attachments/{f['id']}", headers=ha)).status_code == 404
    assert (await client.post("/chat", headers=ha, json={"attachment_ids": [f["id"]]})).status_code == 404
    async with SessionLocal() as s:
        await cleanup_attachments(s)
    assert not disk.exists() and await file_row(f["id"]) is None


@pytest.mark.parametrize("action", ["leave", "account"])
async def test_pair_erasure_removes_pending_and_sent_files(client, action):
    ha, hb = await make_pair(client)
    sent = (await upload(client, ha)).json()
    pending = (await upload(client, hb)).json()
    assert (await client.post("/chat", headers=ha, json={"attachment_ids": [sent["id"]]})).status_code == 201
    assert len(list(Path(get_settings().uploads_dir).glob("*.bin"))) == 2
    r = await client.post("/pairs/leave", headers=ha) if action == "leave" else await client.delete("/auth/me", headers=ha)
    assert r.status_code == 200, r.text
    assert not list(Path(get_settings().uploads_dir).glob("*.bin"))
    assert await file_row(sent["id"]) is None and await file_row(pending["id"]) is None


async def test_symlink_and_tampered_file_are_not_served(client, tmp_path):
    ha, _ = await make_pair(client)
    f = (await upload(client, ha)).json()
    row = await file_row(f["id"])
    disk = Path(get_settings().uploads_dir) / (row.storage_key + ".bin")
    original = disk.read_bytes()
    disk.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    assert (await client.get(f"/chat/attachments/{f['id']}", headers=ha)).status_code == 404
    disk.unlink()
    other = tmp_path / "outside.txt"
    other.write_bytes(b"must not be served")
    disk.symlink_to(other)
    assert (await client.get(f"/chat/attachments/{f['id']}", headers=ha)).status_code == 404
    assert (await client.delete(f"/chat/attachments/{f['id']}", headers=ha)).status_code == 204
    assert other.read_bytes() == b"must not be served"


async def test_parallel_quota_and_send_idempotency(client, monkeypatch):
    ha, hb = await make_pair(client)
    monkeypatch.setattr(get_settings(), "attachment_pair_quota_bytes", 6)
    results = await asyncio.gather(upload(client, ha, data=b"1234"), upload(client, hb, data=b"1234"))
    assert sorted(r.status_code for r in results) == [201, 413]
    index = next(i for i, r in enumerate(results) if r.status_code == 201)
    h = (ha, hb)[index]
    f = results[index].json()
    body = {"attachment_ids": [f["id"]], "client_message_id": "same-attempt"}
    results = await asyncio.gather(*[client.post("/chat", headers=h, json=body) for _ in range(2)])
    assert [r.status_code for r in results] == [201, 201]
    assert results[0].json()["id"] == results[1].json()["id"]
    async with SessionLocal() as s:
        assert (await s.execute(select(func.count(ChatMessage.id)))).scalar_one() == 1


async def test_cancel_during_write_removes_uncommitted_file(client, monkeypatch):
    ha, _ = await make_pair(client)
    started, release = threading.Event(), threading.Event()
    original = attachments._write

    def delayed(key, data):
        started.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test did not release writer")
        original(key, data)

    monkeypatch.setattr(attachments, "_write", delayed)
    task = asyncio.create_task(upload(client, ha))
    try:
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(0.01)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert not list(Path(get_settings().uploads_dir).glob("*.bin"))
    async with SessionLocal() as s:
        assert (await s.execute(select(func.count(ChatAttachment.id)))).scalar_one() == 0


async def test_uncertain_commit_does_not_delete_accepted_file(client, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    ha, _ = await make_pair(client)
    original = AsyncSession.commit
    fired = False

    async def uncertain(self):
        nonlocal fired
        await original(self)
        if not fired and any(isinstance(row, ChatAttachment) for row in self.identity_map.values()):
            fired = True
            raise RuntimeError("simulated lost commit acknowledgement")

    monkeypatch.setattr(AsyncSession, "commit", uncertain)
    with pytest.raises(RuntimeError, match="lost commit"):
        await upload(client, ha, data=b"accepted bytes")
    async with SessionLocal() as s:
        row = (await s.execute(select(ChatAttachment))).scalar_one()
    assert (await client.get(f"/chat/attachments/{row.id}", headers=ha)).content == b"accepted bytes"
