"""Вкладка «Я»: письма с отложенной доставкой, тихие желания, сюрпризы."""
from datetime import datetime, timedelta, timezone

from app.routers.solo import next_morning
from conftest import make_pair

MSK = timezone(timedelta(hours=3))


def iso(dt: datetime) -> str:
    return dt.isoformat()


def test_next_morning_is_8am_moscow():
    before = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)   # 7:00 МСК → сегодня в 8:00
    after = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)    # 9:00 МСК → завтра в 8:00
    assert next_morning(before) == datetime(2026, 10, 12, 8, 0, tzinfo=MSK)
    assert next_morning(after) == datetime(2026, 10, 13, 8, 0, tzinfo=MSK)


async def test_letter_is_hidden_until_delivery(client):
    ha, hb = await make_pair(client)
    now = datetime.now(MSK)
    future = (await client.post("/letters", headers=ha, json={
        "text": "Доброе утро!", "deliver_at": iso(now + timedelta(hours=5))})).json()
    past = (await client.post("/letters", headers=ha, json={
        "text": "Уже пришло", "deliver_at": iso(now - timedelta(minutes=1))})).json()
    assert future["delivered"] is False and past["delivered"] is True

    inbox = (await client.get("/letters/inbox", headers=hb)).json()
    assert [l["text"] for l in inbox] == ["Уже пришло"]
    assert (await client.get("/letters/inbox", headers=ha)).json() == []  # свои — не во входящих

    # Будущее письмо нельзя прочитать заранее; доставленное отмечается прочитанным.
    assert (await client.post(f"/letters/{future['id']}/read", headers=hb)).status_code == 404
    assert (await client.post(f"/letters/{past['id']}/read", headers=hb)).status_code == 200
    assert (await client.get("/letters/inbox", headers=hb)).json()[0]["read"] is True

    # Отменить можно только недоставленное.
    assert (await client.delete(f"/letters/{past['id']}", headers=ha)).status_code == 409
    assert (await client.delete(f"/letters/{future['id']}", headers=ha)).status_code == 204

    # По умолчанию — к утру.
    default = (await client.post("/letters", headers=ha, json={"text": "К утру"})).json()
    assert datetime.fromisoformat(default["deliver_at"]).astimezone(MSK).hour == 8


async def test_quiet_wishes_are_private(client):
    ha, hb = await make_pair(client)
    w = (await client.post("/solo/wishes", headers=ha, json={"text": "Научиться танцевать сальсу"})).json()
    assert [x["text"] for x in (await client.get("/solo", headers=ha)).json()["wishes"]] == ["Научиться танцевать сальсу"]
    assert (await client.get("/solo", headers=hb)).json()["wishes"] == []
    assert (await client.delete(f"/solo/wishes/{w['id']}", headers=hb)).status_code == 404
    assert (await client.delete(f"/solo/wishes/{w['id']}", headers=ha)).status_code == 204


async def test_surprise_teaser_hides_details_until_time(client):
    ha, hb = await make_pair(client)
    now = datetime.now(MSK)
    await client.post("/surprises", headers=ha, json={
        "title": "Ужин на крыше", "note": "Оденься тепло", "at": iso(now + timedelta(days=2))})
    await client.post("/surprises", headers=ha, json={"title": "Цветы", "at": iso(now - timedelta(minutes=5))})

    inc = (await client.get("/surprises/incoming", headers=hb)).json()
    assert [(s["revealed"], s["title"]) for s in inc] == [(True, "Цветы"), (False, None)]
    assert inc[1]["note"] is None
    assert (await client.get("/surprises/incoming", headers=ha)).json() == []
    mine = (await client.get("/solo", headers=ha)).json()["surprises"]
    assert {s["title"] for s in mine} == {"Ужин на крыше", "Цветы"}


async def test_solo_progress(client):
    ha, hb = await make_pair(client)
    s = (await client.get("/solo", headers=ha)).json()
    assert s["swiped"] == 0 and s["remaining"] > 0 and s["partner_active_today"] is False
    card = (await client.get("/wishes/decks/dates/next", headers=ha)).json()[0]["id"]
    await client.post("/wishes/swipe", headers=ha, json={"card_id": card, "choice": "yes"})
    await client.post("/wishes/swipe", headers=hb, json={"card_id": card, "choice": "no"})
    s2 = (await client.get("/solo", headers=ha)).json()
    assert s2["swiped"] == 1 and s2["remaining"] == s["remaining"] - 1
    assert s2["partner_active_today"] is True
