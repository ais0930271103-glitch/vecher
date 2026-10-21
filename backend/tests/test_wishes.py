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


async def test_levels_by_more_careful_partner(client):
    from app import push
    from app.content.wishes import SPICY_CARDS
    push.outbox.clear()
    ha, hb = await make_pair(client)
    d = (await client.get("/wishes/decks", headers=ha)).json()
    assert d["level"] == {"mine": "warm", "effective": "warm", "night_open": False}
    romance = next(x for x in d["decks"] if x["id"] == "romance")
    assert romance["total"] == 15 and romance["locked"] == sum(1 for dk, _ in SPICY_CARDS if dk == "romance")
    spicy_text = next(t for dk, t in SPICY_CARDS if dk == "romance")
    texts = {c["text"] for c in (await client.get("/wishes/decks/romance/next?limit=50", headers=ha)).json()}
    assert spicy_text not in texts

    # Я выбрал «Остро», партнёр — нет: паре всё ещё «Тепло», и выбор партнёра мне не раскрывается.
    assert (await client.put("/wishes/level", headers=ha, json={"level": "wild"})).status_code == 422
    r = (await client.put("/wishes/level", headers=ha, json={"level": "spicy"})).json()
    assert r == {"mine": "spicy", "effective": "warm", "night_open": False}
    assert (await client.get("/wishes/decks", headers=hb)).json()["level"]["mine"] == "warm"

    await client.put("/wishes/level", headers=hb, json={"level": "spicy"})
    cards = (await client.get("/wishes/decks/romance/next?limit=50", headers=ha)).json()
    spicy = next(c for c in cards if c["text"] == spicy_text)
    assert (await client.get("/wishes/decks", headers=ha)).json()["level"]["effective"] == "spicy"

    # Партнёр вернулся на «Тепло» — острые карточки снова скрыты, свайпнуть их нельзя.
    await client.put("/wishes/level", headers=hb, json={"level": "warm"})
    assert (await client.post("/wishes/swipe", headers=ha, json={"card_id": spicy["id"], "choice": "yes"})).status_code == 403

    # «Жарко» у обоих — только сигнал интереса; контент будущего раздела 18+ не открывается.
    await client.put("/wishes/level", headers=ha, json={"level": "hot"})
    r = (await client.put("/wishes/level", headers=hb, json={"level": "hot"})).json()
    assert r["effective"] == "hot" and r["night_open"] is True
    d = (await client.get("/wishes/decks", headers=ha)).json()
    assert all(x["locked"] == 0 for x in d["decks"])  # «Остро» открыто целиком, «Жарко» здесь не считается
    assert sum(1 for _, _, body, _ in push.outbox if body == "Настройки для вас двоих обновлены") == 2
