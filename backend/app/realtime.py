"""Personal state invalidations; domain data remains behind the REST API.

Router helpers only add encrypted outbox rows to the caller's transaction.
The delivery worker alone calls Centrifugo, after that transaction commits.
"""
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import PairMember

SCOPES = frozenset({"today", "wishes", "plans", "chat", "us", "solo", "pair", "all"})
EVENT_TTL = timedelta(minutes=5)


class RealtimePublishError(RuntimeError):
    """A retryable publication failure with no remote body or credentials."""


def personal_channel(user_id: str) -> str:
    """Only canonical UUIDs can become channel names; no client channel input."""
    if not isinstance(user_id, str) or str(UUID(user_id)) != user_id:
        raise ValueError("Invalid realtime recipient")
    return f"events:{user_id}"


def session_credentials(user_id: str) -> dict:
    settings = get_settings()
    if not settings.realtime_enabled:
        return {"enabled": False}
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expires_at = now + timedelta(seconds=settings.realtime_token_ttl_seconds)
    channel = personal_channel(user_id)
    token = jwt.encode(
        {"sub": user_id, "iat": now, "exp": expires_at, "channels": [channel]},
        settings.realtime_token_secret,
        algorithm="HS256",
    )
    return {
        "enabled": True,
        "url": settings.realtime_ws_url,
        "token": token,
        "channel": channel,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }


def validate_invalidation(payload: dict) -> None:
    """Fail closed if a future call site accidentally adds private content."""
    if (not isinstance(payload, dict)
            or set(payload) != {"version", "type", "event_id", "scopes"}
            or type(payload["version"]) is not int or payload["version"] != 1
            or payload["type"] != "invalidate"):
        raise ValueError("Invalid realtime payload")
    event_id, scopes = payload["event_id"], payload["scopes"]
    if not isinstance(event_id, str) or str(UUID(event_id)) != event_id:
        raise ValueError("Invalid realtime event identifier")
    if (not isinstance(scopes, list) or not 1 <= len(scopes) <= len(SCOPES)
            or any(not isinstance(scope, str) or scope not in SCOPES for scope in scopes)
            or len(set(scopes)) != len(scopes)):
        raise ValueError("Invalid realtime scopes")


async def enqueue_invalidation(
    session: AsyncSession,
    user_ids: Iterable[str],
    scopes: Iterable[str],
    *,
    pair_id: str | None = None,
    key: str | None = None,
    force: bool = False,
) -> None:
    """Stage an invalidation for each recipient. Never commit or send here."""
    if not get_settings().realtime_enabled:
        return
    # Import locally: the worker imports publish_invalidation when dispatching.
    from .outbox import enqueue_delivery

    payload = {"version": 1, "type": "invalidate", "event_id": str(uuid4()),
               "scopes": sorted(set(scopes))}
    validate_invalidation(payload)
    expires_at = datetime.now(timezone.utc) + EVENT_TTL
    for user_id in dict.fromkeys(user_ids):
        if not user_id:  # pending pair: the invited person has not joined yet
            continue
        personal_channel(user_id)
        await enqueue_delivery(
            session, kind="realtime", user_id=user_id, payload=payload,
            key=key, force=force, pair_id=pair_id, expires_at=expires_at,
        )


async def invalidate_pair(session: AsyncSession, ctx, scopes: Iterable[str], *, force: bool = False) -> None:
    """Common pair mutation: the author's other devices and their partner."""
    await enqueue_invalidation(session, [ctx.user.id, ctx.partner_id], scopes,
                               pair_id=ctx.pair.id, force=force)


async def invalidate_profile(session: AsyncSession, user_id: str, scopes: Iterable[str]) -> None:
    """Shared profile/city changes also affect a current partner's views."""
    if not get_settings().realtime_enabled:
        return
    pair_id = (await session.execute(
        select(PairMember.pair_id).where(PairMember.user_id == user_id)
    )).scalar_one_or_none()
    if pair_id:
        recipients = (await session.execute(
            select(PairMember.user_id).where(PairMember.pair_id == pair_id)
        )).scalars().all()
    else:
        recipients = [user_id]
    await enqueue_invalidation(session, recipients, scopes, pair_id=pair_id)


async def publish_invalidation(user_id: str, payload: dict) -> None:
    """Single bounded HTTP attempt; persistent retries belong to the worker."""
    channel = personal_channel(user_id)
    validate_invalidation(payload)
    settings = get_settings()
    if not settings.realtime_enabled:
        raise RealtimePublishError("Realtime delivery is disabled")
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False, follow_redirects=False) as client:
            response = await client.post(
                settings.centrifugo_api_url.rstrip("/") + "/publish",
                headers={"X-API-Key": settings.centrifugo_api_key},
                json={"channel": channel, "data": payload, "idempotency_key": payload["event_id"]},
            )
    except httpx.HTTPError:
        raise RealtimePublishError("Realtime transport unavailable") from None
    if response.status_code != 200:
        raise RealtimePublishError("Realtime transport rejected publication")
    try:
        reply = response.json()
    except ValueError:
        raise RealtimePublishError("Realtime transport returned invalid JSON") from None
    if not isinstance(reply, dict) or "error" in reply or not isinstance(reply.get("result"), dict):
        raise RealtimePublishError("Realtime server rejected publication")
