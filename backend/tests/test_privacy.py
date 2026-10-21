"""Приватность и 152-ФЗ: согласие, настройки уведомлений, пауза, завершение пары, удаление аккаунта."""
from sqlalchemy import func, select

from app import push
from app.consent import CONSENT_VERSION
from app.db import SessionLocal
from app.models import DailyAnswer, ProfileFact, User, WishSwipe
from conftest import login, make_pair


async def uid(client, h):
    return (await client.get("/auth/me", headers=h)).json()["id"]


def bodies(user_id):
    return [body for u, _, body, _ in push.outbox if u == user_id]


async def count(model, *where):
    async with SessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def test_consent_personal_at_login_special_when_needed(client):
    h = await login(client, "new@example.ru", consent=False)
    me = (await client.get("/auth/me", headers=h)).json()
    assert me["consent_ok"] is False and me["special_ok"] is False and me["consent_version"] == CONSENT_VERSION
    # Без общего согласия нельзя ни создать пару, ни работать с данными пары.
    assert (await client.post("/pairs/invite", headers=h)).status_code == 403
    assert (await client.get("/today", headers=h)).status_code == 403
    assert (await client.post("/auth/consent", headers=h, json={"version": CONSENT_VERSION})).status_code == 422
    old = await client.post("/auth/consent", headers=h, json={"version": "2000-01-01", "personal": True})
    assert old.status_code == 409
    ok = await client.post("/auth/consent", headers=h, json={"version": CONSENT_VERSION, "personal": True})
    assert ok.status_code == 200 and ok.json()["consent_ok"] is True and ok.json()["special_ok"] is False
    code = (await client.post("/auth/request-code", json={"email": "new@example.ru"})).json()["dev_code"]
    assert (await client.post("/auth/verify", json={"email": "new@example.ru", "code": code})).json()["has_consent"] is True

    # С партнёром: вопрос дня и список колод доступны, а карточки, совпадения, планы и анкета — после
    # отдельного согласия на сведения об интимной жизни и здоровье.
    code = (await client.post("/pairs/invite", headers=h)).json()["invite_code"]
    hb = await login(client, "partner@example.ru")
    assert (await client.post("/pairs/join", headers=hb, json={"code": code})).status_code == 200
    assert (await client.get("/today", headers=h)).status_code == 200
    assert (await client.get("/wishes/decks", headers=h)).status_code == 200
    assert (await client.get("/wishes/decks/romance/next", headers=h)).status_code == 403
    assert (await client.get("/wishes/matches", headers=h)).status_code == 403
    assert (await client.put("/person/me", headers=h, json={"key": "flowers", "value": "пионы"})).status_code == 403
    sp = await client.post("/auth/consent", headers=h, json={"version": CONSENT_VERSION, "special": True})
    assert sp.json()["special_ok"] is True
    assert (await client.get("/wishes/decks/romance/next", headers=h)).status_code == 200
    assert (await client.put("/person/me", headers=h, json={"key": "flowers", "value": "пионы"})).status_code == 200


async def test_push_settings_off_and_private(client):
    ha, hb = await make_pair(client)
    push.outbox.clear()  # подготовка пары отправляет отдельное приветствие
    a = await uid(client, ha)
    assert (await client.get("/settings", headers=ha)).json() == {"push_enabled": True, "push_private": False}

    await client.put("/settings", headers=ha, json={"push_private": True})
    await client.post("/radar", headers=hb, json={"kind": "miss"})
    assert bodies(a) == [push.PRIVATE_BODY]  # без имени и без текста сигнала

    await client.put("/settings", headers=ha, json={"push_enabled": False})
    await client.post("/radar", headers=hb, json={"kind": "thinking"})
    assert bodies(a) == [push.PRIVATE_BODY]  # ничего нового не пришло
    assert (await client.get("/settings", headers=ha)).json() == {"push_enabled": False, "push_private": True}


async def test_pause_mutes_and_only_author_lifts(client):
    ha, hb = await make_pair(client)
    push.outbox.clear()  # проверяем уведомления после начала паузы, без события присоединения
    await client.put("/auth/me", headers=ha, json={"display_name": "Аня"})
    a, b = await uid(client, ha), await uid(client, hb)

    r = (await client.post("/pairs/pause", headers=ha, json={"on": True})).json()
    assert r["paused"] is True and r["paused_by_me"] is True
    assert any("на паузу" in t for t in bodies(b))  # партнёр честно узнаёт о паузе
    p_b = (await client.get("/pairs/me", headers=hb)).json()
    assert p_b["paused"] is True and p_b["paused_by_me"] is False

    await client.post("/radar", headers=hb, json={"kind": "miss"})
    await client.post("/radar", headers=ha, json={"kind": "miss"})
    assert bodies(a) == []                               # на паузе уведомлений нет
    assert not any("Скучаю" in t for t in bodies(b))
    assert (await client.get("/today", headers=hb)).status_code == 200  # всё остальное работает

    assert (await client.post("/pairs/pause", headers=hb, json={"on": False})).status_code == 403
    r = (await client.post("/pairs/pause", headers=ha, json={"on": False})).json()
    assert r["paused"] is False
    assert any("снял(а) паузу" in t for t in bodies(b))


async def test_leave_pair_erases_shared_keeps_personal(client):
    push.outbox.clear()
    ha, hb = await make_pair(client)
    a, b = await uid(client, ha), await uid(client, hb)
    await client.post("/today/answer", headers=ha, json={"text": "ответ"})
    card = (await client.get("/wishes/decks/romance/next", headers=hb)).json()[0]
    await client.post("/wishes/swipe", headers=hb, json={"card_id": card["id"], "choice": "yes"})
    await client.put("/person/me", headers=ha, json={"key": "flowers", "value": "пионы"})

    assert (await client.post("/pairs/leave", headers=ha)).status_code == 200
    assert (await client.get("/pairs/me", headers=ha)).json() is None
    assert (await client.get("/pairs/me", headers=hb)).json() is None
    assert any("завершил(а) вашу пару" in t for t in bodies(b))
    assert await count(DailyAnswer, DailyAnswer.user_id == a) == 0
    assert await count(WishSwipe, WishSwipe.user_id == b) == 0
    assert await count(ProfileFact, ProfileFact.user_id == a) == 1   # анкета «о себе» остаётся
    assert (await client.post("/pairs/leave", headers=ha)).status_code == 404
    # Можно начать заново — с новым приглашением.
    assert (await client.post("/pairs/invite", headers=ha)).status_code == 200


async def test_delete_account(client):
    push.outbox.clear()
    ha, hb = await make_pair(client)
    a, b = await uid(client, ha), await uid(client, hb)
    await client.put("/person/me", headers=ha, json={"key": "flowers", "value": "пионы"})

    assert (await client.delete("/auth/me", headers=ha)).json() == {"deleted": True}
    assert await count(User, User.id == a) == 0
    assert await count(ProfileFact, ProfileFact.user_id == a) == 0
    assert (await client.get("/auth/me", headers=ha)).status_code == 401
    assert (await client.get("/pairs/me", headers=hb)).json() is None
    assert any("удалил(а) аккаунт" in t for t in bodies(b))
    # С той же почтой можно зарегистрироваться заново — уже как новый человек.
    code = (await client.post("/auth/request-code", json={"email": "maxim@example.ru"})).json()["dev_code"]
    again = (await client.post("/auth/verify", json={"email": "maxim@example.ru", "code": code})).json()
    assert again["is_new"] is True and again["has_consent"] is False
