"""Планы к совпадениям: доступ только к совпадениям, первый бесплатно, выбор вдвоём, расписание."""
from datetime import datetime, timedelta, timezone

from conftest import make_pair


async def make_matches(client, ha, hb, n=2) -> list[int]:
    cards = (await client.get("/wishes/decks/home/next", headers=ha)).json()[:n]
    ids = [c["id"] for c in cards]
    for cid in ids:
        await client.post("/wishes/swipe", headers=ha, json={"card_id": cid, "choice": "yes"})
        await client.post("/wishes/swipe", headers=hb, json={"card_id": cid, "choice": "yes"})
    return ids


async def test_plan_only_for_matches(client):
    ha, hb = await make_pair(client)
    card = (await client.get("/wishes/decks/home/next", headers=ha)).json()[0]["id"]
    await client.post("/wishes/swipe", headers=ha, json={"card_id": card, "choice": "yes"})
    # Совпадения ещё нет — план недоступен (и не раскрывает выбор партнёра).
    assert (await client.get(f"/plans/{card}", headers=ha)).status_code == 404
    assert (await client.get(f"/plans/{card}", headers=hb)).status_code == 404


async def test_locked_preview_then_first_plan_free_second_needs_subscription(client):
    ha, hb = await make_pair(client)
    c1, c2 = await make_matches(client, ha, hb)

    p = (await client.get(f"/plans/{c1}", headers=ha)).json()
    assert p["locked"] is True and p["free_plans_left"] == 1
    assert len(p["options"]) >= 2 and p["options"][0]["items"] == []  # превью без содержимого

    p = (await client.post(f"/plans/{c1}/open", headers=ha)).json()
    assert p["locked"] is False and p["free_plans_left"] == 0
    assert p["options"][0]["items"] or p["options"][0]["steps"]

    # Открытый план открыт и партнёру — одна пара, один план.
    assert (await client.get(f"/plans/{c1}", headers=hb)).json()["locked"] is False

    r = await client.post(f"/plans/{c2}/open", headers=hb)
    assert r.status_code == 402

    sub = (await client.post("/subscription/dev-activate", headers=hb)).json()
    assert sub["premium"] is True
    # Подписка общая: открывает и второй партнёр.
    assert (await client.get("/subscription", headers=ha)).json()["premium"] is True
    assert (await client.post(f"/plans/{c2}/open", headers=ha)).json()["locked"] is False


async def test_vote_needs_unlock_and_chosen_when_both_agree(client):
    ha, hb = await make_pair(client)
    (c1,) = await make_matches(client, ha, hb, n=1)

    assert (await client.post(f"/plans/{c1}/vote", headers=ha, json={"option": 0})).status_code == 403
    await client.post(f"/plans/{c1}/open", headers=ha)
    assert (await client.post(f"/plans/{c1}/vote", headers=ha, json={"option": 99})).status_code == 422

    p = (await client.post(f"/plans/{c1}/vote", headers=ha, json={"option": 0})).json()
    assert (p["my_vote"], p["partner_vote"], p["chosen"]) == (0, None, None)
    p = (await client.post(f"/plans/{c1}/vote", headers=hb, json={"option": 1})).json()
    assert (p["my_vote"], p["partner_vote"], p["chosen"]) == (1, 0, None)
    p = (await client.post(f"/plans/{c1}/vote", headers=hb, json={"option": 0})).json()
    assert p["chosen"] == 0


async def test_schedule_and_upcoming(client):
    ha, hb = await make_pair(client)
    (c1,) = await make_matches(client, ha, hb, n=1)
    await client.post(f"/plans/{c1}/open", headers=ha)

    msk = timezone(timedelta(hours=3))
    at = (datetime.now(msk) + timedelta(days=2)).replace(hour=19, minute=0, second=0, microsecond=0)
    p = (await client.post(f"/plans/{c1}/schedule", headers=hb, json={"at": at.isoformat()})).json()
    assert datetime.fromisoformat(p["scheduled_at"]) == at

    up = (await client.get("/plans/upcoming", headers=ha)).json()
    assert [u["card_id"] for u in up] == [c1]
    assert datetime.fromisoformat(up[0]["scheduled_at"]) == at

    m = (await client.get("/wishes/matches", headers=ha)).json()
    assert m[0]["plan_open"] is True and m[0]["scheduled_at"] is not None

    await client.post(f"/plans/{c1}/schedule", headers=ha, json={"at": None})
    assert (await client.get("/plans/upcoming", headers=ha)).json() == []
