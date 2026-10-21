"""Real pair flows, stale devices, privacy withdrawal and encrypted history."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.config import get_settings
from app.db import SessionLocal
from app.models import Consent, DeliveryOutbox, Evening, EveningFeedback, PairLevel, PlanUnlock, WishCard
from app.routers import evenings
from conftest import login, make_pair
from test_plans import make_matches


async def ready_plan(client, ha, hb, cid=None):
    if cid is None:
        (cid,) = await make_matches(client, ha, hb, n=1)
    r = await client.post(f"/plans/{cid}/open", headers=ha)
    assert r.status_code == 200, r.text
    assert r.json()["evening_tracking_available"] is True
    for h in (ha, hb):
        assert (await client.post(f"/plans/{cid}/vote", headers=h, json={"option": 0})).status_code == 200
    return cid


async def begin(client, ha, cid, *, mode="together", client_id=None):
    body = {"client_id": client_id or str(uuid4()), "option": 0, "mode": mode}
    response = await client.post(f"/plans/{cid}/start", headers=ha, json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


async def action(client, h, evening, name, **extra):
    r = await client.post(f"/evenings/{evening['id']}/{name}", headers=h,
                          json={"revision": evening["revision"], **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def test_real_evening_flow_resume_complete_and_repeat_keep_history(client):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    first, _ = await begin(client, ha, cid)
    assert first["mode"] == "together" and first["revision"] == 1
    assert len(first["steps"]) >= 2 and all(s["status"] == "pending" for s in first["steps"])
    assert first["my_feedback"] is None and first["completed_at"] is None
    assert (await client.get("/evenings/current", headers=hb)).json() == first
    updated = await action(client, hb, first, "step", index=0, status="done")
    updated = await action(client, ha, updated, "step", index=1, status="skipped")
    paused = await action(client, ha, updated, "pause")
    assert paused["status"] == "paused"
    # A new HTTP request (including from another device) reads durable progress.
    assert (await client.get(f"/evenings/{first['id']}", headers=hb)).json() == paused
    assert (await client.post(f"/evenings/{first['id']}/step", headers=hb,
                              json={"revision": paused["revision"], "index": 0, "status": "pending"})).status_code == 409
    resumed = await action(client, hb, paused, "resume")
    done = await action(client, hb, resumed, "complete")
    assert done["status"] == "completed" and done["completed_at"]
    assert done["steps"][0]["status"] == "done"
    assert all(s["status"] != "pending" for s in done["steps"])
    assert (await client.get("/evenings/current", headers=ha)).json() is None
    repeated, _ = await begin(client, hb, cid, mode="apart")
    assert repeated["id"] != first["id"] and repeated["mode"] == "apart"
    assert all(s["status"] == "pending" for s in repeated["steps"])
    assert (await client.get("/evenings/history", headers=ha)).json() == {"items": [done], "next_before": None}
    cancelled = await action(client, ha, repeated, "cancel")
    assert cancelled["status"] == "cancelled"
    assert len((await client.get("/evenings/history", headers=hb)).json()["items"]) == 2


async def test_start_requires_open_current_mutual_option_and_idempotent_request(client):
    ha, hb = await make_pair(client)
    (cid,) = await make_matches(client, ha, hb, n=1)
    body = {"client_id": str(uuid4()), "option": 0, "mode": "apart"}
    assert (await client.post(f"/plans/{cid}/start", headers=ha, json=body)).status_code == 403
    await client.post(f"/plans/{cid}/open", headers=ha)
    assert (await client.post(f"/plans/{cid}/start", headers=ha, json=body)).status_code == 409
    await client.post(f"/plans/{cid}/vote", headers=ha, json={"option": 0})
    await client.post(f"/plans/{cid}/vote", headers=hb, json={"option": 1})
    assert (await client.post(f"/plans/{cid}/start", headers=ha, json=body)).status_code == 409
    await client.post(f"/plans/{cid}/vote", headers=hb, json={"option": 0})
    first = (await client.post(f"/plans/{cid}/start", headers=ha, json=body)).json()
    assert (await client.post(f"/plans/{cid}/start", headers=ha, json=body)).json() == first
    assert (await client.post(f"/plans/{cid}/start", headers=hb,
                              json={**body, "mode": "together"})).status_code == 409
    assert (await client.post(f"/plans/{cid}/start", headers=hb,
                              json={**body, "client_id": str(uuid4())})).status_code == 409
    done = await action(client, hb, first, "complete")
    # Delayed retry after completion still returns the first occurrence.
    assert (await client.post(f"/plans/{cid}/start", headers=ha, json=body)).json() == done
    async with SessionLocal() as session:
        assert len(list((await session.execute(select(Evening))).scalars())) == 1


async def test_new_start_consumes_appointment_in_upcoming_plan_and_matches(client):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert (await client.post(f"/plans/{cid}/schedule", headers=ha, json={"at": at})).status_code == 200
    assert [p["card_id"] for p in (await client.get("/plans/upcoming", headers=hb)).json()] == [cid]
    row, _ = await begin(client, hb, cid)
    for h in (ha, hb):
        assert (await client.get("/plans/upcoming", headers=h)).json() == []
        assert (await client.get(f"/plans/{cid}", headers=h)).json()["scheduled_at"] is None
        match = next(m for m in (await client.get("/wishes/matches", headers=h)).json() if m["card_id"] == cid)
        assert match["scheduled_at"] is None
    await action(client, ha, row, "complete")
    assert (await client.get("/plans/upcoming", headers=ha)).json() == []


async def test_delayed_start_retry_does_not_consume_next_appointment(client):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    row, body = await begin(client, ha, cid)
    done = await action(client, hb, row, "complete")
    at = datetime.now(timezone.utc) + timedelta(days=2)
    await client.post(f"/plans/{cid}/schedule", headers=hb, json={"at": at.isoformat()})
    assert (await client.post(f"/plans/{cid}/start", headers=ha, json=body)).json() == done
    saved = (await client.get(f"/plans/{cid}", headers=ha)).json()["scheduled_at"]
    assert datetime.fromisoformat(saved) == at
    assert [p["card_id"] for p in (await client.get("/plans/upcoming", headers=ha)).json()] == [cid]
    next_row, _ = await begin(client, ha, cid)
    assert next_row["id"] != done["id"]
    assert (await client.get("/plans/upcoming", headers=hb)).json() == []


async def test_conflicting_start_preserves_unstarted_appointment(client):
    ha, hb = await make_pair(client)
    c1, c2 = await make_matches(client, ha, hb, n=2)
    await ready_plan(client, ha, hb, c1)
    await ready_plan(client, ha, hb, c2)
    await begin(client, ha, c1)
    at = datetime.now(timezone.utc) + timedelta(days=1)
    await client.post(f"/plans/{c2}/schedule", headers=ha, json={"at": at.isoformat()})
    response = await client.post(f"/plans/{c2}/start", headers=hb,
                                 json={"client_id": str(uuid4()), "option": 0, "mode": "apart"})
    assert response.status_code == 409
    saved = (await client.get(f"/plans/{c2}", headers=hb)).json()["scheduled_at"]
    assert datetime.fromisoformat(saved) == at
    assert [p["card_id"] for p in (await client.get("/plans/upcoming", headers=ha)).json()] == [c2]


async def test_concurrent_schedule_waits_for_start_and_preserves_the_next_date(client, monkeypatch):
    from app.routers import plans

    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    old_at = datetime.now(timezone.utc) + timedelta(days=1)
    next_at = old_at + timedelta(days=1)
    await client.post(f"/plans/{cid}/schedule", headers=ha, json={"at": old_at.isoformat()})
    start_read, allow_start = asyncio.Event(), asyncio.Event()
    schedule_attempted, schedule_entered = asyncio.Event(), asyncio.Event()
    before_schedule = []
    real_require = evenings._require_unlocked
    real_lock = plans.pair_lock

    async def hold_start_after_read(*args):
        unlock = await real_require(*args)
        start_read.set()
        await allow_start.wait()
        return unlock

    @asynccontextmanager
    async def observe_schedule_lock(session, pair_id, **kwargs):
        schedule_attempted.set()
        async with real_lock(session, pair_id, **kwargs) as pair:
            before_schedule.append((await session.execute(select(PlanUnlock.scheduled_at).where(
                PlanUnlock.pair_id == pair_id, PlanUnlock.card_id == cid
            ))).scalar_one())
            schedule_entered.set()
            yield pair

    monkeypatch.setattr(evenings, "_require_unlocked", hold_start_after_read)
    monkeypatch.setattr(plans, "pair_lock", observe_schedule_lock)
    start_task = asyncio.create_task(client.post(f"/plans/{cid}/start", headers=ha,
        json={"client_id": str(uuid4()), "option": 0, "mode": "together"}))
    await asyncio.wait_for(start_read.wait(), timeout=5)
    schedule_task = asyncio.create_task(client.post(f"/plans/{cid}/schedule", headers=hb,
                                                    json={"at": next_at.isoformat()}))
    await asyncio.wait_for(schedule_attempted.wait(), timeout=5)
    assert not schedule_entered.is_set()  # lock order; no timing sleep
    allow_start.set()
    start_response, schedule_response = await asyncio.wait_for(
        asyncio.gather(start_task, schedule_task), timeout=5)
    assert start_response.status_code == 200 and schedule_response.status_code == 200
    assert before_schedule == [None]  # old appointment was consumed first
    saved = (await client.get(f"/plans/{cid}", headers=ha)).json()["scheduled_at"]
    assert datetime.fromisoformat(saved) == next_at
    await action(client, hb, start_response.json(), "complete")
    saved = (await client.get(f"/plans/{cid}", headers=hb)).json()["scheduled_at"]
    assert datetime.fromisoformat(saved) == next_at


async def test_simultaneous_starts_and_stale_step_are_serialized(client):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    body = {"client_id": str(uuid4()), "option": 0, "mode": "together"}
    responses = await asyncio.gather(*[client.post(f"/plans/{cid}/start", headers=h, json=body) for h in (ha, hb)])
    assert [r.status_code for r in responses] == [200, 200]
    row = responses[0].json()
    assert responses[1].json()["id"] == row["id"]
    updates = await asyncio.gather(*[
        client.post(f"/evenings/{row['id']}/step", headers=h,
                    json={"revision": row["revision"], "index": i, "status": "done"})
        for i, h in enumerate((ha, hb))
    ])
    assert sorted(r.status_code for r in updates) == [200, 409]
    current = (await client.get("/evenings/current", headers=ha)).json()
    assert current["revision"] == row["revision"] + 1
    assert sum(s["status"] == "done" for s in current["steps"]) == 1


async def test_different_concurrent_start_keys_cannot_create_two_active_evenings(client):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    responses = await asyncio.gather(*[
        client.post(f"/plans/{cid}/start", headers=h,
                    json={"client_id": str(uuid4()), "option": 0, "mode": "together"}) for h in (ha, hb)
    ])
    assert sorted(r.status_code for r in responses) == [200, 409]
    async with SessionLocal() as session:
        assert len(list((await session.execute(select(Evening))).scalars())) == 1


async def test_snapshot_is_frozen_even_if_catalogue_option_changes(client, monkeypatch):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    row, _ = await begin(client, ha, cid)
    original = evenings._matched_plan

    async def changed_plan(*args):
        card, kind, plan = await original(*args)
        return card, kind, {"intro": "changed", "options": [{"title": "New option", "steps": ["New instruction"]}]}

    monkeypatch.setattr(evenings, "_matched_plan", changed_plan)
    assert (await client.get(f"/evenings/{row['id']}", headers=hb)).json() == row
    await action(client, ha, row, "complete")
    next_row, _ = await begin(client, ha, cid)
    assert next_row["option_title"] == "New option"
    assert next_row["steps"][0]["text"] == "New instruction"
    assert (await client.get(f"/evenings/{row['id']}", headers=hb)).json()["steps"] == [
        {**step, "status": "skipped"} for step in row["steps"]
    ]


async def test_start_retry_uses_snapshot_when_catalogue_plan_was_removed(client, monkeypatch):
    from app.routers import plans

    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    row, body = await begin(client, ha, cid)
    monkeypatch.setattr(plans, "plan_for", lambda _: None)
    assert (await client.post(f"/plans/{cid}/start", headers=ha, json=body)).json() == row
    assert (await client.get(f"/evenings/{row['id']}", headers=hb)).json() == row
    done = await action(client, ha, row, "complete")
    assert (await client.post(f"/plans/{cid}/start", headers=ha, json=body)).json() == done
    assert (await client.post(f"/plans/{cid}/start", headers=ha,
                              json={**body, "client_id": str(uuid4())})).status_code == 404


async def test_private_feedback_is_not_shared_in_revision_history_or_events(client, monkeypatch):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    row, _ = await begin(client, ha, cid)
    data = {"feeling": "liked", "want_repeat": True}
    assert (await client.post(f"/evenings/{row['id']}/feedback", headers=ha, json=data)).status_code == 409
    done = await action(client, ha, row, "complete")
    monkeypatch.setattr(get_settings(), "realtime_enabled", True)
    response = await client.post(f"/evenings/{row['id']}/feedback", headers=ha, json=data)
    assert response.status_code == 200, response.text
    assert response.json() == {**done, "my_feedback": data}
    assert (await client.get(f"/evenings/{row['id']}", headers=hb)).json() == done
    assert (await client.get("/evenings/history", headers=hb)).json()["items"] == [done]
    await client.post(f"/evenings/{row['id']}/feedback", headers=ha, json=data)
    a_id = (await client.get("/auth/me", headers=ha)).json()["id"]
    async with SessionLocal() as session:
        jobs = list((await session.execute(select(DeliveryOutbox))).scalars())
        realtimes = [j for j in jobs if j.kind == "realtime"]
        assert len(realtimes) == 1 and realtimes[0].user_id == a_id
        assert json.loads(realtimes[0].payload)["scopes"] == ["plans"]
        feedbacks = list((await session.execute(select(EveningFeedback))).scalars())
        assert len(feedbacks) == 1 and feedbacks[0].user_id == a_id
        raw = (await session.execute(text("SELECT snapshot, progress FROM evenings"))).one()
        assert all(value.startswith("enc1:") for value in raw)
        raw_feedback = await session.scalar(text("SELECT data FROM evening_feedback"))
        assert raw_feedback.startswith("enc1:") and "liked" not in raw_feedback
    # Own replacement stays private; no shared updated_at or revision changes.
    response = await client.post(f"/evenings/{row['id']}/feedback", headers=ha,
                                json={"feeling": "not_for_me", "want_repeat": False})
    assert response.json()["revision"] == done["revision"]
    assert response.json()["updated_at"] == done["updated_at"]


async def test_another_pair_or_account_cannot_read_mutate_or_page_evening(client):
    ha, hb = await make_pair(client)
    hc, _ = await make_pair(client, "outside-a@example.ru", "outside-b@example.ru")
    solo = await login(client, "solo@example.ru")
    cid = await ready_plan(client, ha, hb)
    row, _ = await begin(client, ha, cid)
    path = f"/evenings/{row['id']}"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=solo)).status_code == 409
    assert (await client.get(path, headers=hc)).status_code == 404
    assert (await client.get("/evenings/current", headers=hc)).json() is None
    for route in ("pause", "complete", "cancel"):
        assert (await client.post(f"{path}/{route}", headers=hc, json={"revision": 1})).status_code == 404
    await action(client, ha, row, "complete")
    assert (await client.get(f"/evenings/history?before={row['id']}", headers=hc)).status_code == 404
    assert (await client.post(f"{path}/feedback", headers=hc,
                              json={"feeling": "liked", "want_repeat": True})).status_code == 404


async def test_withdrawn_match_hides_snapshot_and_releases_current_on_next_start(client):
    ha, hb = await make_pair(client)
    c1, c2 = await make_matches(client, ha, hb, n=2)
    await ready_plan(client, ha, hb, c1)
    await ready_plan(client, ha, hb, c2)
    row, body = await begin(client, ha, c1)
    await client.post("/wishes/swipe", headers=hb, json={"card_id": c1, "choice": "no"})
    for h in (ha, hb):
        assert (await client.get("/evenings/current", headers=h)).json() is None
        assert (await client.get(f"/evenings/{row['id']}", headers=h)).status_code == 404
    assert (await client.post(f"/plans/{c1}/start", headers=ha, json=body)).status_code == 404
    new, _ = await begin(client, ha, c2)
    assert new["card_id"] == c2
    assert (await client.get("/evenings/history", headers=ha)).json()["items"] == []
    async with SessionLocal() as session:
        hidden = await session.get(Evening, row["id"])
        assert hidden.status == "cancelled" and hidden.active_pair_id is None


@pytest.mark.parametrize("withdrawal", ["special", "personal", "card_inactive", "level"])
async def test_current_consent_and_level_gate_history_even_with_saved_snapshot(client, withdrawal):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    row, _ = await begin(client, ha, cid)
    done = await action(client, ha, row, "complete")
    b_id = (await client.get("/auth/me", headers=hb)).json()["id"]
    async with SessionLocal() as session:
        if withdrawal in ("special", "personal"):
            await session.execute(delete(Consent).where(Consent.user_id == b_id, Consent.kind == withdrawal))
        else:
            card = await session.get(WishCard, cid)
            if withdrawal == "card_inactive":
                card.active = False
            else:
                card.level = "spicy"  # both remain on warm
        await session.commit()
    assert (await client.get(f"/evenings/{done['id']}", headers=ha)).status_code == 404
    assert (await client.get("/evenings/history", headers=ha)).json()["items"] == []
    assert (await client.post(f"/evenings/{done['id']}/feedback", headers=ha,
                              json={"feeling": "liked", "want_repeat": True})).status_code == 404


async def test_original_snapshot_level_is_not_lowered_by_later_catalogue_edit(client):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    pair_id = (await client.get("/pairs/me", headers=ha)).json()["pair_id"]
    a_id, b_id = [(await client.get("/auth/me", headers=h)).json()["id"] for h in (ha, hb)]
    async with SessionLocal() as session:
        card = await session.get(WishCard, cid)
        card.level = "spicy"
        session.add_all([PairLevel(pair_id=pair_id, user_id=u, level="spicy") for u in (a_id, b_id)])
        await session.commit()
    row, _ = await begin(client, ha, cid)
    async with SessionLocal() as session:
        card = await session.get(WishCard, cid)
        card.level = "warm"
        for level in (await session.execute(select(PairLevel))).scalars():
            level.level = "warm"
        await session.commit()
    assert (await client.get("/evenings/current", headers=hb)).json() is None
    assert (await client.get(f"/evenings/{row['id']}", headers=ha)).status_code == 404


async def test_history_pages_are_stable_and_do_not_include_active_or_hidden_evenings(client):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    ids = []
    for _ in range(3):
        row, _ = await begin(client, ha, cid)
        ids.append(row["id"])
        await action(client, hb, row, "complete")
    active, _ = await begin(client, ha, cid)
    page = (await client.get("/evenings/history?limit=2", headers=ha)).json()
    assert [e["id"] for e in page["items"]] == list(reversed(ids[1:]))
    assert page["next_before"] == ids[1]
    page = (await client.get(f"/evenings/history?limit=2&before={page['next_before']}", headers=hb)).json()
    assert [e["id"] for e in page["items"]] == ids[:1]
    assert page["next_before"] is None
    assert (await client.get("/evenings/history?limit=51", headers=ha)).status_code == 422
    assert (await client.get("/evenings/history?limit=0", headers=ha)).status_code == 422
    assert (await client.get("/evenings/history?before=broken", headers=ha)).status_code == 422
    assert (await client.get(f"/evenings/history?before={active['id']}", headers=ha)).status_code == 404


async def test_unknown_fields_invalid_states_and_invalid_progress_are_rejected(client):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    body = {"client_id": str(uuid4()), "option": 0, "mode": "apart"}
    for bad in ({**body, "mode": "forever"}, {**body, "option": True}, {**body, "client_id": "a"},
                {**body, "status": "completed"}):
        assert (await client.post(f"/plans/{cid}/start", headers=ha, json=bad)).status_code == 422
    row, _ = await begin(client, ha, cid)
    for bad in ({"revision": True}, {"revision": 0}, {"revision": 1, "status": "completed"}):
        assert (await client.post(f"/evenings/{row['id']}/pause", headers=ha, json=bad)).status_code == 422
    assert (await client.post(f"/evenings/{row['id']}/resume", headers=ha, json={"revision": 1})).status_code == 409
    for bad in ({"revision": 1, "index": 1000, "status": "done"},
                {"revision": 1, "index": -1, "status": "done"},
                {"revision": 1, "index": 0, "status": "other"}):
        assert (await client.post(f"/evenings/{row['id']}/step", headers=ha, json=bad)).status_code == 422
    cancelled = await action(client, hb, row, "cancel")
    for name in ("resume", "complete", "pause"):
        assert (await client.post(f"/evenings/{row['id']}/{name}", headers=ha,
                                  json={"revision": cancelled["revision"]})).status_code == 409


async def test_evening_and_events_commit_together_and_abort_together(client, monkeypatch):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    monkeypatch.setattr(get_settings(), "realtime_enabled", True)
    original = evenings.invalidate_pair

    async def abort(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("evening transaction abort")

    monkeypatch.setattr(evenings, "invalidate_pair", abort)
    with pytest.raises(RuntimeError, match="evening transaction abort"):
        await begin(client, ha, cid)
    async with SessionLocal() as session:
        assert not (await session.execute(select(Evening))).first()
        assert not (await session.execute(select(DeliveryOutbox).where(DeliveryOutbox.kind == "realtime"))).first()
    monkeypatch.setattr(evenings, "invalidate_pair", original)
    row, _ = await begin(client, ha, cid)
    async with SessionLocal() as session:
        jobs = list((await session.execute(select(DeliveryOutbox).where(DeliveryOutbox.kind == "realtime"))).scalars())
        assert len(jobs) == 2
        assert all(json.loads(job.payload)["scopes"] == ["plans", "today", "us", "wishes"] for job in jobs)
        assert all(set(json.loads(job.payload)) == {"version", "type", "event_id", "scopes"} for job in jobs)
    monkeypatch.setattr(evenings, "invalidate_pair", abort)
    with pytest.raises(RuntimeError, match="evening transaction abort"):
        await action(client, ha, row, "step", index=0, status="done")
    assert (await client.get("/evenings/current", headers=hb)).json() == row


@pytest.mark.parametrize("deletion", ["leave", "account"])
async def test_pair_and_account_deletion_erase_all_evenings_and_feedback(client, deletion):
    ha, hb = await make_pair(client)
    cid = await ready_plan(client, ha, hb)
    row, _ = await begin(client, ha, cid)
    done = await action(client, ha, row, "complete")
    await client.post(f"/evenings/{row['id']}/feedback", headers=hb,
                      json={"feeling": "neutral", "want_repeat": False})
    if deletion == "leave":
        response = await client.post("/pairs/leave", headers=ha)
    else:
        response = await client.delete("/auth/me", headers=ha)
    assert response.status_code == 200
    async with SessionLocal() as session:
        assert not (await session.execute(select(Evening))).first()
        assert not (await session.execute(select(EveningFeedback))).first()
    assert (await client.get(f"/evenings/{done['id']}", headers=hb)).status_code == 409
