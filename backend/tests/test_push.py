"""Push: регистрация устройства, уведомления о событиях, фоновые уведомления по времени."""
from datetime import date, datetime, timedelta, timezone

from app import push
from app.db import SessionLocal
from app.scheduler import tick
from conftest import login, make_pair

MSK = timezone(timedelta(hours=3))


async def ids(client, ha, hb):
    a = (await client.get("/auth/me", headers=ha)).json()["id"]
    b = (await client.get("/auth/me", headers=hb)).json()["id"]
    return a, b


def bodies(user_id):
    return [body for uid, _, body, _ in push.outbox if uid == user_id]


async def test_register_device(client):
    ha, hb = await make_pair(client)
    token = "fcm-token-0123456789"
    assert (await client.post("/push/register", headers=ha, json={"token": token})).status_code == 200
    # Тот же телефон перешёл к другому человеку — токен переносится, а не дублируется.
    assert (await client.post("/push/register", headers=hb, json={"token": token})).status_code == 200
    assert (await client.post("/push/register", headers=hb, json={"token": token, "platform": "pager"})).status_code == 422
    assert (await client.post("/push/unregister", headers=hb, json={"token": token})).status_code == 200


async def test_events_notify_partner_without_private_text(client):
    push.outbox.clear()
    ha, hb = await make_pair(client)
    await client.put("/auth/me", headers=ha, json={"display_name": "Аня"})
    a, b = await ids(client, ha, hb)

    await client.post("/today/answer", headers=ha, json={"text": "очень личный ответ"})
    await client.post("/radar", headers=ha, json={"kind": "miss"})
    card = (await client.get("/wishes/decks/romance/next", headers=ha)).json()[0]
    await client.post("/wishes/swipe", headers=ha, json={"card_id": card["id"], "choice": "yes"})
    await client.post("/wishes/swipe", headers=hb, json={"card_id": card["id"], "choice": "yes"})

    got_b = bodies(b)
    assert "Аня ответил(а) на вопрос дня" in got_b
    assert "Аня: «Скучаю»" in got_b
    assert all("личный" not in t for t in got_b)          # текст ответа не уходит в уведомление
    assert "У вас новое совпадение" in bodies(a)            # тот, кто ответил раньше
    assert all(card["text"] not in t for t in bodies(a))   # текст карточки не раскрывается


async def test_scheduler_delivers_once(client):
    push.outbox.clear()
    ha, hb = await make_pair(client)
    a, b = await ids(client, ha, hb)
    now = datetime.now(MSK)
    await client.post("/letters", headers=ha, json={"text": "утро", "deliver_at": (now - timedelta(minutes=1)).isoformat()})
    await client.post("/letters", headers=ha, json={"text": "позже", "deliver_at": (now + timedelta(hours=3)).isoformat()})
    await client.post("/reminders", headers=ha, json={
        "text": "Купить хлеб", "target": "both", "due_at": (now - timedelta(minutes=2)).isoformat()})

    async with SessionLocal() as s:
        await tick(s)
        await tick(s)  # повторный проход не дублирует

    assert bodies(b).count("Партнёр оставил(а) тебе письмо") == 1
    assert bodies(a).count("Купить хлеб") == 1 and bodies(b).count("Купить хлеб") == 1


async def test_important_dates_reminders(client):
    push.outbox.clear()
    ha, hb = await make_pair(client)
    a, _ = await ids(client, ha, hb)
    today = datetime.now(MSK).date()
    in7 = today + timedelta(days=7)
    await client.post("/dates", headers=ha, json={"title": "ДР Ани", "day": date(2000, in7.month, in7.day).isoformat(),
                                                   "kind": "birthday"})
    morning = datetime.combine(today, datetime.min.time(), MSK).replace(hour=9)
    late = morning.replace(hour=11)
    async with SessionLocal() as s:
        await tick(s, morning.astimezone(timezone.utc))   # до 10:00 — рано
        assert not [t for t in bodies(a) if "ДР Ани" in t]
        await tick(s, late.astimezone(timezone.utc))
        await tick(s, late.astimezone(timezone.utc))
    assert bodies(a).count("Через 7 дней: ДР Ани") == 1


async def test_push_test_endpoint_without_key(client):
    """Без ключа Firebase проверка честно отвечает: firebase=None, устройств — сколько зарегистрировано."""
    h = await login(client, "tester@example.com")
    r = await client.post("/push/test", headers=h)
    assert r.status_code == 200
    assert r.json() == {"firebase": None, "devices": 0, "delay": 5}
    await client.post("/push/register", json={"token": "x" * 20}, headers=h)
    assert (await client.post("/push/test?delay=99", headers=h)).json() == {"firebase": None, "devices": 1, "delay": 30}
