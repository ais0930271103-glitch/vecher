"""Бесплатные планы: прежние APK и история, приватность, выбор вдвоём и расписание."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import Consent, DeliveryOutbox, PairSubscription, PlanUnlock, utcnow
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


async def test_all_matched_plans_are_free_for_both_partners_and_legacy_apks(client):
    ha, hb = await make_pair(client)
    cards = await make_matches(client, ha, hb, n=3)

    p = (await client.get(f"/plans/{cards[0]}", headers=ha)).json()
    assert p["locked"] is True and p["core_plans_unlimited"] is True
    assert type(p["free_plans_left"]) is int and p["free_plans_left"] > 0
    assert len(p["options"]) >= 2 and p["options"][0]["items"] == []  # превью без содержимого

    for i, cid in enumerate(cards):
        h = (ha, hb)[i % 2]
        preview = (await client.get(f"/plans/{cid}", headers=h)).json()
        # APK6/7 показывают кнопку именно при положительном free_plans_left.
        assert type(preview["free_plans_left"]) is int and preview["free_plans_left"] > 0
        response = await client.post(f"/plans/{cid}/open", headers=h)
        assert response.status_code == 200, response.text
        p = response.json()
        assert p["locked"] is False and p["core_plans_unlimited"] is True
        assert p["premium"] is False and p["free_plans_left"] > 0
        assert p["options"][0]["items"] or p["options"][0]["steps"]
        for partner in (ha, hb):
            assert (await client.get(f"/plans/{cid}", headers=partner)).json()["locked"] is False
        # Повторное открытие не расходует лимит и не создаёт новую запись.
        assert (await client.post(f"/plans/{cid}/open", headers=h)).status_code == 200

    sub = (await client.get("/subscription", headers=ha)).json()
    assert sub == {"premium": False, "until": None, "free_plans_left": 1, "core_plans_unlimited": True}
    async with SessionLocal() as session:
        unlocks = list((await session.execute(select(PlanUnlock))).scalars())
        assert len(unlocks) == len(cards) and all(row.free for row in unlocks)
        assert not (await session.execute(select(PairSubscription))).first()


@pytest.mark.parametrize("subscription_days", [None, -2, 2])
async def test_old_quota_and_subscription_history_do_not_limit_free_plans(client, subscription_days):
    ha, hb = await make_pair(client)
    c1, c2, c3 = await make_matches(client, ha, hb, n=3)
    pair_id = (await client.get("/pairs/me", headers=ha)).json()["pair_id"]
    until = utcnow() + timedelta(days=subscription_days) if subscription_days is not None else None
    async with SessionLocal() as session:
        # Состояние существующей базы: старый бесплатный план уже использован,
        # а другой ранее открывался по подписке. Исторические признаки не переписываются.
        old_free = PlanUnlock(pair_id=pair_id, card_id=c1, free=True)
        old_paid = PlanUnlock(pair_id=pair_id, card_id=c2, free=False)
        session.add_all([old_free, old_paid])
        if until is not None:
            session.add(PairSubscription(pair_id=pair_id, until=until, source="dev"))
        await session.commit()
        old_ids = old_free.id, old_paid.id

    sub = (await client.get("/subscription", headers=hb)).json()
    active = subscription_days is not None and subscription_days > 0
    assert sub["premium"] is active and sub["core_plans_unlimited"] is True
    assert type(sub["free_plans_left"]) is int and sub["free_plans_left"] > 0
    assert (sub["until"] is not None) is active
    if active:
        assert datetime.fromisoformat(sub["until"]) == until

    preview = (await client.get(f"/plans/{c3}", headers=ha)).json()
    assert preview["locked"] is True and preview["free_plans_left"] > 0
    assert preview["premium"] is active
    assert (await client.post(f"/plans/{c3}/open", headers=hb)).status_code == 200
    for cid in (c1, c2):
        assert (await client.get(f"/plans/{cid}", headers=ha)).json()["locked"] is False

    async with SessionLocal() as session:
        rows = list((await session.execute(select(PlanUnlock))).scalars())
        by_card = {row.card_id: row for row in rows}
        assert len(rows) == 3
        assert (by_card[c1].id, by_card[c2].id) == old_ids
        assert by_card[c1].free is True and by_card[c2].free is False
        assert by_card[c3].free is True
        legacy = (await session.execute(select(PairSubscription))).scalar_one_or_none()
        if until is None:
            assert legacy is None
        else:
            assert legacy.source == "dev"
            assert legacy.until.replace(tzinfo=timezone.utc) == until


async def test_retired_dev_activation_cannot_create_or_extend_legacy_subscription(client):
    ha, hb = await make_pair(client)
    assert (await client.post("/subscription/dev-activate", headers=ha)).status_code == 404
    async with SessionLocal() as session:
        assert not (await session.execute(select(PairSubscription))).first()
    pair_id = (await client.get("/pairs/me", headers=ha)).json()["pair_id"]
    until = utcnow() + timedelta(days=1)
    async with SessionLocal() as session:
        session.add(PairSubscription(pair_id=pair_id, until=until, source="dev"))
        await session.commit()
    assert (await client.post("/subscription/dev-activate", headers=hb)).status_code == 404
    async with SessionLocal() as session:
        legacy = (await session.execute(select(PairSubscription))).scalar_one()
        assert legacy.until.replace(tzinfo=timezone.utc) == until and legacy.source == "dev"


def test_obsolete_free_plans_environment_does_not_restore_quota(monkeypatch):
    monkeypatch.setenv("VECHER_FREE_PLANS", "0")
    settings = Settings(_env_file=None)
    assert "free_plans" not in settings.model_dump()


async def test_free_access_preserves_authentication_consent_and_pair_isolation(client):
    ha, hb = await make_pair(client)
    (cid,) = await make_matches(client, ha, hb, n=1)
    hc, _ = await make_pair(client, "other-a@example.ru", "other-b@example.ru")
    assert (await client.get(f"/plans/{cid}")).status_code == 401
    assert (await client.post(f"/plans/{cid}/open")).status_code == 401
    assert (await client.get(f"/plans/{cid}", headers=hc)).status_code == 404
    assert (await client.post(f"/plans/{cid}/open", headers=hc)).status_code == 404

    uid = (await client.get("/auth/me", headers=ha)).json()["id"]
    async with SessionLocal() as session:
        await session.execute(delete(Consent).where(Consent.user_id == uid, Consent.kind == "special"))
        await session.commit()
    assert (await client.get(f"/plans/{cid}", headers=ha)).status_code == 403
    assert (await client.post(f"/plans/{cid}/open", headers=ha)).status_code == 403
    assert (await client.post(f"/plans/{cid}/open", headers=hb)).status_code == 200


async def test_free_open_commits_private_pair_invalidations_once(client, monkeypatch):
    ha, hb = await make_pair(client)
    (cid,) = await make_matches(client, ha, hb, n=1)
    pair_id = (await client.get("/pairs/me", headers=ha)).json()["pair_id"]
    users = {(await client.get("/auth/me", headers=h)).json()["id"] for h in (ha, hb)}
    monkeypatch.setattr(get_settings(), "realtime_enabled", True)

    for h in (ha, hb):
        assert (await client.post(f"/plans/{cid}/open", headers=h)).status_code == 200
    async with SessionLocal() as session:
        jobs = list((await session.execute(
            select(DeliveryOutbox).where(DeliveryOutbox.kind == "realtime")
        )).scalars())
        assert len(jobs) == 2 and {job.user_id for job in jobs} == users
        assert all(job.pair_id == pair_id for job in jobs)
        for job in jobs:
            data = json.loads(job.payload)
            assert set(data) == {"version", "type", "event_id", "scopes"}
            assert data["scopes"] == ["plans", "us", "wishes"]
        assert len(list((await session.execute(select(PlanUnlock))).scalars())) == 1


async def test_free_open_rolls_back_with_its_realtime_events(client, monkeypatch):
    from app.routers import plans

    ha, hb = await make_pair(client)
    (cid,) = await make_matches(client, ha, hb, n=1)
    monkeypatch.setattr(get_settings(), "realtime_enabled", True)
    original = plans.invalidate_pair

    async def abort_after_enqueue(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("free plan transaction aborted")

    monkeypatch.setattr(plans, "invalidate_pair", abort_after_enqueue)
    with pytest.raises(RuntimeError, match="free plan transaction aborted"):
        await client.post(f"/plans/{cid}/open", headers=ha)
    async with SessionLocal() as session:
        assert not (await session.execute(select(PlanUnlock))).first()
        assert not (await session.execute(
            select(DeliveryOutbox).where(DeliveryOutbox.kind == "realtime")
        )).first()


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
