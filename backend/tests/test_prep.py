"""«Подготовь „Вечер“ для вас двоих»: пока партнёр не пришёл, можно листать, отвечать, рассказать о себе, написать письмо."""
from datetime import datetime, timedelta, timezone

from app import push
from conftest import login


async def test_prepare_before_partner_joins(client):
    push.outbox.clear()
    ha = await login(client, "first@example.ru")
    await client.put("/auth/me", headers=ha, json={"display_name": "Аня"})
    code = (await client.post("/pairs/invite", headers=ha)).json()["invite_code"]
    assert (await client.get("/pairs/prep", headers=ha)).json()["done"] == 0

    cards = (await client.get("/wishes/decks/dates/next", headers=ha)).json()
    for c in cards[:5]:
        r = await client.post("/wishes/swipe", headers=ha, json={"card_id": c["id"], "choice": "yes"})
        assert r.status_code == 200 and r.json()["match"] is None
    for key, value in (("flowers", "пионы"), ("food", "паста"), ("music", "джаз")):
        assert (await client.put("/person/me", headers=ha, json={"key": key, "value": value})).status_code == 200
    assert (await client.post("/today/answer", headers=ha, json={"text": "Море"})).status_code == 200
    soon = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert (await client.post("/letters", headers=ha, json={"text": "Добро пожаловать!", "deliver_at": soon})).status_code == 201
    prep = (await client.get("/pairs/prep", headers=ha)).json()
    assert prep == {"swiped": 5, "about_me": 3, "answered": True, "letter": True, "done": 4}
    assert push.outbox == []  # пока партнёра нет — уведомлять некого

    # Партнёр присоединился — всё на месте, а «да» Ани по-прежнему не раскрыто.
    hb = await login(client, "second@example.ru")
    assert (await client.post("/pairs/join", headers=hb, json={"code": code})).status_code == 200
    t = (await client.get("/today", headers=hb)).json()
    assert t["partner_answered"] is True and t["partner_answer"] is None
    assert t["partner_swiped"] == 5 and t["my_swiped"] == 0
    assert [l["text"] for l in (await client.get("/letters/inbox", headers=hb)).json()] == ["Добро пожаловать!"]
    assert (await client.get("/wishes/matches", headers=hb)).json() == []
    r = await client.post("/wishes/swipe", headers=hb, json={"card_id": cards[0]["id"], "choice": "yes"})
    assert r.json()["match"] is not None  # совпадение сразу, как только партнёр тоже полистал
