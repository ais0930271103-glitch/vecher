"""WebRTC: только своя пара, принятие, порядок сигналов, повторы, expiry и TURN."""
import asyncio
import base64
import hashlib
import hmac
from datetime import timedelta

from sqlalchemy import Text, func, select, type_coerce

from app import push
from app.config import get_settings
from app.db import SessionLocal
from app.models import CallSignal, PairCall, PairMember, utcnow
from conftest import login, make_pair


async def test_turn_credentials_require_pair_and_configuration(client, monkeypatch):
    assert (await client.get("/calls/config")).status_code == 401
    ha, _ = await make_pair(client)
    monkeypatch.setattr(get_settings(), "env", "staging")
    monkeypatch.setattr(get_settings(), "turn_urls", [])
    monkeypatch.setattr(get_settings(), "turn_secret", "")
    assert (await client.get("/calls/config", headers=ha)).json()["enabled"] is False
    assert (await client.post("/calls", headers=ha)).status_code == 503
    secret = "test-turn-secret-" + "x" * 32
    monkeypatch.setattr(get_settings(), "turn_urls", ["turn:turn.example.test:3478?transport=udp"])
    monkeypatch.setattr(get_settings(), "turn_secret", secret)
    cfg = (await client.get("/calls/config", headers=ha)).json()
    assert cfg["enabled"] is True and cfg["relay_configured"] is True and secret not in str(cfg)
    server = cfg["ice_servers"][0]
    assert server["credential"] == base64.b64encode(hmac.new(secret.encode(), server["username"].encode(), hashlib.sha1).digest()).decode()
    expiry = int(server["username"].split(":")[0])
    assert expiry >= int(utcnow().timestamp()) + get_settings().call_max_duration_seconds + 598


async def test_call_roles_signaling_order_retry_and_end(client):
    ha, hb = await make_pair(client)
    hc, _ = await make_pair(client, "c@example.ru", "d@example.ru")
    push.outbox.clear()
    r = await client.post("/calls", headers=ha, json={})
    assert r.status_code == 201, r.text
    call = r.json()
    cid = call["id"]
    assert call["status"] == "ringing" and call["mine"] is True
    assert (await client.post("/calls", headers=ha)).json()["id"] == cid
    assert (await client.post("/calls", headers=hb)).status_code == 409
    incoming = (await client.get("/calls/active", headers=hb)).json()
    assert incoming["id"] == cid and incoming["mine"] is False
    assert any(data == {"open": "call", "call_id": cid} for _, _, _, data in push.outbox)
    assert (await client.get(f"/calls/{cid}", headers=hc)).status_code == 404
    offer = {"kind": "offer", "data": {"type": "offer", "sdp": "v=0\r\nprivate-offer"}, "client_id": "offer-1"}
    assert (await client.post(f"/calls/{cid}/signals", headers=ha, json=offer)).status_code == 409
    assert (await client.post(f"/calls/{cid}/accept", headers=ha)).status_code == 403
    assert (await client.post(f"/calls/{cid}/accept", headers=hc)).status_code == 404
    assert (await client.post(f"/calls/{cid}/accept", headers=hb)).json()["status"] == "active"
    assert (await client.post(f"/calls/{cid}/accept", headers=hb)).json()["status"] == "active"
    assert (await client.post(f"/calls/{cid}/signals", headers=hb, json=offer)).status_code == 403
    seq1 = (await client.post(f"/calls/{cid}/signals", headers=ha, json=offer)).json()["seq"]
    assert (await client.post(f"/calls/{cid}/signals", headers=ha, json=offer)).json()["seq"] == seq1
    conflict = {**offer, "data": {"sdp": "different"}}
    assert (await client.post(f"/calls/{cid}/signals", headers=ha, json=conflict)).status_code == 409
    ice = {"kind": "ice", "data": {"candidate": "candidate:example", "sdpMid": "0", "sdpMLineIndex": 0}, "client_id": "ice-1"}
    seq2 = (await client.post(f"/calls/{cid}/signals", headers=ha, json=ice)).json()["seq"]
    got = (await client.get(f"/calls/{cid}/signals", headers=hb, params={"after": 0, "limit": 1})).json()
    assert [s["seq"] for s in got["signals"]] == [seq1] and got["cursor"] == seq1
    got2 = (await client.get(f"/calls/{cid}/signals", headers=hb, params={"after": got["cursor"]})).json()
    assert [s["seq"] for s in got2["signals"]] == [seq2]
    assert (await client.get(f"/calls/{cid}/signals", headers=ha)).json()["signals"] == []
    answer = {"kind": "answer", "data": {"sdp": "v=0\r\nanswer"}, "client_id": "answer-1"}
    assert (await client.post(f"/calls/{cid}/signals", headers=ha, json=answer)).status_code == 403
    assert (await client.post(f"/calls/{cid}/signals", headers=hb, json=answer)).status_code == 200
    assert (await client.get(f"/calls/{cid}/signals", headers=ha)).json()["signals"][0]["kind"] == "answer"
    assert (await client.get(f"/calls/{cid}/signals", headers=hc)).status_code == 404
    async with SessionLocal() as s:
        values = (await s.execute(select(type_coerce(CallSignal.data, Text)))).scalars().all()
        assert values and all(v.startswith("enc1:") and "private-offer" not in v for v in values)
    assert (await client.post(f"/calls/{cid}/decline", headers=hb)).status_code == 409
    done = (await client.post(f"/calls/{cid}/end", headers=ha, json={})).json()
    assert done["status"] == "ended" and done["end_reason"] == "hangup"
    assert (await client.post(f"/calls/{cid}/end", headers=hb)).json()["ended_at"] == done["ended_at"]
    assert (await client.get(f"/calls/{cid}/signals", headers=hb)).json()["signals"] == []
    assert (await client.get("/calls/active", headers=ha)).json() is None
    assert (await client.post(f"/calls/{cid}/accept", headers=hb)).status_code == 409
    assert (await client.post("/calls", headers=hb)).json()["id"] != cid


async def test_ring_expiry_decline_and_heartbeat(client):
    ha, hb = await make_pair(client)
    cid = (await client.post("/calls", headers=ha)).json()["id"]
    assert (await client.post(f"/calls/{cid}/decline", headers=ha)).status_code == 403
    assert (await client.post(f"/calls/{cid}/decline", headers=hb)).json()["end_reason"] == "declined"
    cid = (await client.post("/calls", headers=ha)).json()["id"]
    async with SessionLocal() as s:
        row = await s.get(PairCall, cid)
        row.expires_at = utcnow() - timedelta(seconds=1)
        await s.commit()
    assert (await client.get("/calls/active", headers=hb)).json() is None
    assert (await client.get(f"/calls/{cid}", headers=ha)).json()["end_reason"] == "missed"
    cid = (await client.post("/calls", headers=ha)).json()["id"]
    await client.post(f"/calls/{cid}/accept", headers=hb)
    async with SessionLocal() as s:
        row = await s.get(PairCall, cid)
        row.caller_seen_at = utcnow() - timedelta(seconds=91)
        await s.commit()
    # Один watcher не держит потерянный разговор бесконечно.
    assert (await client.get("/calls/active", headers=hb)).json() is None
    assert (await client.get(f"/calls/{cid}/signals", headers=hb)).json()["call"]["end_reason"] == "disconnected"


async def test_concurrent_calls_pause_mute_and_pair_erasure(client):
    ha, hb = await make_pair(client)
    await client.post("/pairs/pause", headers=ha, json={"on": True})
    assert (await client.post("/calls", headers=hb)).status_code == 409
    await client.post("/pairs/pause", headers=ha, json={"on": False})
    await client.put("/settings", headers=hb, json={"push_enabled": False})
    push.outbox.clear()
    responses = await asyncio.gather(client.post("/calls", headers=ha), client.post("/calls", headers=hb))
    assert sorted(r.status_code for r in responses) == [201, 409]
    cid = next(r.json()["id"] for r in responses if r.status_code == 201)
    caller, callee = (ha, hb) if responses[0].status_code == 201 else (hb, ha)
    if caller is ha:
        assert push.outbox == []  # FCM учитывает выключение push
    await client.post(f"/calls/{cid}/accept", headers=callee)
    await client.post(f"/calls/{cid}/signals", headers=caller, json={"kind": "offer", "data": {"sdp": "v=0"}})
    assert (await client.post("/pairs/leave", headers=ha)).status_code == 200
    async with SessionLocal() as s:
        assert (await s.execute(select(func.count(PairCall.id)))).scalar_one() == 0
        assert (await s.execute(select(func.count(CallSignal.seq)))).scalar_one() == 0


async def test_signal_validation_and_no_media_before_accept(client):
    ha, hb = await make_pair(client)
    cid = (await client.post("/calls", headers=ha)).json()["id"]
    ice = {"kind": "ice", "data": {"candidate": "candidate"}}
    assert (await client.post(f"/calls/{cid}/signals", headers=ha, json=ice)).status_code == 409
    assert (await client.get(f"/calls/{cid}/signals", headers=hb)).json()["signals"] == []
    await client.post(f"/calls/{cid}/accept", headers=hb)
    bad = [
        {"kind": "offer", "data": {"sdp": "x" * 65537}},
        {"kind": "offer", "data": {"sdp": "ok", "type": "answer"}},
        {"kind": "ice", "data": {"candidate": "x", "sdpMLineIndex": -1}},
        {"kind": "ice", "data": {"candidate": "x", "sdpMid": {"bad": "value"}}},
    ]
    for body in bad:
        assert (await client.post(f"/calls/{cid}/signals", headers=ha, json=body)).status_code == 422
    oversized = {"kind": "offer", "data": {"sdp": "v=0"}, "ignored_field": "x" * (100 * 1024)}
    assert (await client.post(f"/calls/{cid}/signals", headers=ha, json=oversized)).status_code == 413

    async def chunks():
        yield b'{"kind":"offer","data":{"sdp":"'
        yield b"x" * (100 * 1024)
        yield b'"}}'

    r = await client.post(f"/calls/{cid}/signals", headers={**ha, "Content-Type": "application/json"}, content=chunks())
    assert r.status_code == 413, r.text


async def test_two_join_requests_cannot_create_three_person_call_pair(client):
    ha, hb, hc = await login(client, "a@example.ru"), await login(client, "b@example.ru"), await login(client, "c@example.ru")
    invited = (await client.post("/pairs/invite", headers=ha)).json()
    results = await asyncio.gather(*[client.post("/pairs/join", headers=h, json={"code": invited["invite_code"]})
                                     for h in (hb, hc)])
    assert sorted(r.status_code for r in results) == [200, 404]
    async with SessionLocal() as s:
        assert (await s.execute(select(func.count(PairMember.id)).where(PairMember.pair_id == invited["pair_id"]))).scalar_one() == 2
    assert (await client.post("/calls", headers=ha)).status_code == 201
