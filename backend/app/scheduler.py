"""Фоновые уведомления по времени: письма к утру, сюрпризы, напоминания, важные даты.

`tick(session, now)` можно вызывать сколько угодно раз — outbox сохраняет ключ события.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import SessionLocal
from .models import Letter, PairDate, PairMember, Reminder, Surprise, utcnow
from .push import display_name, notify
from .realtime import enqueue_invalidation

log = logging.getLogger("vecher.scheduler")

DATE_REMIND_DAYS = (14, 7, 2, 0)  # за сколько дней напоминать о важной дате
DATE_REMIND_HOUR = 10             # в котором часу (по часовому поясу проекта)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _partner_of(session: AsyncSession, pair_id: str, user_id: str) -> str | None:
    return (await session.execute(
        select(PairMember.user_id).where(PairMember.pair_id == pair_id, PairMember.user_id != user_id)
    )).scalar_one_or_none()


async def tick(session: AsyncSession, now: datetime | None = None) -> None:
    now = now or utcnow()
    from .attachments import cleanup_attachments
    from .routers.calls import cleanup_calls

    await cleanup_calls(session, now)
    await cleanup_attachments(session, now)
    window = now - timedelta(days=2)  # не будим давно прошедшее (например, после долгого простоя)

    # Письма, время доставки которых наступило.
    for l in (await session.execute(select(Letter).where(Letter.read_at.is_(None)))).scalars().all():
        at = _aware(l.deliver_at)
        if window <= at <= now:
            to = await _partner_of(session, l.pair_id, l.from_id)
            if to:
                name = await display_name(session, l.from_id)
                await notify(session, to, "Вечер", f"{name} оставил(а) тебе письмо",
                             {"open": "today"}, key=f"letter:{l.id}", pair_id=l.pair_id)
                await enqueue_invalidation(session, [to], ["today", "solo"],
                                           pair_id=l.pair_id, key=f"letter:{l.id}")
                await enqueue_invalidation(session, [l.from_id], ["solo"],
                                           pair_id=l.pair_id, key=f"letter:{l.id}")

    # Сюрпризы, которые открылись.
    for s in (await session.execute(select(Surprise))).scalars().all():
        at = _aware(s.at)
        if window <= at <= now:
            to = await _partner_of(session, s.pair_id, s.from_id)
            if to:
                await notify(session, to, "Вечер", "Сюрприз открылся — загляни в «Вечер»",
                             {"open": "today"}, key=f"surprise-open:{s.id}", pair_id=s.pair_id)
                await enqueue_invalidation(session, [to], ["today", "solo"],
                                           pair_id=s.pair_id, key=f"surprise-open:{s.id}")
                await enqueue_invalidation(session, [s.from_id], ["solo"],
                                           pair_id=s.pair_id, key=f"surprise-open:{s.id}")

    # Напоминания со временем.
    for r in (await session.execute(
        select(Reminder).where(Reminder.done_at.is_(None), Reminder.due_at.is_not(None))
    )).scalars().all():
        at = _aware(r.due_at)
        if not (window <= at <= now):
            continue
        partner = await _partner_of(session, r.pair_id, r.author_id)
        targets = {"me": [r.author_id], "partner": [partner], "both": [r.author_id, partner]}[r.target]
        for uid in filter(None, targets):
            await notify(session, uid, "Напоминание", r.text, {"open": "reminders"},
                         key=f"reminder:{r.id}:{uid}", pair_id=r.pair_id)
            await enqueue_invalidation(session, [uid], ["today", "us"],
                                       pair_id=r.pair_id, key=f"reminder:{r.id}:{uid}")

    # Важные даты: за 14, 7, 2 дня и в сам день — после 10:00 по местному времени.
    tz = ZoneInfo(get_settings().timezone)
    local = now.astimezone(tz)
    if local.hour >= DATE_REMIND_HOUR:
        from .routers.us import next_occurrence  # здесь, чтобы не было цикла импортов

        today = local.date()
        for d in (await session.execute(select(PairDate))).scalars().all():
            nxt = next_occurrence(d.day, today, d.yearly)
            days = (nxt - today).days
            if days not in DATE_REMIND_DAYS:
                continue
            body = f"Сегодня: {d.title}" if days == 0 else f"Через {days} {_days(days)}: {d.title}"
            members = (await session.execute(
                select(PairMember.user_id).where(PairMember.pair_id == d.pair_id)
            )).scalars().all()
            for uid in members:
                await notify(session, uid, "Вечер", body, {"open": "dates"},
                             key=f"date:{d.id}:{nxt}:{days}:{uid}", pair_id=d.pair_id)
                await enqueue_invalidation(session, [uid], ["today", "us"],
                                           pair_id=d.pair_id, key=f"date:{d.id}:{nxt}:{days}:{uid}")
    await session.commit()


def _days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


async def run_forever(interval: int = 60) -> None:
    """Фоновый цикл внутри сервера (раз в минуту)."""
    while True:
        try:
            async with SessionLocal() as session:
                await tick(session)
        except Exception:  # SQL exceptions can include private bind values.
            log.error("Ошибка фоновых уведомлений; следующий проход повторит попытку")
        await asyncio.sleep(interval)
