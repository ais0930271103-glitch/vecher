"""Этап 2: вопрос дня, радар, огонёк."""
from datetime import date, timedelta

from app.deps import today_date
from app.main import app
from conftest import login, make_pair

DAY1 = date(2026, 10, 12)


def set_day(d: date) -> None:
    app.dependency_overrides[today_date] = lambda: d


async def test_today_requires_active_pair(client):
    h = await login(client, "solo@example.ru")
    r = await client.get("/today", headers=h)
    assert r.status_code == 409


async def test_same_question_and_answer_hidden_until_own_answer(client):
    set_day(DAY1)
    ha, hb = await make_pair(client)

    qa = (await client.get("/today", headers=ha)).json()
    qb = (await client.get("/today", headers=hb)).json()
    assert qa["question"]["id"] == qb["question"]["id"]
    assert qa["my_answer"] is None and qa["partner_answered"] is False

    r = await client.post("/today/answer", headers=ha, json={"text": "  Поездка на море  "})
    assert r.status_code == 200, r.text
    assert r.json()["my_answer"] == "Поездка на море"

    # Партнёр видит, что ответ есть, но не видит текст, пока не ответит сам.
    b_view = (await client.get("/today", headers=hb)).json()
    assert b_view["partner_answered"] is True
    assert b_view["partner_answer"] is None

    r = await client.post("/today/answer", headers=hb, json={"text": "Первое свидание"})
    assert r.json()["partner_answer"] == "Поездка на море"
    a_view = (await client.get("/today", headers=ha)).json()
    assert a_view["partner_answer"] == "Первое свидание"

    # Второй ответ в тот же день запрещён.
    r = await client.post("/today/answer", headers=ha, json={"text": "ещё раз"})
    assert r.status_code == 409


async def test_empty_answer_rejected(client):
    set_day(DAY1)
    ha, _ = await make_pair(client)
    r = await client.post("/today/answer", headers=ha, json={"text": "   "})
    assert r.status_code == 422


async def test_streak_counts_only_days_where_both_acted(client):
    ha, hb = await make_pair(client)

    async def streak():
        return (await client.get("/today", headers=ha)).json()

    # День 1: оба ответили → 1, день закрыт.
    set_day(DAY1)
    await client.post("/today/answer", headers=ha, json={"text": "a"})
    await client.post("/today/answer", headers=hb, json={"text": "b"})
    s = await streak()
    assert (s["streak"], s["today_complete"]) == (1, True)

    # День 2: оба через радар → 2.
    set_day(DAY1 + timedelta(days=1))
    await client.post("/radar", headers=ha, json={"kind": "thinking"})
    s = await streak()
    assert (s["streak"], s["today_complete"]) == (1, False)  # днём серия не сгорает
    await client.post("/radar", headers=hb, json={"kind": "miss"})
    assert (await streak())["streak"] == 2

    # День 3 пропущен, день 4: отметился только один → серия обнулилась.
    set_day(DAY1 + timedelta(days=3))
    await client.post("/radar", headers=ha, json={"kind": "hug"})
    assert (await streak())["streak"] == 0
    await client.post("/radar", headers=hb, json={"kind": "hug"})
    assert (await streak())["streak"] == 1


async def test_radar_incoming_and_seen(client):
    set_day(DAY1)
    ha, hb = await make_pair(client)

    r = await client.post("/radar", headers=ha, json={"kind": "plans"})
    assert r.status_code == 201
    assert (await client.post("/radar", headers=ha, json={"kind": "nope"})).status_code == 422

    incoming = (await client.get("/today", headers=hb)).json()["radar_incoming"]
    assert [s["label"] for s in incoming] == ["Вечером есть планы…"]
    # Отправитель свой сигнал во входящих не видит.
    assert (await client.get("/today", headers=ha)).json()["radar_incoming"] == []

    await client.post("/radar/seen", headers=hb)
    assert (await client.get("/today", headers=hb)).json()["radar_incoming"] == []


async def test_radar_daily_limit(client):
    set_day(DAY1)
    ha, _ = await make_pair(client)
    from app.config import get_settings
    limit = get_settings().radar_daily_limit
    for _ in range(limit):
        assert (await client.post("/radar", headers=ha, json={"kind": "hug"})).status_code == 201
    assert (await client.post("/radar", headers=ha, json={"kind": "hug"})).status_code == 429
