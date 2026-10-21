"""Transactional delivery queue, shared by push and realtime invalidations.

The caller owns the domain transaction. Workers claim with a compare-and-swap
lease, then do network I/O outside the DB transaction. Delivery is at least once:
an external acknowledgement lost immediately before its receipt commit can cause
a duplicate. A stable event ID and per-device receipts bound that failure window.
"""
import asyncio
import hashlib
import json
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import SessionLocal
from .models import (
    DeliveryOutbox, DeliveryReceipt, DeviceToken, Pair, PairCall, PairMember,
    PairPause, User, UserSettings, utcnow,
)

log = logging.getLogger("vecher.outbox")
KINDS = {"push", "realtime"}
TERMINAL_STATES = {"delivered", "cancelled", "dead"}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _insert(session: AsyncSession, model):
    """Both supported databases offer atomic INSERT ... ON CONFLICT."""
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif session.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Outbox requires PostgreSQL or SQLite")
    return insert(model)


def _dedupe(kind: str, user_id: str, key: str) -> str:
    return hashlib.sha256(json.dumps([kind, user_id, key], separators=(",", ":")).encode()).hexdigest()


async def enqueue_delivery(
    session: AsyncSession, *, kind: str, user_id: str, payload: dict,
    key: str | None = None, force: bool = False, pair_id: str | None = None,
    expires_at: datetime | None = None, available_at: datetime | None = None,
) -> bool:
    """Stage one intent, without committing or contacting an external service.

    A supplied key is idempotent per kind and recipient, including across worker
    restarts. Domain data and this row either commit together or both roll back.
    Terminal rows retain the key but erase the encrypted payload.
    """
    if kind not in KINDS or not isinstance(payload, dict):
        raise ValueError("Unsupported delivery kind or payload")
    now = utcnow()
    due = _aware(available_at) if available_at else now
    expiry = _aware(expires_at) if expires_at else now + timedelta(seconds=get_settings().outbox_ttl_seconds)
    if expiry <= now or due >= expiry:
        return False
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("Delivery payload exceeds limit")
    event_id = str(uuid.uuid4())
    dedupe = _dedupe(kind, user_id, key or event_id)
    # Flush newly created users/pairs and pending domain changes before the FK insert.
    await session.flush()
    result = await session.execute(
        _insert(session, DeliveryOutbox).values(
            id=event_id, kind=kind, user_id=user_id, pair_id=pair_id,
            dedupe_key=dedupe, payload=encoded, force=force,
            state="pending", attempts=0, available_at=due, expires_at=expiry,
            created_at=now,
        ).on_conflict_do_nothing(index_elements=["dedupe_key"])
    )
    return result.rowcount == 1


async def suppress_push(session: AsyncSession, *, user_id: str, key: str | None,
                        pair_id: str | None, reason: str) -> None:
    """Remember a timed event skipped due to preferences, without storing text.

    Otherwise a scheduler pass after resuming notifications would replay every
    previously suppressed reminder in its look-back window. Legacy PushLog had
    this behavior; the marker now commits with the caller's domain transaction.
    """
    if key is None:
        return
    if reason not in {"push_disabled", "pair_paused"}:
        raise ValueError("Unsupported suppression reason")
    now = utcnow()
    await session.flush()
    await session.execute(_insert(session, DeliveryOutbox).values(
        id=str(uuid.uuid4()), kind="push", user_id=user_id, pair_id=pair_id,
        dedupe_key=_dedupe("push", user_id, key), payload=None, force=False,
        state="cancelled", attempts=0, available_at=now,
        expires_at=now + timedelta(seconds=get_settings().outbox_ttl_seconds),
        last_error=reason, created_at=now, finished_at=now,
    ).on_conflict_do_nothing(index_elements=["dedupe_key"]))


@dataclass(frozen=True)
class Claim:
    id: str
    owner: str
    attempts: int
    expires_at: datetime


class DeliveryCancelled(Exception):
    """Safe reason code; never include user text or provider response bodies."""


class DeliveryRetry(Exception):
    pass


def _eligible(now: datetime):
    return or_(
        and_(DeliveryOutbox.state == "pending", DeliveryOutbox.available_at <= now),
        and_(DeliveryOutbox.state == "processing", DeliveryOutbox.lease_until <= now),
    )


async def claim_next(session_factory=SessionLocal, *, now: datetime | None = None) -> Claim | None:
    """Atomic optimistic lease, also safe on SQLite where SKIP LOCKED is absent."""
    now = _aware(now) if now else utcnow()
    async with session_factory() as session:
        query = (select(DeliveryOutbox.id).where(_eligible(now))
                 .order_by(DeliveryOutbox.available_at, DeliveryOutbox.created_at).limit(16))
        if session.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        candidates = list((await session.execute(query)).scalars())
        for event_id in candidates:
            owner = str(uuid.uuid4())
            row = (await session.execute(
                update(DeliveryOutbox).where(DeliveryOutbox.id == event_id, _eligible(now))
                .values(state="processing", lease_owner=owner,
                        lease_until=now + timedelta(seconds=get_settings().outbox_lease_seconds),
                        attempts=DeliveryOutbox.attempts + 1)
                .returning(DeliveryOutbox.id, DeliveryOutbox.attempts, DeliveryOutbox.expires_at)
            )).first()
            if row:
                await session.commit()
                return Claim(row.id, owner, row.attempts, _aware(row.expires_at))
        await session.commit()
    return None


def _owned(claim: Claim):
    return (DeliveryOutbox.id == claim.id, DeliveryOutbox.state == "processing",
            DeliveryOutbox.lease_owner == claim.owner)


async def _renew(claim: Claim, session_factory, now: datetime) -> bool:
    async with session_factory() as session:
        result = await session.execute(
            update(DeliveryOutbox).where(*_owned(claim), DeliveryOutbox.lease_until > now)
            .values(lease_until=now + timedelta(seconds=get_settings().outbox_lease_seconds))
        )
        await session.commit()
        return result.rowcount == 1


async def _finish(claim: Claim, state: str, reason: str | None, session_factory, now: datetime) -> None:
    assert state in TERMINAL_STATES
    async with session_factory() as session:
        await session.execute(update(DeliveryOutbox).where(*_owned(claim)).values(
            state=state, payload=None, lease_owner=None, lease_until=None,
            last_error=reason, finished_at=now,
        ))
        await session.commit()


async def _retry(claim: Claim, reason: str, session_factory, now: datetime, retry_after: float = 0) -> None:
    settings = get_settings()
    if claim.attempts >= settings.outbox_max_attempts or claim.expires_at <= now:
        await _finish(claim, "dead", reason, session_factory, now)
        return
    cap = min(settings.outbox_retry_max_seconds, settings.outbox_retry_base_seconds * 2 ** (claim.attempts - 1))
    delay = max(random.uniform(cap / 2, cap), min(max(retry_after, 0), 3600))
    due = min(now + timedelta(seconds=delay), claim.expires_at)
    async with session_factory() as session:
        await session.execute(update(DeliveryOutbox).where(*_owned(claim)).values(
            state="pending", available_at=due, lease_owner=None, lease_until=None,
            last_error=reason,
        ))
        await session.commit()


async def _authorized(session: AsyncSession, job: DeliveryOutbox, payload: dict, now: datetime) -> bool:
    """Return current private-push preference or raise a safe cancellation code."""
    if not await session.get(User, job.user_id):
        raise DeliveryCancelled("user_deleted")
    if _aware(job.expires_at) <= now:
        raise DeliveryCancelled("expired")
    if job.pair_id:
        current = (await session.execute(select(PairMember.id).join(Pair).where(
            PairMember.user_id == job.user_id, PairMember.pair_id == job.pair_id,
        ))).scalar_one_or_none()
        if current is None:
            raise DeliveryCancelled("pair_revoked")
    # A pause changes what partners may do, but the UI must still receive state
    # changes. Push preferences never disable in-app invalidations.
    if job.kind == "realtime":
        return False
    if not job.force:
        paused = (await session.execute(
            select(PairPause.id).join(PairMember, PairMember.pair_id == PairPause.pair_id)
            .where(PairMember.user_id == job.user_id)
        )).first()
        if paused:
            raise DeliveryCancelled("pair_paused")
    prefs = await session.get(UserSettings, job.user_id)
    if prefs and not prefs.push_enabled:
        raise DeliveryCancelled("push_disabled")
    data = payload.get("data", {})
    if data.get("open") == "call":
        call = await session.get(PairCall, data.get("call_id", ""))
        if (not call or call.to_id != job.user_id or call.status != "ringing"
                or _aware(call.expires_at) <= now or (job.pair_id and call.pair_id != job.pair_id)):
            raise DeliveryCancelled("call_ended")
    return bool(prefs and prefs.push_private)


async def _load(claim: Claim, session_factory, now: datetime):
    async with session_factory() as session:
        job = (await session.execute(select(DeliveryOutbox).where(*_owned(claim)))).scalar_one_or_none()
        if job is None:
            raise DeliveryCancelled("lease_lost")
        payload = json.loads(job.payload or "{}")
        private = await _authorized(session, job, payload, now)
        return job, payload, private


async def _receipt(claim: Claim, token: str, status: str, user_id: str, session_factory, now: datetime) -> None:
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    async with session_factory() as session:
        # Lock the parent so account/pair deletion cannot race the FK insert.
        exists = (await session.execute(select(DeliveryOutbox.id).where(*_owned(claim)).with_for_update())).first()
        if not exists:
            return
        await session.execute(_insert(session, DeliveryReceipt).values(
            outbox_id=claim.id, token_fingerprint=fingerprint, status=status, created_at=now,
        ).on_conflict_do_nothing(index_elements=["outbox_id", "token_fingerprint"]))
        if status == "invalid":
            await session.execute(delete(DeviceToken).where(DeviceToken.token == token, DeviceToken.user_id == user_id))
        await session.commit()


async def _push(claim: Claim, session_factory, clock: Callable[[], datetime], sender=None) -> None:
    from .push import FcmDeliveryExpired, PRIVATE_BODY, get_sender

    job, payload, _ = await _load(claim, session_factory, clock())
    async with session_factory() as session:
        tokens = list((await session.execute(select(DeviceToken.token).where(
            DeviceToken.user_id == job.user_id).order_by(DeviceToken.id))).scalars())
    if not tokens:
        return
    sender = sender or get_sender()
    if sender is None:
        raise DeliveryRetry("sender_unavailable")
    failures = []
    retry_after = 0.0
    for token in tokens:
        job, payload, private = await _load(claim, session_factory, clock())
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        async with session_factory() as session:
            ack = await session.get(DeliveryReceipt, (claim.id, fingerprint))
            owned_token = (await session.execute(select(DeviceToken.id).where(
                DeviceToken.token == token, DeviceToken.user_id == job.user_id,
            ))).first()
        if ack or not owned_token:
            continue
        if not await _renew(claim, session_factory, clock()):
            raise DeliveryCancelled("lease_lost")
        title = "Вечер" if private else payload["title"]
        body = PRIVATE_BODY if private else payload["body"]
        data = dict(payload.get("data", {}))
        data.setdefault("event_id", claim.id)
        try:
            accepted = await asyncio.wait_for(
                sender.send(token, title, body, data, expires_at=_aware(job.expires_at)),
                timeout=min(20, get_settings().outbox_lease_seconds / 2),
            )
        except FcmDeliveryExpired:
            raise DeliveryCancelled("expired") from None
        except Exception as error:  # Keep other devices independent; never log payloads/tokens/errors.
            failures.append(type(error).__name__)
            hinted_delay = getattr(error, "retry_after", 0)
            if isinstance(hinted_delay, (int, float)):
                retry_after = max(retry_after, hinted_delay)
            continue
        if accepted is not True and accepted is not False:
            failures.append("invalid_ack")
            continue
        await _receipt(claim, token, "accepted" if accepted else "invalid", job.user_id, session_factory, clock())
    if failures:
        error = DeliveryRetry("push_unavailable")
        error.retry_after = retry_after
        raise error


async def process_one(session_factory=SessionLocal, *, now: datetime | None = None, sender=None, publisher=None) -> bool:
    """Process at most one intent. Injectable dependencies make crash/retry tests real."""
    clock = (lambda: _aware(now)) if now is not None else utcnow
    claim = await claim_next(session_factory, now=clock())
    if claim is None:
        return False
    if claim.expires_at <= clock():
        await _finish(claim, "cancelled", "expired", session_factory, clock())
        return True
    if claim.attempts > get_settings().outbox_max_attempts:
        await _finish(claim, "dead", "retry_exhausted", session_factory, clock())
        return True
    try:
        job, payload, _ = await _load(claim, session_factory, clock())
        if job.kind == "push":
            await _push(claim, session_factory, clock, sender=sender)
        elif job.kind == "realtime":
            if not get_settings().realtime_enabled and publisher is None:
                raise DeliveryCancelled("realtime_disabled")
            if publisher is None:
                from .realtime import publish_invalidation
                publisher = publish_invalidation
            if not await _renew(claim, session_factory, clock()):
                raise DeliveryCancelled("lease_lost")
            await asyncio.wait_for(publisher(job.user_id, payload), timeout=min(20, get_settings().outbox_lease_seconds / 2))
        else:
            raise DeliveryCancelled("unsupported_kind")
    except DeliveryCancelled as error:
        await _finish(claim, "cancelled", str(error), session_factory, clock())
    except asyncio.CancelledError:
        # Do not release a possibly acknowledged intent: lease reclamation handles crashes/shutdowns.
        raise
    except Exception as error:
        reason = str(error) if isinstance(error, DeliveryRetry) else "delivery_unavailable"
        await _retry(claim, reason, session_factory, clock(), getattr(error, "retry_after", 0))
        log.warning("Delivery retry: kind=%s attempt=%d reason=%s", "outbox", claim.attempts, reason)
    else:
        await _finish(claim, "delivered", None, session_factory, clock())
    return True


async def run_forever() -> None:
    """Small bounded batches keep cancellation responsive and share DB leases across workers."""
    while True:
        try:
            for _ in range(25):
                if not await process_one():
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            # SQL exceptions can contain bound private data, so do not log exception/traceback.
            log.error("Delivery worker unavailable; will retry")
        await asyncio.sleep(get_settings().outbox_poll_seconds)
