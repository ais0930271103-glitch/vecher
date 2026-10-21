"""«Выбрать конкретное»: подборка мест к плану с учётом памятки, голосование вдвоём."""
from app import push
from conftest import make_pair

DINNER = "Ужин в ресторане, куда мы ещё не ходили"


async def match_card(client, ha, hb, text: str) -> int:
    cards = (await client.get("/wishes/decks/dates/next?limit=50", headers=ha)).json()
    cid = next(c["id"] for c in cards if c["text"] == text)
    for h in (ha, hb):
        await client.post("/wishes/swipe", headers=h, json={"card_id": cid, "choice": "yes"})
    return cid


async def test_places_for_plan(client):
    push.outbox.clear()
    ha, hb = await make_pair(client)
    await client.put("/auth/me", headers=hb, json={"display_name": "Аня"})
    await client.put("/me/city", headers=ha, json={"home_city": "Казань"})
    cid = await match_card(client, ha, hb, DINNER)

    p = (await client.get(f"/plans/{cid}", headers=ha)).json()
    assert p["places_available"] is True
    unopened = await client.get(f"/plans/{cid}/places", headers=ha)
    assert unopened.status_code == 403  # явное открытие плана, не требование оплаты
    assert unopened.json()["detail"] == "Сначала откройте план"
    await client.post(f"/plans/{cid}/open", headers=ha)

    # У Ани аллергия на рыбу и любимая еда — паста.
    await client.put("/person/me", headers=hb, json={"key": "allergies", "value": "Рыба и морепродукты"})
    await client.put("/person/me", headers=hb, json={"key": "food", "value": "Паста карбонара"})
    r = (await client.get(f"/plans/{cid}/places", headers=ha)).json()
    titles = [x["title"] for x in r["places"]]
    assert r["cities"] == ["Казань"] and r["hidden"] == 1
    assert all("Рыбный" not in t for t in titles)                    # аллергия партнёра учтена
    assert "Итальянская" in titles[0] and r["places"][0]["reasons"] == ["Аня любит: паста карбонара"]
    assert all(x["kind"] == "restaurant" for x in r["places"]) and all(x["demo"] for x in r["places"])

    pid = r["places"][0]["id"]
    r = (await client.post(f"/plans/{cid}/places/{pid}/vote", headers=ha)).json()
    assert r["places"][0]["mine"] is True and r["chosen"] == []
    rb = (await client.get(f"/plans/{cid}/places", headers=hb)).json()
    assert next(x for x in rb["places"] if x["id"] == pid)["partner"] is True  # видно, что отметил партнёр
    rb = (await client.post(f"/plans/{cid}/places/{pid}/vote", headers=hb)).json()
    assert rb["chosen"] == [pid]
    assert any("Совпало место" in body for _, _, body, _ in push.outbox)
    # Снять отметку — место больше не выбрано.
    assert (await client.post(f"/plans/{cid}/places/{pid}/vote", headers=hb)).json()["chosen"] == []
    assert (await client.post(f"/plans/{cid}/places/99999/vote", headers=hb)).status_code == 404


async def test_no_places_for_home_plans(client):
    ha, hb = await make_pair(client)
    cards = (await client.get("/wishes/decks/home/next", headers=ha)).json()
    cid = cards[0]["id"]
    for h in (ha, hb):
        await client.post("/wishes/swipe", headers=h, json={"card_id": cid, "choice": "yes"})
    assert (await client.get(f"/plans/{cid}", headers=ha)).json()["places_available"] is False
