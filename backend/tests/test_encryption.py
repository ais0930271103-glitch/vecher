"""Личные данные лежат в базе зашифрованными, а приложение видит их как обычно."""
from sqlalchemy import Text, select, type_coerce

from app import crypto
from app.db import SessionLocal, engine
from app.models import DailyAnswer, ProfileFact, WishSwipe
from conftest import make_pair


async def raw(model, column):
    async with SessionLocal() as s:
        return (await s.execute(select(type_coerce(column, Text)))).scalars().all()


async def test_sensitive_values_are_encrypted_at_rest(client):
    ha, hb = await make_pair(client)
    await client.put("/person/me", headers=ha, json={"key": "flowers", "value": "пионы"})
    await client.post("/today/answer", headers=ha, json={"text": "секретный ответ"})
    card = (await client.get("/wishes/decks/romance/next", headers=ha)).json()[0]
    await client.post("/wishes/swipe", headers=ha, json={"card_id": card["id"], "choice": "yes"})
    r = await client.post("/wishes/swipe", headers=hb, json={"card_id": card["id"], "choice": "maybe"})
    assert r.json()["match"]["kind"] == "maybe"  # совпадения считаются, хотя выбор зашифрован

    for model, col, plain in [(ProfileFact, ProfileFact.value, "пионы"),
                              (DailyAnswer, DailyAnswer.text, "секретный ответ"),
                              (WishSwipe, WishSwipe.choice, "yes")]:
        values = await raw(model, col)
        assert values and all(v.startswith(crypto.PREFIX) and plain not in v for v in values)

    person = (await client.get("/person", headers=hb)).json()
    assert "пионы" in str(person)  # партнёр видит памятку как обычно


async def test_old_plaintext_is_read_and_then_encrypted():
    assert crypto.decrypt("старое значение") == "старое значение"
    token = crypto.encrypt("да")
    assert token != crypto.encrypt("да")  # каждый раз новый шифротекст
    assert crypto.decrypt(token) == "да"
    async with engine.begin() as conn:
        assert await conn.run_sync(crypto.encrypt_existing) == 0  # всё уже зашифровано
