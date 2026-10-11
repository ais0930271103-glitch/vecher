"""Чат пары: сообщения, «Обсудить», ответы, реакции, непрочитанные, удаление, шифрование."""
from sqlalchemy import Text, select, type_coerce

from app import crypto, push
from app.db import SessionLocal
from app.models import ChatMessage
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
