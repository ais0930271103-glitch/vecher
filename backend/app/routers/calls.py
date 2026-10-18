"""Приватные 1:1 WebRTC звонки. Сервер хранит только состояние и временный сигналинг.

Медиа идёт между устройствами / через coturn и не записывается API. Камера
включается клиентом после принятия; до этого сервер не принимает SDP и ICE.
"""
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..deps import PairContext, active_pair
from ..models import CallSignal, PairCall, utcnow
from ..pair_lock import pair_lock
from ..push import display_name, is_paused, notify

router = APIRouter(prefix="/calls", tags=["calls"])
log = logging.getLogger("vecher.calls")
MAX_SIGNALS = 2048
MAX_SIGNAL_BYTES = 4 * 1024 * 1024


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


class IceServerOut(BaseModel):
    urls: list[str]
    username: str | None = None
    credential: str | None = None


class CallConfigOut(BaseModel):
    enabled: bool
    relay_configured: bool
    ice_servers: list[IceServerOut]
    credentials_expires_at: datetime | None = None


class CallOut(BaseModel):
    id: str
    status: Literal["ringing", "active", "ended"]
    mine: bool
    partner_name: str
    created_at: datetime
    accepted_at: datetime | None
    ended_at: datetime | None
    expires_at: datetime
    end_reason: str | None


class EndIn(BaseModel):
    reason: Literal["hangup", "failed"] = "hangup"


class SignalIn(BaseModel):
    kind: Literal["offer", "answer", "ice"]
    data: dict
    client_id: str | None = Field(default=None, min_length=1, max_length=64)


class SignalOut(BaseModel):
    seq: int
    kind: str
    data: dict


class SignalsOut(BaseModel):
    call: CallOut
    signals: list[SignalOut]
    cursor: int


def _relay_configured() -> bool:
    s = get_settings()
    return bool(s.turn_urls and len(s.turn_secret) >= 32 and all(
        u.startswith(("turn:", "turns:")) and len(u) <= 300 and "@" not in u for u in s.turn_urls))


@router.get("/config", response_model=CallConfigOut)
async def config(ctx: PairContext = Depends(active_pair)):
    s = get_settings()
    relay = _relay_configured()
    servers = []
    stun = [u for u in s.stun_urls if u.startswith("stun:") and len(u) <= 300]
    if stun:
        servers.append(IceServerOut(urls=stun))
    expires = None
    if relay:
        expires = utcnow() + timedelta(seconds=max(s.turn_credential_ttl_seconds, s.call_max_duration_seconds + 600))
        username = f"{int(expires.timestamp())}:{ctx.user.id}"
        credential = base64.b64encode(hmac.new(s.turn_secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
        servers.append(IceServerOut(urls=s.turn_urls, username=username, credential=credential))
    return CallConfigOut(enabled=s.is_dev or relay, relay_configured=relay,
                         ice_servers=servers, credentials_expires_at=expires)


async def _out(session: AsyncSession, call: PairCall, ctx: PairContext) -> CallOut:
    return CallOut(id=call.id, status=call.status, mine=call.from_id == ctx.user.id,
                   partner_name=await display_name(session, ctx.partner_id),
                   created_at=_aware(call.created_at), accepted_at=_aware(call.accepted_at),
                   ended_at=_aware(call.ended_at), expires_at=_aware(call.expires_at), end_reason=call.end_reason)


async def _finish(session: AsyncSession, call: PairCall, reason: str, now: datetime) -> None:
    if call.status == "ended":
        return
    call.status, call.active_pair_id, call.end_reason, call.ended_at = "ended", None, reason, now
    await session.execute(delete(CallSignal).where(CallSignal.call_id == call.id))


async def _expire(session: AsyncSession, call: PairCall, now: datetime) -> None:
    if call.status == "ended":
        return
    if now >= _aware(call.expires_at):
        await _finish(session, call, "missed" if call.status == "ringing" else "expired", now)
    elif call.status == "active":
        cutoff = now - timedelta(seconds=get_settings().call_heartbeat_timeout_seconds)
        if _aware(call.caller_seen_at) < cutoff or _aware(call.callee_seen_at or call.accepted_at) < cutoff:
            await _finish(session, call, "disconnected", now)


def _touch(call: PairCall, user_id: str, now: datetime) -> None:
    if call.status != "ended":
        if call.from_id == user_id:
            call.caller_seen_at = now
        else:
            call.callee_seen_at = now


async def _get(session: AsyncSession, ctx: PairContext, call_id: str) -> PairCall:
    call = (await session.execute(select(PairCall).where(
        PairCall.id == call_id, PairCall.pair_id == ctx.pair.id).execution_options(populate_existing=True))).scalar_one_or_none()
    if call is None or ctx.user.id not in (call.from_id, call.to_id):
        raise HTTPException(404, "Звонок не найден")
    return call


@router.get("/active", response_model=CallOut | None)
async def active(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        call = (await session.execute(select(PairCall).where(PairCall.active_pair_id == ctx.pair.id))).scalar_one_or_none()
        if call is None:
            await session.commit()
            return None
        now = utcnow()
        await _expire(session, call, now)
        # Watcher не является heartbeat разговора: его посылает экран звонка через /signals.
        out = await _out(session, call, ctx) if call.status != "ended" else None
        await session.commit()
        return out


@router.post("", response_model=CallOut, status_code=201)
async def start(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    if not get_settings().is_dev and not _relay_configured():
        raise HTTPException(503, "Видеозвонки ещё не настроены на сервере")
    created = False
    async with pair_lock(session, ctx.pair.id):
        call = (await session.execute(select(PairCall).where(PairCall.active_pair_id == ctx.pair.id))).scalar_one_or_none()
        now = utcnow()
        if call:
            await _expire(session, call, now)
            if call.status != "ended":
                if call.from_id != ctx.user.id:
                    await session.commit()
                    raise HTTPException(409, "У тебя уже есть входящий или активный звонок")
                out = await _out(session, call, ctx)
                await session.commit()
                return out
        if await is_paused(session, ctx.user.id):
            await session.commit()
            raise HTTPException(409, "Пара на паузе — звонок можно начать после её завершения")
        await session.flush()  # освобождает UNIQUE active_pair_id завершившегося звонка.
        call = PairCall(pair_id=ctx.pair.id, active_pair_id=ctx.pair.id, from_id=ctx.user.id, to_id=ctx.partner_id,
                        status="ringing", created_at=now, caller_seen_at=now,
                        expires_at=now + timedelta(seconds=get_settings().call_ring_timeout_seconds))
        session.add(call)
        await session.flush()
        out = await _out(session, call, ctx)
        await session.commit()
        created = True
    if created:
        try:
            await notify(session, ctx.partner_id, "Вечер", "Входящий видеозвонок",
                         {"open": "call", "call_id": call.id}, key=f"call:{call.id}")
        except Exception:
            await session.rollback()
            log.warning("Уведомление о звонке не доставлено; входящий доступен через API")
    return out


@router.get("/{call_id}", response_model=CallOut)
async def get_call(call_id: str, ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        call = await _get(session, ctx, call_id)
        now = utcnow()
        await _expire(session, call, now)
        _touch(call, ctx.user.id, now)
        out = await _out(session, call, ctx)
        await session.commit()
        return out


@router.post("/{call_id}/accept", response_model=CallOut)
async def accept(call_id: str, ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        call = await _get(session, ctx, call_id)
        now = utcnow()
        await _expire(session, call, now)
        if call.to_id != ctx.user.id:
            raise HTTPException(403, "Принять звонок может только получатель")
        if call.status == "ended":
            await session.commit()
            raise HTTPException(409, "Звонок уже завершён")
        if call.status == "ringing":
            call.status, call.accepted_at = "active", now
            call.caller_seen_at = call.callee_seen_at = now
            call.expires_at = now + timedelta(seconds=get_settings().call_max_duration_seconds)
        out = await _out(session, call, ctx)
        await session.commit()
        return out


@router.post("/{call_id}/decline", response_model=CallOut)
async def decline(call_id: str, ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        call = await _get(session, ctx, call_id)
        now = utcnow()
        await _expire(session, call, now)
        if call.to_id != ctx.user.id:
            raise HTTPException(403, "Отклонить звонок может только получатель")
        if call.status == "active":
            raise HTTPException(409, "Звонок уже принят — заверши разговор")
        await _finish(session, call, "declined", now)
        out = await _out(session, call, ctx)
        await session.commit()
        return out


@router.post("/{call_id}/end", response_model=CallOut)
async def end(call_id: str, body: EndIn | None = None, ctx: PairContext = Depends(active_pair),
              session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        call = await _get(session, ctx, call_id)
        now = utcnow()
        await _expire(session, call, now)
        reason = (body or EndIn()).reason
        if call.status == "ringing" and reason == "hangup":
            reason = "cancelled" if call.from_id == ctx.user.id else "declined"
        await _finish(session, call, reason, now)
        out = await _out(session, call, ctx)
        await session.commit()
        return out


def _signal_data(body: SignalIn) -> str:
    data = body.data
    if body.kind in ("offer", "answer"):
        sdp = data.get("sdp")
        if not isinstance(sdp, str) or not 1 <= len(sdp.encode()) <= 65536 or data.get("type", body.kind) != body.kind:
            raise HTTPException(422, "Некорректное описание соединения")
        clean = {"type": body.kind, "sdp": sdp}
    else:
        candidate, mid, index = data.get("candidate"), data.get("sdpMid"), data.get("sdpMLineIndex")
        if (not isinstance(candidate, str) or len(candidate.encode()) > 8192
                or (mid is not None and (not isinstance(mid, str) or len(mid) > 128))
                or (index is not None and (type(index) is not int or not 0 <= index <= 32))):
            raise HTTPException(422, "Некорректный ICE-кандидат")
        clean = {"candidate": candidate, "sdpMid": mid, "sdpMLineIndex": index}
    return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))


@router.post("/{call_id}/signals")
async def signal(call_id: str, body: SignalIn, ctx: PairContext = Depends(active_pair),
                 session: AsyncSession = Depends(get_session)):
    data = _signal_data(body)
    async with pair_lock(session, ctx.pair.id):
        call = await _get(session, ctx, call_id)
        now = utcnow()
        await _expire(session, call, now)
        if call.status != "active":
            await session.commit()
            raise HTTPException(409, "Соединение можно установить только после принятия звонка")
        if ((body.kind == "offer" and call.from_id != ctx.user.id)
                or (body.kind == "answer" and call.to_id != ctx.user.id)):
            raise HTTPException(403, "Этот сигнал отправляет другой участник звонка")
        _touch(call, ctx.user.id, now)
        if body.client_id:
            old = (await session.execute(select(CallSignal).where(CallSignal.call_id == call.id,
                CallSignal.from_id == ctx.user.id, CallSignal.client_id == body.client_id))).scalar_one_or_none()
            if old:
                if old.kind != body.kind or old.data != data:
                    raise HTTPException(409, "Идентификатор сигнала уже использован")
                await session.commit()
                return {"seq": old.seq}
        count, used = (await session.execute(select(func.count(CallSignal.seq), func.coalesce(func.sum(CallSignal.size), 0))
                                             .where(CallSignal.call_id == call.id))).one()
        if count >= MAX_SIGNALS or used + len(data.encode()) > MAX_SIGNAL_BYTES:
            raise HTTPException(429, "Слишком много данных соединения — начните новый звонок")
        row = CallSignal(call_id=call.id, from_id=ctx.user.id, client_id=body.client_id,
                         kind=body.kind, data=data, size=len(data.encode()))
        session.add(row)
        await session.flush()
        await session.commit()
        return {"seq": row.seq}


@router.get("/{call_id}/signals", response_model=SignalsOut)
async def signals(call_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100),
                  ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        call = await _get(session, ctx, call_id)
        now = utcnow()
        await _expire(session, call, now)
        _touch(call, ctx.user.id, now)
        rows = (await session.execute(select(CallSignal).where(
            CallSignal.call_id == call.id, CallSignal.from_id != ctx.user.id, CallSignal.seq > after)
            .order_by(CallSignal.seq).limit(limit))).scalars().all() if call.status == "active" else []
        out = SignalsOut(call=await _out(session, call, ctx),
                         signals=[SignalOut(seq=r.seq, kind=r.kind, data=json.loads(r.data)) for r in rows],
                         cursor=rows[-1].seq if rows else after)
        await session.commit()
        return out


async def cleanup_calls(session: AsyncSession, now: datetime | None = None) -> None:
    now = now or utcnow()
    ids = (await session.execute(select(PairCall.id, PairCall.pair_id).where(PairCall.status != "ended"))).all()
    for call_id, pair_id in ids:
        try:
            async with pair_lock(session, pair_id, active=False):
                call = await session.get(PairCall, call_id, populate_existing=True)
                if call:
                    await _expire(session, call, now)
                await session.commit()
        except HTTPException:
            await session.rollback()
    # Терминальное состояние доступно клиенту ещё сутки; SDP/ICE удалены при завершении.
    await session.execute(delete(PairCall).where(PairCall.status == "ended", PairCall.ended_at < now - timedelta(days=1)))
    await session.commit()
