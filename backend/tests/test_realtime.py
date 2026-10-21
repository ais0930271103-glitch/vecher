"""Personal session isolation, transactional delivery and private-data boundaries."""
import asyncio
import json
from datetime import datetime
from uuid import uuid4

import httpx
import jwt
import pytest
from sqlalchemy import delete, select

from app import push, realtime
from app.config import get_settings
from app.db import SessionLocal
from app.models import ChatMessage, DeliveryOutbox, DeliveryReceipt, QuietWish
from app.outbox import process_one
from conftest import login, make_pair


@pytest.fixture
def enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "realtime_token_secret", "realtime-test-secret-" + "a" * 32)
    monkeypatch.setattr(settings, "centrifugo_api_key", "realtime-test-api-" + "b" * 32)
    monkeypatch.setattr(settings, "centrifugo_api_url", "http://centrifugo:8001/api")
    monkeypatch.setattr(settings, "realtime_ws_url", "wss://api.example.test/connection/websocket")
    monkeypatch.setattr(settings, "realtime_enabled", True)
    return settings


async def identity(client, headers):
    return (await client.get("/auth/me", headers=headers)).json()["id"]


async def jobs(kind="realtime"):
    async with SessionLocal() as session:
        return list((await session.execute(select(DeliveryOutbox).where(DeliveryOutbox.kind == kind))).scalars())


async def clear_jobs():
    async with SessionLocal() as session:
        await session.execute(delete(DeliveryReceipt))
        await session.execute(delete(DeliveryOutbox))
        await session.commit()


def payload(row):
    data = json.loads(row.payload)
    realtime.validate_invalidation(data)
    return data


async def test_session_is_authenticated_and_disabled_by_default(client):
    assert (await client.get("/realtime/session")).status_code == 401
    headers = await login(client, "disabled@example.ru")
    response = await client.get("/realtime/session", headers=headers)
    assert response.json() == {"enabled": False}
    assert response.headers["cache-control"] == "no-store"


async def test_session_binds_only_the_authenticated_user_and_rejects_deleted_account(client, enabled):
    ha, hb = await make_pair(client)
    a, b = await identity(client, ha), await identity(client, hb)
    response = await client.get("/realtime/session", headers=ha, params={"channel": f"events:{b}", "user_id": b})
    data = response.json()
    assert set(data) == {"enabled", "url", "token", "channel", "expires_at"}
    assert data["enabled"] is True and data["channel"] == f"events:{a}"
    claims = jwt.decode(data["token"], enabled.realtime_token_secret, algorithms=["HS256"])
    assert claims["sub"] == a and claims["channels"] == [f"events:{a}"]
    assert claims["exp"] - claims["iat"] == enabled.realtime_token_ttl_seconds
    assert datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp() == claims["exp"]
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(data["token"], enabled.jwt_secret, algorithms=["HS256"])
    # A realtime token cannot authenticate to the REST API, either.
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {data['token']}"})).status_code == 401
    expired = jwt.encode({"sub": a, "exp": 1}, enabled.jwt_secret, algorithm="HS256")
    assert (await client.get("/realtime/session", headers={"Authorization": f"Bearer {expired}"})).status_code == 401
    assert (await client.delete("/auth/me", headers=ha)).status_code == 200
    assert (await client.get("/realtime/session", headers=ha)).status_code == 401
    assert (await client.get("/realtime/session", headers=hb)).json()["channel"] == f"events:{b}"
    assert all(j.user_id != a for j in await jobs() + await jobs("push"))


async def test_rollback_removes_domain_change_and_its_queued_event(client, enabled):
    headers = await login(client, "rollback@example.ru")
    uid = await identity(client, headers)
    await clear_jobs()
    async with SessionLocal() as session:
        session.add(QuietWish(user_id=uid, text="Only in the aborted transaction"))
        await realtime.enqueue_invalidation(session, [uid], ["solo"])
        await session.rollback()
    async with SessionLocal() as session:
        assert not (await session.execute(select(QuietWish))).first()
    assert await jobs() == []


async def test_chat_and_push_intent_roll_back_together_if_origin_transaction_fails(client, enabled, monkeypatch):
    from app.routers import chat

    ha, _ = await make_pair(client)
    await clear_jobs()
    push.outbox.clear()
    original = chat.invalidate_pair

    async def fail_after_enqueue(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("test transaction aborted")

    monkeypatch.setattr(chat, "invalidate_pair", fail_after_enqueue)
    with pytest.raises(RuntimeError, match="transaction aborted"):
        await client.post("/chat", headers=ha, json={"text": "Must not escape rollback"})
    async with SessionLocal() as session:
        assert not (await session.execute(select(ChatMessage))).first()
        assert not (await session.execute(select(DeliveryOutbox))).first()
    assert push.outbox == []


async def test_answer_invalidation_cannot_reveal_partner_answer(client, enabled):
    ha, hb = await make_pair(client)
    a, b = await identity(client, ha), await identity(client, hb)
    await clear_jobs()
    private_answer = "The private answer is not part of the event"
    assert (await client.post("/today/answer", headers=ha, json={"text": private_answer})).status_code == 200
    queued = await jobs()
    assert {j.user_id for j in queued} == {a, b}
    for job in queued:
        data = payload(job)
        assert "today" in data["scopes"]
        assert set(data) == {"version", "type", "event_id", "scopes"}
        assert private_answer not in job.payload and a not in job.payload and b not in job.payload
    partner_view = (await client.get("/today", headers=hb)).json()
    assert partner_view["partner_answered"] is True and partner_view["partner_answer"] is None
    # Both answers are stored, but the event still only tells the client to refetch.
    await client.post("/today/answer", headers=hb, json={"text": "My own answer"})
    assert (await client.get("/today", headers=hb)).json()["partner_answer"] == private_answer


@pytest.mark.parametrize("path,body,scope", [
    ("/person/notes", {"text": "private-note-content"}, "us"),
    ("/solo/wishes", {"text": "private-wish-content"}, "solo"),
    ("/reminders", {"text": "private-reminder-content", "target": "me"}, "us"),
    ("/letters", {"text": "future-private-letter", "deliver_at": "2099-01-01T09:00:00Z"}, "solo"),
])
async def test_private_mutations_only_invalidate_author_devices(client, enabled, path, body, scope):
    ha, hb = await make_pair(client)
    a = await identity(client, ha)
    await clear_jobs()
    response = await client.post(path, headers=ha, json=body)
    assert response.status_code == 201, response.text
    queued = await jobs()
    assert len(queued) == 1 and queued[0].user_id == a
    assert scope in payload(queued[0])["scopes"]
    assert body["text"] not in queued[0].payload
    if path == "/letters":
        assert (await client.get("/letters/inbox", headers=hb)).json() == []
    elif path == "/person/notes":
        assert (await client.get("/person", headers=hb)).json()["notes"] == []
    elif path == "/solo/wishes":
        assert (await client.get("/solo", headers=hb)).json()["wishes"] == []
    else:
        assert (await client.get("/reminders", headers=hb)).json() == []


async def test_chat_read_and_retry_are_idempotent_for_realtime(client, enabled):
    ha, hb = await make_pair(client)
    await clear_jobs()
    body = {"text": "message", "client_message_id": str(uuid4())}
    response = await client.post("/chat", headers=ha, json=body)
    assert response.status_code == 201
    ids = {j.id for j in await jobs()}
    assert len(ids) == 2
    retry = await client.post("/chat", headers=ha, json=body)
    assert retry.json()["id"] == response.json()["id"]
    assert {j.id for j in await jobs()} == ids
    await clear_jobs()
    assert (await client.post("/chat/read", headers=hb)).status_code == 200
    receipts = {j.id for j in await jobs()}
    assert len(receipts) == 2
    for _ in range(3):
        assert (await client.post("/chat/read", headers=hb)).status_code == 200
    assert {j.id for j in await jobs()} == receipts
    assert (await client.get("/chat/unread", headers=hb)).json() == {"count": 0}


async def test_wish_invalidation_preserves_choice_privacy_and_seen_badge_does_not_loop(client, enabled):
    ha, hb = await make_pair(client)
    a, b = await identity(client, ha), await identity(client, hb)
    card = (await client.get("/wishes/decks/romance/next", headers=ha)).json()[0]
    await clear_jobs()
    await client.post("/wishes/swipe", headers=ha, json={"card_id": card["id"], "choice": "yes"})
    assert (await client.get("/wishes/matches", headers=hb)).json() == []
    assert {j.user_id for j in await jobs()} == {a, b}
    assert all(set(payload(j)) == {"version", "type", "event_id", "scopes"} for j in await jobs())
    assert all("choice" not in j.payload and card["text"] not in j.payload for j in await jobs())
    await client.post("/wishes/swipe", headers=hb, json={"card_id": card["id"], "choice": "yes"})
    await clear_jobs()
    first = (await client.get("/wishes/matches", headers=hb)).json()
    assert first[0]["new"] is True
    viewed = await jobs()
    assert len(viewed) == 1 and viewed[0].user_id == b
    assert payload(viewed[0])["scopes"] == ["wishes"]
    for _ in range(3):
        assert (await client.get("/wishes/matches", headers=hb)).json()[0]["new"] is False
    assert [j.id for j in await jobs()] == [viewed[0].id]


async def test_parallel_match_views_create_one_read_receipt_and_one_event(client, enabled, monkeypatch):
    from app.db import engine
    from app import pair_lock

    ha, hb = await make_pair(client)
    card = (await client.get("/wishes/decks/romance/next", headers=ha)).json()[0]
    for headers in (ha, hb):
        await client.post("/wishes/swipe", headers=headers, json={"card_id": card["id"], "choice": "yes"})
    await clear_jobs()
    if engine.dialect.name == "postgresql":
        # API processes do not share the fallback Python lock. Use independent
        # local locks here so PostgreSQL itself must serialize these requests.
        class IndependentLocks:
            def setdefault(self, _key, default):
                return default

        monkeypatch.setattr(pair_lock, "_locks", IndependentLocks())
    responses = await asyncio.gather(*[client.get("/wishes/matches", headers=hb) for _ in range(3)])
    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()[0]["new"] for response in responses) == [False, False, True]
    queued = await jobs()
    assert len(queued) == 1 and queued[0].user_id == await identity(client, hb)
    assert payload(queued[0])["scopes"] == ["wishes"]


async def test_unpair_revokes_old_pair_jobs_and_leaves_only_safe_final_invalidations(client, enabled):
    ha, hb = await make_pair(client)
    a, b = await identity(client, ha), await identity(client, hb)
    await clear_jobs()
    await client.post("/chat", headers=ha, json={"text": "old pair"})
    old_ids = {j.id for j in await jobs()} | {j.id for j in await jobs("push")}
    async with SessionLocal() as session:
        session.add(DeliveryReceipt(outbox_id=next(iter(old_ids)), token_fingerprint="a" * 64, status="accepted"))
        await session.commit()
    assert (await client.post("/pairs/leave", headers=ha)).status_code == 200
    queued = await jobs()
    assert len(queued) == 2 and {j.user_id for j in queued} == {a, b}
    assert all(j.pair_id is None and payload(j)["scopes"] == ["all"] for j in queued)
    async with SessionLocal() as session:
        assert not (await session.execute(select(DeliveryOutbox).where(DeliveryOutbox.id.in_(old_ids)))).first()
        assert not (await session.execute(select(DeliveryReceipt))).first()
    assert (await client.get("/chat", headers=hb)).status_code == 409


async def test_realtime_remains_available_with_push_disabled_and_pair_paused(client, enabled):
    ha, hb = await make_pair(client)
    a, b = await identity(client, ha), await identity(client, hb)
    await client.put("/settings", headers=hb, json={"push_enabled": False})
    await client.post("/pairs/pause", headers=ha, json={"on": True})
    await clear_jobs()
    await client.post("/today/answer", headers=ha, json={"text": "Paused pair still syncs"})
    published = []

    async def publish(user_id, data):
        realtime.validate_invalidation(data)
        published.append(user_id)

    while await process_one(publisher=publish):
        pass
    assert set(published) == {a, b}
    assert all(j.state == "cancelled" and j.payload is None for j in await jobs("push"))


async def test_pending_pair_has_own_realtime_updates(client, enabled):
    headers = await login(client, "pending@example.ru")
    a = await identity(client, headers)
    await client.post("/pairs/invite", headers=headers)
    await clear_jobs()
    await client.post("/today/answer", headers=headers, json={"text": "Before joining"})
    queued = await jobs()
    assert len(queued) == 1 and queued[0].user_id == a
    published = []

    async def publish(user_id, data):
        published.append(user_id)

    assert await process_one(publisher=publish)
    assert published == [a]


@pytest.mark.parametrize("bad", [
    {"version": 1, "type": "invalidate", "event_id": str(uuid4()), "scopes": ["chat"], "text": "private"},
    {"version": True, "type": "invalidate", "event_id": str(uuid4()), "scopes": ["chat"]},
    {"version": 1, "type": "message", "event_id": str(uuid4()), "scopes": ["chat"]},
    {"version": 1, "type": "invalidate", "event_id": "not-uuid", "scopes": ["chat"]},
    {"version": 1, "type": "invalidate", "event_id": str(uuid4()), "scopes": ["unsafe"]},
    {"version": 1, "type": "invalidate", "event_id": str(uuid4()), "scopes": ["chat", "chat"]},
])
async def test_publisher_rejects_unsafe_payload_before_any_io(enabled, bad):
    with pytest.raises(ValueError):
        await realtime.publish_invalidation(str(uuid4()), bad)


async def test_publisher_uses_internal_api_key_and_idempotent_personal_publication(enabled, monkeypatch):
    uid = str(uuid4())
    data = {"version": 1, "type": "invalidate", "event_id": str(uuid4()), "scopes": ["chat"]}
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"result": {}})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(realtime.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(handler), **kwargs))
    await realtime.publish_invalidation(uid, data)
    request = seen[0]
    assert str(request.url) == "http://centrifugo:8001/api/publish"
    assert request.headers["X-API-Key"] == enabled.centrifugo_api_key
    assert json.loads(request.content) == {"channel": f"events:{uid}", "data": data, "idempotency_key": data["event_id"]}
    with pytest.raises(ValueError):
        await realtime.publish_invalidation("someone:else", data)
    assert len(seen) == 1


@pytest.mark.parametrize("status,reply", [(503, {"private": "remote-secret"}), (200, {"error": {"message": "remote-secret"}}),
                                        (200, {}), (200, []), (200, "not JSON")])
async def test_publisher_does_not_ack_errors_or_expose_remote_body(enabled, monkeypatch, status, reply):
    def handler(request):
        return httpx.Response(status, content=reply if isinstance(reply, str) else json.dumps(reply))

    client_class = httpx.AsyncClient
    monkeypatch.setattr(realtime.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(handler), **kwargs))
    data = {"version": 1, "type": "invalidate", "event_id": str(uuid4()), "scopes": ["chat"]}
    with pytest.raises(realtime.RealtimePublishError) as failure:
        await realtime.publish_invalidation(str(uuid4()), data)
    assert "remote-secret" not in str(failure.value)
