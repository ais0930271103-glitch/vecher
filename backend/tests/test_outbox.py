"""Real DB transactions, retries, crash reclamation and privacy at dispatch time."""
import asyncio
from datetime import timedelta, timezone
import hashlib
import json
import threading
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import push
from app.config import get_settings
from app.db import Base
from app.models import (
    DeliveryOutbox, DeliveryReceipt, DeviceToken, Pair, PairCall, PairMember,
    PairPause, User, UserSettings, utcnow,
)
from app.outbox import claim_next, enqueue_delivery, process_one, _finish
from delivery_test_support import isolated_engine


@pytest.fixture
async def queue(tmp_path):
    async with isolated_engine(tmp_path) as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        a, b, pair = (str(uuid.uuid4()) for _ in range(3))
        async with factory() as session:
            session.add_all([User(id=a, email="a@queue.test"), User(id=b, email="b@queue.test"), Pair(id=pair, status="active")])
            await session.flush()
            session.add_all([
                PairMember(pair_id=pair, user_id=a, role="initiator"),
                PairMember(pair_id=pair, user_id=b, role="invited"),
                UserSettings(user_id=b),
                DeviceToken(id="token-a", user_id=b, token="device-one"),
                DeviceToken(id="token-b", user_id=b, token="device-two"),
            ])
            await session.commit()
        yield SimpleNamespace(factory=factory, engine=engine, a=a, b=b, pair=pair)


class Sender:
    def __init__(self, failing=(), invalid=()):
        self.failing, self.invalid, self.calls = set(failing), set(invalid), []

    async def send(self, token, title, body, data, *, expires_at=None):
        self.calls.append((token, title, body, data))
        if token in self.failing:
            raise push.FcmDeliveryError(503)
        return token not in self.invalid


def aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def stage(queue, **kwargs):
    async with queue.factory() as session:
        await push.notify(session, queue.b, "Private title", "PRIVATE BODY", {"open": "today"},
                          key=kwargs.pop("key", "event-1"), pair_id=queue.pair, **kwargs)
        await session.commit()


async def job(queue):
    async with queue.factory() as session:
        return (await session.execute(select(DeliveryOutbox))).scalar_one()


async def test_domain_rollback_also_rolls_back_intent_and_dev_capture(queue, monkeypatch):
    push.outbox.clear()
    monkeypatch.setattr(push, "get_sender", lambda: pytest.fail("enqueue must not contact Firebase"))
    async with queue.factory() as session:
        user = await session.get(User, queue.a)
        user.display_name = "Uncommitted"
        await push.notify(session, queue.b, "title", "body", key="rollback", pair_id=queue.pair)
        await session.rollback()
    async with queue.factory() as session:
        assert (await session.get(User, queue.a)).display_name is None
        assert (await session.scalar(select(func.count()).select_from(DeliveryOutbox))) == 0
    assert push.outbox == []


async def test_commit_persists_encrypted_payload_and_atomic_dedupe(queue):
    async def enqueue_once():
        async with queue.factory() as session:
            inserted = await enqueue_delivery(session, kind="push", user_id=queue.b,
                                              payload={"title": "secret", "body": "PRIVATE BODY", "data": {}},
                                              key="same", pair_id=queue.pair)
            await session.commit()
            return inserted
    assert sorted(await asyncio.gather(enqueue_once(), enqueue_once())) == [False, True]
    async with queue.engine.connect() as connection:
        ciphertext = await connection.scalar(text("SELECT payload FROM delivery_outbox"))
    assert ciphertext.startswith("enc1:") and "PRIVATE BODY" not in ciphertext
    assert "PRIVATE BODY" in (await job(queue)).payload


async def test_device_receipts_prevent_replaying_successful_devices(queue):
    await stage(queue)
    sender = Sender(failing={"device-two"})
    assert await process_one(queue.factory, sender=sender)
    first = await job(queue)
    assert first.state == "pending" and first.attempts == 1
    async with queue.factory() as session:
        receipts = (await session.execute(select(DeliveryReceipt))).scalars().all()
    assert len(receipts) == 1
    assert receipts[0].token_fingerprint == hashlib.sha256(b"device-one").hexdigest()
    sender.failing.clear()
    assert await process_one(queue.factory, now=aware(first.available_at) + timedelta(milliseconds=1), sender=sender)
    assert [c[0] for c in sender.calls] == ["device-one", "device-two", "device-two"]
    finished = await job(queue)
    assert finished.state == "delivered" and finished.payload is None
    assert len({c[3]["event_id"] for c in sender.calls}) == 1
    await stage(queue)
    assert (await job(queue)).id == finished.id  # no requeue after terminal payload removal


async def test_confirmed_dead_token_removed_while_other_device_succeeds(queue):
    await stage(queue)
    sender = Sender(invalid={"device-one"})
    await process_one(queue.factory, sender=sender)
    async with queue.factory() as session:
        assert list((await session.execute(select(DeviceToken.token).where(DeviceToken.user_id == queue.b))).scalars()) == ["device-two"]
        assert set((await session.execute(select(DeliveryReceipt.status))).scalars()) == {"accepted", "invalid"}
    assert (await job(queue)).state == "delivered"


async def test_concurrent_claims_and_crash_reclaim_reject_old_owner(queue):
    await stage(queue)
    now = utcnow()
    claims = await asyncio.gather(claim_next(queue.factory, now=now), claim_next(queue.factory, now=now))
    assert sum(c is not None for c in claims) == 1
    old = next(c for c in claims if c)
    assert not await process_one(queue.factory, now=now, sender=Sender())
    later = now + timedelta(seconds=get_settings().outbox_lease_seconds + 1)
    reclaimed = await claim_next(queue.factory, now=later)
    assert reclaimed.id == old.id and reclaimed.owner != old.owner and reclaimed.attempts == 2
    await _finish(old, "delivered", None, queue.factory, later)
    still_owned = await job(queue)
    assert still_owned.state == "processing" and still_owned.lease_owner == reclaimed.owner
    after_second_crash = later + timedelta(seconds=get_settings().outbox_lease_seconds + 1)
    sender = Sender()
    assert await process_one(queue.factory, now=after_second_crash, sender=sender)
    assert len(sender.calls) == 2 and (await job(queue)).state == "delivered"


async def test_late_private_preference_and_token_ownership_are_rechecked(queue):
    await stage(queue)
    async with queue.factory() as session:
        (await session.get(UserSettings, queue.b)).push_private = True
        await session.execute(update(DeviceToken).where(DeviceToken.token == "device-one").values(user_id=queue.a))
        await session.commit()
    sender = Sender()
    await process_one(queue.factory, sender=sender)
    assert len(sender.calls) == 1 and sender.calls[0][0] == "device-two"
    assert sender.calls[0][1:3] == ("Вечер", push.PRIVATE_BODY)


@pytest.mark.parametrize("reason", ["disabled", "paused", "unpaired", "deleted"])
async def test_privacy_changes_cancel_queued_push(queue, reason):
    await stage(queue)
    async with queue.factory() as session:
        if reason == "disabled":
            (await session.get(UserSettings, queue.b)).push_enabled = False
        elif reason == "paused":
            session.add(PairPause(pair_id=queue.pair, by_user_id=queue.a))
        elif reason == "unpaired":
            await session.execute(delete(PairMember).where(PairMember.user_id == queue.b))
        else:
            await session.execute(delete(User).where(User.id == queue.b))
        await session.commit()
    sender = Sender()
    await process_one(queue.factory, sender=sender)
    assert sender.calls == []
    async with queue.factory() as session:
        remaining = (await session.execute(select(DeliveryOutbox))).scalars().all()
    assert not remaining or (remaining[0].state == "cancelled" and remaining[0].payload is None)


async def test_ended_call_never_rings_after_retry(queue):
    call_id = str(uuid.uuid4())
    async with queue.factory() as session:
        call = PairCall(id=call_id, pair_id=queue.pair, from_id=queue.a, to_id=queue.b,
                        expires_at=utcnow() + timedelta(minutes=1))
        session.add(call)
        await push.notify(session, queue.b, "call", "incoming", {"open": "call", "call_id": call_id},
                          pair_id=queue.pair, expires_at=call.expires_at)
        call.status = "ended"
        await session.commit()
    sender = Sender()
    await process_one(queue.factory, sender=sender)
    assert sender.calls == [] and (await job(queue)).last_error == "call_ended"


async def test_realtime_delivery_survives_pause_and_push_disabled(queue):
    async with queue.factory() as session:
        session.add(PairPause(pair_id=queue.pair, by_user_id=queue.a))
        (await session.get(UserSettings, queue.b)).push_enabled = False
        await enqueue_delivery(session, kind="realtime", user_id=queue.b, pair_id=queue.pair,
                               payload={"version": 1, "type": "invalidate", "event_id": "id", "scopes": ["today"]})
        await session.commit()
    calls = []
    async def publish(user_id, payload):
        calls.append((user_id, payload))
    await process_one(queue.factory, publisher=publish)
    assert calls[0][0] == queue.b and (await job(queue)).state == "delivered"


async def test_retry_budget_and_ttl_are_bounded(queue, monkeypatch):
    monkeypatch.setattr(get_settings(), "outbox_max_attempts", 2)
    await stage(queue)
    sender = Sender(failing={"device-one", "device-two"})
    await process_one(queue.factory, sender=sender)
    first = await job(queue)
    assert aware(first.available_at) > aware(first.created_at)
    await process_one(queue.factory, now=aware(first.available_at) + timedelta(milliseconds=1), sender=sender)
    dead = await job(queue)
    assert dead.state == "dead" and dead.attempts == 2 and dead.payload is None
    assert not await process_one(queue.factory, sender=sender)


async def test_delayed_job_waits_and_expired_job_does_not_send(queue):
    now = utcnow()
    await stage(queue, available_at=now + timedelta(seconds=10), expires_at=now + timedelta(seconds=15))
    assert not await process_one(queue.factory, now=now + timedelta(seconds=5), sender=Sender())
    sender = Sender()
    assert await process_one(queue.factory, now=now + timedelta(seconds=16), sender=sender)
    assert sender.calls == [] and (await job(queue)).last_error == "expired"


@pytest.mark.parametrize("reason", ["disabled", "paused"])
async def test_suppressed_timed_events_are_not_replayed_after_resume(queue, reason):
    async with queue.factory() as session:
        if reason == "disabled":
            (await session.get(UserSettings, queue.b)).push_enabled = False
        else:
            session.add(PairPause(pair_id=queue.pair, by_user_id=queue.a))
        await session.commit()
    await stage(queue)
    skipped = await job(queue)
    assert skipped.state == "cancelled" and skipped.payload is None
    async with queue.factory() as session:
        (await session.get(UserSettings, queue.b)).push_enabled = True
        await session.execute(delete(PairPause))
        await session.commit()
    await stage(queue)
    assert (await job(queue)).id == skipped.id
    assert not await process_one(queue.factory, sender=Sender())


@pytest.mark.parametrize("status,code,expected", [(404, "UNREGISTERED", False), (400, "INVALID_ARGUMENT", "retry"), (503, "UNAVAILABLE", "retry"), (200, None, True)])
async def test_fcm_ack_semantics_and_retry_after(monkeypatch, status, code, expected):
    sender = object.__new__(push.FcmSender)
    async def response(*args, **kwargs):
        return httpx.Response(status, headers={"retry-after": "120"}, json={"error": {"details": [
            {"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": code}
        ]}})
    monkeypatch.setattr(sender, "_request", response)
    if expected == "retry":
        with pytest.raises(push.FcmDeliveryError) as failure:
            await sender.send("token", "title", "body", {})
        assert failure.value.retry_after == 120
    else:
        assert await sender.send("token", "title", "body", {}) is expected


def test_oauth_refresh_bounds_network_timeout_and_delegates_retries(monkeypatch):
    from google.auth.exceptions import TransportError
    from google.auth.transport import requests

    requests_made = []
    closed = []

    class FakeRequest:
        session = SimpleNamespace(close=lambda: closed.append(True))

        def __call__(self, *args, **kwargs):
            requests_made.append(kwargs)

    class Credentials:
        valid = False

        def refresh(self, request):
            request(url="https://oauth.example.test", timeout=120)
            request(url="https://oauth.example.test", timeout=120)

    monkeypatch.setattr(requests, "Request", FakeRequest)
    sender = object.__new__(push.FcmSender)
    sender.creds = Credentials()
    sender._refresh_lock = threading.Lock()
    with pytest.raises(TransportError, match="delegated"):
        sender._token()
    assert len(requests_made) == 1 and requests_made[0]["timeout"] == (2.0, 4.0)
    assert closed == [True]
    assert not sender._refresh_lock.locked()


async def test_fcm_provider_ttl_follows_job_deadline_after_refresh(monkeypatch):
    sender = object.__new__(push.FcmSender)
    sender.project_id = "test-project"
    sender._token = lambda: "test-access"
    requests_made = []

    def provider(request):
        requests_made.append(json.loads(request.content))
        return httpx.Response(200, json={"name": "accepted"})

    client = httpx.AsyncClient
    monkeypatch.setattr(push.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(provider), **kwargs))
    await sender.send("token", "title", "body", {}, expires_at=utcnow() + timedelta(seconds=40))
    ttl = requests_made[0]["message"]["android"]["ttl"]
    assert ttl.endswith("s") and 0 <= int(ttl[:-1]) <= 40
    with pytest.raises(push.FcmDeliveryExpired):
        await sender.send("token", "title", "body", {}, expires_at=utcnow() - timedelta(seconds=1))
    assert len(requests_made) == 1
