"""Чат пары: сообщения, «Обсудить», ответы, реакции, непрочитанные, удаление, шифрование."""
from sqlalchemy import Text, select, type_coerce

from app import crypto, push
from app.db import SessionLocal
from app.models import ChatAttachment, ChatMessage, ChatSendKey, DeliveryOutbox
from conftest import login, make_pair


async def test_chat_flow(client):
    push.outbox.clear()
    ha, hb = await make_pair(client)
    await client.put("/auth/me", headers=ha, json={"display_name": "Аня"})
    b = (await client.get("/auth/me", headers=hb)).json()["id"]

    assert (await client.post("/chat", headers=ha, json={"text": "   "})).status_code == 422
    m1 = (await client.post("/chat", headers=ha, json={"text": "Привет! Скучаю"})).json()
    assert m1["mine"] is True
    m2 = (await client.post("/chat", headers=ha, json={
        "text": "Как тебе?", "ref_kind": "plan", "ref_id": "12", "ref_title": "Ужин при свечах"})).json()
    assert m2["ref_title"] == "Ужин при свечах"
    assert (await client.post("/chat", headers=ha, json={"ref_kind": "spam"})).status_code == 422
    # Уведомление — одно на серию и без текста сообщения.
    got = [body for u, _, body, _ in push.outbox if u == b]
    assert got and set(got) == {"Аня написал(а) тебе"} and len(got) <= 2  # не на каждое сообщение

    assert (await client.get("/chat/unread", headers=hb)).json() == {"count": 2}
    chat = (await client.get("/chat", headers=hb)).json()
    assert [m["text"] for m in chat["messages"]] == ["Привет! Скучаю", "Как тебе?"]
    assert all(m["mine"] is False for m in chat["messages"])

    reply = (await client.post("/chat", headers=hb, json={"text": "Я тоже!", "reply_to": m1["id"]})).json()
    assert reply["reply_text"] == "Привет! Скучаю"
    assert (await client.get("/chat/unread", headers=hb)).json() == {"count": 0}  # ответил — значит прочитал
    assert (await client.get("/chat", headers=ha)).json()["partner_read_at"] is not None

    assert (await client.post(f"/chat/{m1['id']}/react", headers=ha, json={"emoji": "❤️"})).status_code == 403
    assert (await client.post(f"/chat/{m1['id']}/react", headers=hb, json={"emoji": "🍕"})).status_code == 422
    assert (await client.post(f"/chat/{m1['id']}/react", headers=hb, json={"emoji": "❤️"})).json()["reaction"] == "❤️"

    assert (await client.delete(f"/chat/{m1['id']}", headers=hb)).status_code == 403
    gone = (await client.delete(f"/chat/{m1['id']}", headers=ha)).json()
    assert gone["deleted"] is True and gone["text"] is None
    texts = [m["reply_text"] for m in (await client.get("/chat", headers=ha)).json()["messages"] if m["reply_to"]]
    assert texts == ["Сообщение удалено"]

    async with SessionLocal() as s:
        raw = (await s.execute(select(type_coerce(ChatMessage.text, Text)).where(ChatMessage.text.is_not(None)))).scalars().all()
    assert raw and all(v.startswith(crypto.PREFIX) and "тоже" not in v for v in raw)

    # Посторонний чат пары не видит.
    hc = await login(client, "stranger@example.ru")
    assert (await client.get("/chat", headers=hc)).status_code == 409


async def test_chat_paging(client):
    ha, hb = await make_pair(client)
    for i in range(5):
        await client.post("/chat", headers=ha, json={"text": f"m{i}"})
    page = (await client.get("/chat?limit=3", headers=hb)).json()
    assert [m["text"] for m in page["messages"]] == ["m2", "m3", "m4"] and page["has_more"] is True
    older = (await client.get("/chat", headers=hb, params={"limit": 3, "before": page["messages"][0]["created_at"]})).json()
    assert [m["text"] for m in older["messages"]] == ["m0", "m1"] and older["has_more"] is False


async def test_confirmed_pair_send_and_legacy_clients_remain_compatible(client):
    ha, hb = await make_pair(client)
    pair_id = (await client.get("/pairs/me", headers=ha)).json()["pair_id"]
    body = {"text": "Для нашей пары", "client_message_id": "confirmed-draft",
            "expected_pair_id": pair_id}
    sent = await client.post("/chat", headers=ha, json=body)
    assert sent.status_code == 201, sent.text
    assert (await client.post("/chat", headers=ha, json=body)).json()["id"] == sent.json()["id"]
    # The recipient guard runs before returning a cached idempotent result.
    conflict = await client.post("/chat", headers=ha, json={**body, "expected_pair_id": "old-pair"})
    assert conflict.status_code == 409 and conflict.json()["detail"] == "pair_changed"
    for legacy in ({"text": "Старый APK"}, {"text": "Без проверки", "expected_pair_id": None}):
        assert (await client.post("/chat", headers=ha, json=legacy)).status_code == 201
    assert len((await client.get("/chat", headers=hb)).json()["messages"]) == 3


async def test_old_pair_draft_cannot_send_to_new_partner_or_consume_attachment(client):
    ha, _ = await make_pair(client)
    old_pair_id = (await client.get("/pairs/me", headers=ha)).json()["pair_id"]
    assert (await client.post("/pairs/leave", headers=ha)).status_code == 200
    hc = await login(client, "new-partner@example.ru")
    invitation = (await client.post("/pairs/invite", headers=ha)).json()
    assert (await client.post("/pairs/join", headers=hc,
                              json={"code": invitation["invite_code"]})).status_code == 200
    new_pair_id = (await client.get("/pairs/me", headers=ha)).json()["pair_id"]
    assert new_pair_id != old_pair_id
    upload = await client.post("/chat/attachments", headers=ha,
                               files={"file": ("draft.txt", b"private draft", "text/plain")})
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["id"]
    async with SessionLocal() as session:
        job_ids_before = set((await session.execute(select(DeliveryOutbox.id))).scalars())
    push.outbox.clear()
    stale = {"text": "Сообщение для прежней пары", "client_message_id": "old-recipient-draft",
             "attachment_ids": [file_id], "expected_pair_id": old_pair_id}
    rejected = await client.post("/chat", headers=ha, json=stale)
    assert rejected.status_code == 409 and rejected.json()["detail"] == "pair_changed"
    assert (await client.get("/chat", headers=hc)).json()["messages"] == []
    async with SessionLocal() as session:
        assert (await session.get(ChatAttachment, file_id)).message_id is None
        assert not (await session.execute(select(ChatSendKey))).first()
        assert set((await session.execute(select(DeliveryOutbox.id))).scalars()) == job_ids_before
    assert push.outbox == []
    # After an explicit new composition for the confirmed recipient, the file is
    # still available; rejecting the stale draft did not consume it.
    current = {**stale, "text": "Новое сообщение", "expected_pair_id": new_pair_id}
    accepted = await client.post("/chat", headers=ha, json=current)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["attachments"][0]["id"] == file_id
