"""Бесплатные планы к совпадениям без ограничения количества.

Старые поля подписки сохранены для совместимости APK 6/7 и чтения истории.
Они не управляют доступом к основным планам или будущему разделу «Вечер 18+».
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.plans import plan_for
from ..content.places import CARD_KINDS
from ..content.wishes import DECKS
from ..db import get_session
from ..deps import PairContext, active_pair, special_pair
from ..models import PairSubscription, PlanUnlock, PlanVote, WishCard, utcnow
from ..pair_lock import pair_lock
from ..push import display_name, local_when, notify
from ..realtime import invalidate_pair
from .wishes import _aware, match_for_card

router = APIRouter(tags=["plans"])


# APK 6/7 показывают кнопку открытия только при free_plans_left > 0.
# Значение означает «ещё один бесплатный план доступен», а не остаток квоты.
LEGACY_FREE_PLAN_AVAILABLE = 1


# ---------- прежний контракт подписки (только чтение) ----------

async def premium_until(session: AsyncSession, pair_id: str) -> datetime | None:
    until = (await session.execute(
        select(PairSubscription.until).where(PairSubscription.pair_id == pair_id)
    )).scalar_one_or_none()
    if until is None:
        return None
    until = _aware(until)
    return until if until > utcnow() else None


class SubscriptionOut(BaseModel):
    # Только прежняя запись «Вечер+»; это не право на будущий раздел 18+.
    premium: bool
    until: datetime | None
    free_plans_left: int = Field(
        default=LEGACY_FREE_PLAN_AVAILABLE,
        deprecated=True,
        description="Совместимость APK 6/7: ещё один бесплатный план всегда доступен; квоты нет.",
    )
    core_plans_unlimited: bool = True


async def _subscription(session: AsyncSession, pair_id: str) -> SubscriptionOut:
    until = await premium_until(session, pair_id)
    return SubscriptionOut(premium=until is not None, until=until)


@router.get("/subscription", response_model=SubscriptionOut)
async def subscription(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    return await _subscription(session, ctx.pair.id)


# ---------- планы ----------

class OptionOut(BaseModel):
    index: int
    title: str
    note: str = ""
    items_title: str | None = None
    items: list[str] = []
    steps: list[str] = []


class PlanOut(BaseModel):
    card_id: int
    text: str
    deck_title: str
    kind: str
    intro: str
    locked: bool
    premium: bool
    free_plans_left: int = Field(
        default=LEGACY_FREE_PLAN_AVAILABLE,
        deprecated=True,
        description="Совместимость APK 6/7: ещё один бесплатный план всегда доступен; квоты нет.",
    )
    core_plans_unlimited: bool = True
    evening_tracking_available: bool = True
    options: list[OptionOut]
    my_vote: int | None = None
    partner_vote: int | None = None
    chosen: int | None = None  # вариант, за который проголосовали оба
    places_available: bool = False  # есть «Выбрать конкретное» (рестораны, концерты…)
    scheduled_at: datetime | None = None


class VoteIn(BaseModel):
    option: int


class ScheduleIn(BaseModel):
    at: datetime | None  # null — снять с плана


class UpcomingOut(BaseModel):
    card_id: int
    text: str
    scheduled_at: datetime
    chosen_title: str | None


async def _unlock(session: AsyncSession, ctx: PairContext, card_id: int) -> PlanUnlock | None:
    return (await session.execute(
        select(PlanUnlock).where(PlanUnlock.pair_id == ctx.pair.id, PlanUnlock.card_id == card_id)
    )).scalar_one_or_none()


async def _votes(session: AsyncSession, ctx: PairContext, card_id: int) -> tuple[int | None, int | None]:
    rows = dict((await session.execute(
        select(PlanVote.user_id, PlanVote.option)
        .where(PlanVote.pair_id == ctx.pair.id, PlanVote.card_id == card_id)
    )).all())
    return rows.get(ctx.user.id), rows.get(ctx.partner_id)


async def _matched_plan(session: AsyncSession, ctx: PairContext, card_id: int):
    found = await match_for_card(session, ctx, card_id)
    if found is None:
        raise HTTPException(404, "Это не совпадение")
    card, kind = found
    plan = plan_for(card.text)
    if plan is None:
        raise HTTPException(404, "Для этой карточки план пока не готов")
    return card, kind, plan


async def _plan_out(session: AsyncSession, ctx: PairContext, card_id: int) -> PlanOut:
    card, kind, plan = await _matched_plan(session, ctx, card_id)
    unlock = await _unlock(session, ctx, card_id)
    premium = await premium_until(session, ctx.pair.id) is not None
    locked = unlock is None
    options = []
    for i, o in enumerate(plan["options"]):
        if locked:  # превью: только названия вариантов
            options.append(OptionOut(index=i, title=o["title"], note=o.get("note", "")))
        else:
            options.append(OptionOut(index=i, title=o["title"], note=o.get("note", ""),
                                     items_title=o.get("items_title"), items=o.get("items", []),
                                     steps=o.get("steps", [])))
    mine, theirs = (None, None) if locked else await _votes(session, ctx, card_id)
    return PlanOut(
        card_id=card.id, text=card.text, deck_title=DECKS.get(card.deck, {}).get("title", card.deck),
        kind=kind, intro=plan["intro"], locked=locked, premium=premium,
        places_available=card.text in CARD_KINDS,
        options=options, my_vote=mine, partner_vote=theirs,
        chosen=mine if mine is not None and mine == theirs else None,
        scheduled_at=_aware(unlock.scheduled_at) if unlock and unlock.scheduled_at else None,
    )


async def _require_unlocked(session: AsyncSession, ctx: PairContext, card_id: int) -> PlanUnlock:
    await _matched_plan(session, ctx, card_id)
    unlock = await _unlock(session, ctx, card_id)
    if unlock is None:
        raise HTTPException(403, "Сначала открой план")
    return unlock


@router.get("/plans/upcoming", response_model=list[UpcomingOut])
async def upcoming(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    """Ближайшие запланированные вечера — для экрана «Сегодня»."""
    since = utcnow() - timedelta(hours=6)
    rows = (await session.execute(
        select(PlanUnlock, WishCard)
        .join(WishCard, WishCard.id == PlanUnlock.card_id)
        .where(PlanUnlock.pair_id == ctx.pair.id, PlanUnlock.scheduled_at.is_not(None))
        .order_by(PlanUnlock.scheduled_at)
    )).all()
    out = []
    for unlock, card in rows:
        at = _aware(unlock.scheduled_at)
        if at < since:
            continue
        mine, theirs = await _votes(session, ctx, card.id)
        plan = plan_for(card.text)
        chosen = None
        if plan and mine is not None and mine == theirs and mine < len(plan["options"]):
            chosen = plan["options"][mine]["title"]
        out.append(UpcomingOut(card_id=card.id, text=card.text, scheduled_at=at, chosen_title=chosen))
        if len(out) == 3:
            break
    return out


@router.get("/plans/{card_id}", response_model=PlanOut)
async def get_plan(card_id: int, ctx: PairContext = Depends(special_pair),
                   session: AsyncSession = Depends(get_session)):
    return await _plan_out(session, ctx, card_id)


@router.post("/plans/{card_id}/open", response_model=PlanOut)
async def open_plan(card_id: int, ctx: PairContext = Depends(special_pair),
                    session: AsyncSession = Depends(get_session)):
    await _matched_plan(session, ctx, card_id)
    if await _unlock(session, ctx, card_id) is None:
        session.add(PlanUnlock(pair_id=ctx.pair.id, card_id=card_id, free=True))
        await invalidate_pair(session, ctx, ["plans", "wishes", "us"])
        await session.commit()
    return await _plan_out(session, ctx, card_id)


@router.post("/plans/{card_id}/vote", response_model=PlanOut)
async def vote(card_id: int, body: VoteIn, ctx: PairContext = Depends(special_pair),
               session: AsyncSession = Depends(get_session)):
    await _require_unlocked(session, ctx, card_id)
    _, _, plan = await _matched_plan(session, ctx, card_id)
    if not 0 <= body.option < len(plan["options"]):
        raise HTTPException(422, "Нет такого варианта")
    v = (await session.execute(
        select(PlanVote).where(PlanVote.pair_id == ctx.pair.id, PlanVote.user_id == ctx.user.id,
                               PlanVote.card_id == card_id)
    )).scalar_one_or_none()
    if v is None:
        session.add(PlanVote(pair_id=ctx.pair.id, user_id=ctx.user.id, card_id=card_id, option=body.option))
    else:
        v.option = body.option
    await invalidate_pair(session, ctx, ["plans", "today"])
    await session.commit()
    return await _plan_out(session, ctx, card_id)


@router.post("/plans/{card_id}/schedule", response_model=PlanOut)
async def schedule(card_id: int, body: ScheduleIn, ctx: PairContext = Depends(special_pair),
                   session: AsyncSession = Depends(get_session)):
    # Start consumes the previous appointment under the same lock. A new
    # appointment made concurrently must be applied after that consumption.
    async with pair_lock(session, ctx.pair.id):
        unlock = await _require_unlocked(session, ctx, card_id)
        if body.at is not None and body.at.tzinfo is None:
            raise HTTPException(422, "Укажи время с часовым поясом")
        # Храним в UTC: SQLite не сохраняет часовой пояс.
        unlock.scheduled_at = body.at.astimezone(timezone.utc) if body.at else None
        if body.at is not None:
            name = await display_name(session, ctx.user.id)
            await notify(session, ctx.partner_id, "Вечер", f"{name} запланировал(а) вечер: {local_when(body.at)}",
                         {"open": "plan", "card_id": card_id}, key=f"plan:{card_id}:{ctx.pair.id}:{body.at.isoformat()}",
                         pair_id=ctx.pair.id)
        await invalidate_pair(session, ctx, ["plans", "wishes", "today"])
        await session.commit()
        return await _plan_out(session, ctx, card_id)
