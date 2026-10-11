"""Этап 3: свайпер желаний — приватность и совпадения."""
from datetime import date

from app.deps import today_date
from app.main import app
from conftest import login, make_pair


def set_day(d: date) -> None:
    app.dependency_overrides[today_date] = lambda: d


async def first_card(client, h, deck="dates") -> dict:
    return (await client.get(f"/wishes/decks/{deck}/next", headers=h)).json()[0]


async def test_wishes_require_active_pair(client):
    h = await login(client, "solo@example.ru")
    assert (await client.get("/wishes/decks", headers=h)).status_code == 409


async def test_decks_and_progress(client):
    ha, _ = await make_pair(client)
    d = (await client.get("/wishes/decks", headers=ha)).json()
    assert [x["id"] for x in d["decks"]] == ["dates", "home", "travel", "romance"]
    assert all(x["total"] > 0 and x["done"] == 0 for x in d["decks"])
    assert (d["matches"], d["new_matches"]) == (0, 0)

    card = await first_card(client, ha)
    r = await client.post("/wishes/swipe", headers=ha, json={"card_id": card["id"], "choice": "no"})
    assert r.status_code == 200 and r.json()["match"] is None
    d = (await client.get("/wishes/decks", headers=ha)).json()
    assert d["decks"][0]["done"] == 1
    # Пролистанная карточка больше не предлагается.
    nxt = (await client.get("/wishes/decks/dates/next", headers=ha)).json()
    assert card["id"] not in [c["id"] for c in nxt]


async def test_validation(client):
    ha, _ = await make_pair(client)
    assert (await client.get("/wishes/decks/nope/next", headers=ha)).status_code == 404
    card = await first_card(client, ha)
    r = await client.post("/wishes/swipe", headers=ha, json={"card_id": card["id"], "choice": "love"})
    assert r.status_code == 422
    r = await client.post("/wishes/swipe", headers=ha, json={"card_id": 999999, "choice": "yes"})
    assert r.status_code == 404


async def test_mutual_yes_is_a_match_one_sided_yes_is_invisible(client):
    ha, hb = await make_pair(client)
    cards = (await client.get("/wishes/decks/dates/next", headers=ha)).json()
    c1, c2, c3 = cards[0]["id"], cards[1]["id"], cards[2]["id"]

    # c1: оба «да» → совпадение видит тот, кто ответил вторым, и оба в списке.
    r = await client.post("/wishes/swipe", headers=ha, json={"card_id": c1, "choice": "yes"})
    assert r.json()["match"] is None
    r = await client.post("/wishes/swipe", headers=hb, json={"card_id": c1, "choice": "yes"})
    assert r.json()["match"]["kind"] == "yes"

    # c2: «да» + «нет» → ничего, и «да» партнёра не раскрывается.
    await client.post("/wishes/swipe", headers=ha, json={"card_id": c2, "choice": "yes"})
    r = await client.post("/wishes/swipe", headers=hb, json={"card_id": c2, "choice": "no"})
    assert r.json()["match"] is None

    # c3: «да» + «может быть» → можно обсудить.
    await client.post("/wishes/swipe", headers=ha, json={"card_id": c3, "choice": "maybe"})
    r = await client.post("/wishes/swipe", headers=hb, json={"card_id": c3, "choice": "yes"})
    assert r.json()["match"]["kind"] == "maybe"

    for h in (ha, hb):
        m = (await client.get("/wishes/matches", headers=h)).json()
        assert {x["card_id"]: x["kind"] for x in m} == {c1: "yes", c3: "maybe"}


async def test_new_matches_flag_clears_after_viewing(client):
    ha, hb = await make_pair(client)
    card = await first_card(client, ha)
    await client.post("/wishes/swipe", headers=ha, json={"card_id": card["id"], "choice": "yes"})
    await client.post("/wishes/swipe", headers=hb, json={"card_id": card["id"], "choice": "yes"})

    d = (await client.get("/wishes/decks", headers=ha)).json()
    assert (d["matches"], d["new_matches"]) == (1, 1)
    m = (await client.get("/wishes/matches", headers=ha)).json()
    assert m[0]["new"] is True
    d = (await client.get("/wishes/decks", headers=ha)).json()
    assert (d["matches"], d["new_matches"]) == (1, 0)
    # У партнёра пометка своя.
    assert (await client.get("/wishes/decks", headers=hb)).json()["new_matches"] == 1


async def test_partner_swiped_cards_come_first(client):
    ha, hb = await make_pair(client)
    cards = (await client.get("/wishes/decks/dates/next", headers=ha)).json()
    last = cards[-1]["id"]
    await client.post("/wishes/swipe", headers=ha, json={"card_id": last, "choice": "no"})
    assert (await first_card(client, hb))["id"] == last


async def test_swipe_counts_for_streak(client):
    set_day(date(2026, 10, 12))
    ha, hb = await make_pair(client)
    card = await first_card(client, ha)
    await client.post("/wishes/swipe", headers=ha, json={"card_id": card["id"], "choice": "maybe"})
    await client.post("/wishes/swipe", headers=hb, json={"card_id": card["id"], "choice": "no"})
    t = (await client.get("/today", headers=ha)).json()
    assert (t["streak"], t["today_complete"]) == (1, True)
