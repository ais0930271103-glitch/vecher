"""Свайпер желаний: оба листают одни и те же карточки, совпадения видны только взаимные.

Правило приватности: выбор партнёра никогда не отдаётся напрямую. Карточка попадает
в совпадения, только если ОБА выбрали «да» или «может быть». Невзаимное «да» не видно.
"""
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..content.wishes import CARDS, DECKS
from ..db import get_session
from ..deps import PairContext, active_pair, today_date, special_pair
from ..models import PlanUnlock, WishCard, WishSwipe, WishView, utcnow
from ..push import notify
from .today import mark_activity

router = APIRouter(prefix="/wishes", tags=["wishes"])

CHOICES = ("yes", "maybe", "no")
POSITIVE = ("yes", "maybe")


# ---------- helpers ----------

def _aware(dt: datetime) -> datetime:
    """SQLite отдаёт время без часового пояса — приводим к UTC, чтобы сравнивать."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def ensure_cards(session: AsyncSession) -> None:
    if (await session.execute(select(func.count(WishCard.id)))).scalar_one() == 0:
        session.add_all(WishCard(deck=d, text=t) for d, t in CARDS)
        await session.flush()


def match_kind(a: str, b: str) -> str | None:
    if a == "yes" and b == "yes":
        return "yes"
    if a in POSITIVE and b in POSITIVE:
        return "maybe"
    return None


async def _matches(session: AsyncSession, ctx: PairContext) -> list[tuple[WishCard, str, datetime]]:
    mine, theirs = aliased(WishSwipe), aliased(WishSwipe)
    rows = (await session.execute(
        select(WishCard, mine.choice, theirs.choice, mine.updated_at, theirs.updated_at)
        .join(mine, (mine.card_id == WishCard.id) & (mine.pair_id == ctx.pair.id) & (mine.user_id == ctx.user.id))
        .join(theirs, (theirs.card_id == WishCard.id) & (theirs.pair_id == ctx.pair.id)
              & (theirs.user_id == ctx.partner_id))
        .where(mine.choice.in_(POSITIVE), theirs.choice.in_(POSITIVE))
    )).all()
    out = []
    for card, a, b, ta, tb in rows:
        kind = match_kind(a, b)
        if kind:
            out.append((card, kind, max(_aware(ta), _aware(tb))))
    out.sort(key=lambda m: m[2], reverse=True)
    return out


async def match_for_card(session: AsyncSession, ctx: PairContext, card_id: int) -> tuple[WishCard, str] | None:
    """Совпадение пары по одной карточке или None."""
    rows = dict((await session.execute(
        select(WishSwipe.user_id, WishSwipe.choice)
        .where(WishSwipe.pair_id == ctx.pair.id, WishSwipe.card_id == card_id)
    )).all())
    a, b = rows.get(ctx.user.id), rows.get(ctx.partner_id)
    kind = match_kind(a, b) if a and b else None
    if not kind:
        return None
    card = await session.get(WishCard, card_id)
    return (card, kind) if card else None


async def _seen_at(session: AsyncSession, ctx: PairContext) -> datetime | None:
    v = (await session.execute(
        select(WishView.seen_at).where(WishView.pair_id == ctx.pair.id, WishView.user_id == ctx.user.id)
    )).scalar_one_or_none()
    return _aware(v) if v else None


# ---------- schemas ----------

class DeckOut(BaseModel):
    id: str
    title: str
    subtitle: str
    total: int
    done: int


class DecksOut(BaseModel):
    decks: list[DeckOut]
    matches: int
    new_matches: int


class CardOut(BaseModel):
    id: int
    deck: str
    text: str


class SwipeIn(BaseModel):
    card_id: int
    choice: str


class MatchOut(BaseModel):
    card_id: int
    deck: str
    deck_title: str
    text: str
    kind: str  # yes — оба «да»; maybe — можно обсудить
    matched_at: datetime
    new: bool = False
    plan_open: bool = False
    scheduled_at: datetime | None = None


class SwipeOut(BaseModel):
    match: MatchOut | None


def _match_out(card: WishCard, kind: str, at: datetime, seen: datetime | None = None) -> MatchOut:
    return MatchOut(
        card_id=card.id, deck=card.deck, deck_title=DECKS.get(card.deck, {}).get("title", card.deck),
        text=card.text, kind=kind, matched_at=at, new=seen is None or at > seen,
    )


# ---------- routes ----------

@router.get("/decks", response_model=DecksOut)
async def decks(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)) -> DecksOut:
    await ensure_cards(session)
    rows = (await session.execute(
        select(WishCard.deck, func.count(WishCard.id), func.count(WishSwipe.id))
        .outerjoin(WishSwipe, (WishSwipe.card_id == WishCard.id) & (WishSwipe.pair_id == ctx.pair.id)
                   & (WishSwipe.user_id == ctx.user.id))
        .where(WishCard.active.is_(True))
        .group_by(WishCard.deck)
    )).all()
    counts = {d: (total, done) for d, total, done in rows}
    matches = await _matches(session, ctx)
    seen = await _seen_at(session, ctx)
    await session.commit()
    return DecksOut(
        decks=[
            DeckOut(id=d, title=meta["title"], subtitle=meta["subtitle"],
                    total=counts.get(d, (0, 0))[0], done=counts.get(d, (0, 0))[1])
            for d, meta in sorted(DECKS.items(), key=lambda kv: kv[1]["order"])
        ],
        matches=len(matches),
        new_matches=sum(1 for _, _, at in matches if seen is None or at > seen),
    )


@router.get("/decks/{deck}/next", response_model=list[CardOut])
async def next_cards(
    deck: str,
    limit: int = Query(20, ge=1, le=50),
    ctx: PairContext = Depends(special_pair),
    session: AsyncSession = Depends(get_session),
) -> list[CardOut]:
    """Карточки, которые я ещё не листал. Сначала те, что партнёр уже пролистал, —
    так совпадения появляются быстрее (сам выбор партнёра при этом не раскрывается)."""
    if deck not in DECKS:
        raise HTTPException(404, "Такой колоды нет")
    await ensure_cards(session)
    my_done = select(WishSwipe.card_id).where(WishSwipe.pair_id == ctx.pair.id, WishSwipe.user_id == ctx.user.id)
    partner_done = select(WishSwipe.card_id).where(
        WishSwipe.pair_id == ctx.pair.id, WishSwipe.user_id == ctx.partner_id
    )
    cards = (await session.execute(
        select(WishCard)
        .where(WishCard.deck == deck, WishCard.active.is_(True), WishCard.id.not_in(my_done))
        .order_by(case((WishCard.id.in_(partner_done), 0), else_=1), WishCard.id)
        .limit(limit)
    )).scalars().all()
    await session.commit()
    return [CardOut(id=c.id, deck=c.deck, text=c.text) for c in cards]


@router.post("/swipe", response_model=SwipeOut)
async def swipe(
    body: SwipeIn,
    ctx: PairContext = Depends(special_pair),
    session: AsyncSession = Depends(get_session),
    day: date = Depends(today_date),
) -> SwipeOut:
    if body.choice not in CHOICES:
        raise HTTPException(422, "Выбор: yes, maybe или no")
    card = await session.get(WishCard, body.card_id)
    if card is None or not card.active:
        raise HTTPException(404, "Карточка не найдена")

    now = utcnow()
    mine = (await session.execute(
        select(WishSwipe).where(WishSwipe.pair_id == ctx.pair.id, WishSwipe.user_id == ctx.user.id,
                                WishSwipe.card_id == card.id)
    )).scalar_one_or_none()
    if mine is None:
        session.add(WishSwipe(pair_id=ctx.pair.id, user_id=ctx.user.id, card_id=card.id,
                              choice=body.choice, created_at=now, updated_at=now))
    elif mine.choice != body.choice:
        mine.choice, mine.updated_at = body.choice, now

    await mark_activity(session, ctx, day)

    theirs = (await session.execute(
        select(WishSwipe.choice).where(WishSwipe.pair_id == ctx.pair.id, WishSwipe.user_id == ctx.partner_id,
                                       WishSwipe.card_id == card.id)
    )).scalar_one_or_none()
    await session.commit()

    kind = match_kind(body.choice, theirs) if theirs else None
    if kind:
        # Партнёр ответил раньше — сообщаем ему о совпадении (без текста карточки).
        await notify(session, ctx.partner_id, "Вечер", "У вас новое совпадение",
                     {"open": "matches"}, key=f"match:{ctx.pair.id}:{card.id}")
    return SwipeOut(match=_match_out(card, kind, now) if kind else None)


@router.get("/matches", response_model=list[MatchOut])
async def matches(ctx: PairContext = Depends(special_pair), session: AsyncSession = Depends(get_session)):
    """Взаимные желания. Открытие списка снимает пометку «новое»."""
    found = await _matches(session, ctx)
    seen = await _seen_at(session, ctx)
    unlocks = {
        u.card_id: u for u in (await session.execute(
            select(PlanUnlock).where(PlanUnlock.pair_id == ctx.pair.id)
        )).scalars()
    }
    result = []
    for card, kind, at in found:
        m = _match_out(card, kind, at, seen)
        if (u := unlocks.get(card.id)) is not None:
            m.plan_open = True
            m.scheduled_at = _aware(u.scheduled_at) if u.scheduled_at else None
        result.append(m)

    view = (await session.execute(
        select(WishView).where(WishView.pair_id == ctx.pair.id, WishView.user_id == ctx.user.id)
    )).scalar_one_or_none()
    if view is None:
        session.add(WishView(pair_id=ctx.pair.id, user_id=ctx.user.id, seen_at=utcnow()))
    else:
        view.seen_at = utcnow()
    await session.commit()
    return result
