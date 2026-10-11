"""Вкладка «Я» — «Моё пространство»: что сделать для партнёра, пока его нет рядом."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..deps import PairContext, active_pair, today_date
from ..models import Activity, Letter, QuietWish, Surprise, User, WishCard, WishSwipe, utcnow
from ..push import local_when, notify
from .wishes import ensure_cards

router = APIRouter(tags=["solo"])


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise HTTPException(422, "Укажи время с часовым поясом")
    return dt.astimezone(timezone.utc)


def next_morning(now: datetime | None = None, hour: int = 8) -> datetime:
    """Ближайшие 8:00 по часовому поясу проекта (сегодня, если ещё не наступило, иначе завтра)."""
    tz = ZoneInfo(get_settings().timezone)
    local = (now or utcnow()).astimezone(tz)
    target = datetime.combine(local.date(), time(hour), tzinfo=tz)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


# ---------- схемы ----------

class WishOut(BaseModel):
    id: str
    text: str


class SentLetterOut(BaseModel):
    id: str
    text: str
    deliver_at: datetime
    delivered: bool
    read: bool


class MySurpriseOut(BaseModel):
    id: str
    title: str
    note: str | None
    at: datetime


class SoloOut(BaseModel):
    partner_name: str | None
    partner_active_today: bool
    swiped: int        # на сколько карточек я ответил
    remaining: int     # сколько ещё не пролистал
    wishes: list[WishOut]
    letters: list[SentLetterOut]     # мои письма партнёру
    surprises: list[MySurpriseOut]   # мои сюрпризы, которые ещё впереди


class LetterIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    deliver_at: datetime | None = None  # не указано — к ближайшим 8:00


class InboxLetterOut(BaseModel):
    id: str
    text: str
    deliver_at: datetime
    read: bool


class WishIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class SurpriseIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=500)
    at: datetime


class IncomingSurpriseOut(BaseModel):
    id: str
    at: datetime
    revealed: bool
    title: str | None   # до назначенного времени скрыто
    note: str | None


# ---------- «Моё пространство» ----------

@router.get("/solo", response_model=SoloOut)
async def solo(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session),
               day: date = Depends(today_date)):
    partner = await session.get(User, ctx.partner_id)
    active = (await session.execute(
        select(Activity.id).where(Activity.pair_id == ctx.pair.id, Activity.user_id == ctx.partner_id,
                                  Activity.day == day)
    )).scalar_one_or_none() is not None
    swiped = (await session.execute(
        select(func.count(WishSwipe.id)).where(WishSwipe.pair_id == ctx.pair.id, WishSwipe.user_id == ctx.user.id)
    )).scalar_one()
    await ensure_cards(session)
    total = (await session.execute(select(func.count(WishCard.id)).where(WishCard.active.is_(True)))).scalar_one()
    wishes = (await session.execute(
        select(QuietWish).where(QuietWish.user_id == ctx.user.id).order_by(QuietWish.created_at.desc())
    )).scalars().all()
    now = utcnow()
    letters = (await session.execute(
        select(Letter).where(Letter.pair_id == ctx.pair.id, Letter.from_id == ctx.user.id)
        .order_by(Letter.deliver_at.desc()).limit(10)
    )).scalars().all()
    surprises = (await session.execute(
        select(Surprise).where(Surprise.pair_id == ctx.pair.id, Surprise.from_id == ctx.user.id)
        .order_by(Surprise.at)
    )).scalars().all()
    await session.commit()  # ensure_cards мог залить стартовые карточки
    return SoloOut(
        partner_name=partner.display_name if partner else None,
        partner_active_today=active,
        swiped=swiped,
        remaining=max(0, total - swiped),
        wishes=[WishOut(id=w.id, text=w.text) for w in wishes],
        letters=[SentLetterOut(id=l.id, text=l.text, deliver_at=_aware(l.deliver_at),
                               delivered=_aware(l.deliver_at) <= now, read=l.read_at is not None) for l in letters],
        surprises=[MySurpriseOut(id=s.id, title=s.title, note=s.note, at=_aware(s.at))
                   for s in surprises if _aware(s.at) > now - timedelta(hours=12)],
    )


# ---------- письма ----------

@router.post("/letters", response_model=SentLetterOut, status_code=201)
async def send_letter(body: LetterIn, ctx: PairContext = Depends(active_pair),
                      session: AsyncSession = Depends(get_session)):
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Пустое письмо")
    at = _utc(body.deliver_at) if body.deliver_at else next_morning()
    letter = Letter(pair_id=ctx.pair.id, from_id=ctx.user.id, text=text, deliver_at=at, created_at=utcnow())
    session.add(letter)
    await session.commit()
    return SentLetterOut(id=letter.id, text=text, deliver_at=at, delivered=at <= utcnow(), read=False)


@router.get("/letters/inbox", response_model=list[InboxLetterOut])
async def inbox(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    """Письма от партнёра, время доставки которых наступило. Будущие не видны."""
    now = utcnow()
    rows = (await session.execute(
        select(Letter).where(Letter.pair_id == ctx.pair.id, Letter.from_id == ctx.partner_id)
        .order_by(Letter.deliver_at.desc()).limit(30)
    )).scalars().all()
    return [InboxLetterOut(id=l.id, text=l.text, deliver_at=_aware(l.deliver_at), read=l.read_at is not None)
            for l in rows if _aware(l.deliver_at) <= now]


@router.post("/letters/{letter_id}/read")
async def read_letter(letter_id: str, ctx: PairContext = Depends(active_pair),
                      session: AsyncSession = Depends(get_session)) -> dict:
    letter = await session.get(Letter, letter_id)
    if (letter is None or letter.pair_id != ctx.pair.id or letter.from_id != ctx.partner_id
            or _aware(letter.deliver_at) > utcnow()):
        raise HTTPException(404, "Письмо не найдено")
    if letter.read_at is None:
        letter.read_at = utcnow()
        await session.commit()
    return {"ok": True}


@router.delete("/letters/{letter_id}", status_code=204)
async def cancel_letter(letter_id: str, ctx: PairContext = Depends(active_pair),
                        session: AsyncSession = Depends(get_session)):
    """Отменить своё письмо, пока оно не доставлено."""
    letter = await session.get(Letter, letter_id)
    if letter is None or letter.from_id != ctx.user.id or letter.pair_id != ctx.pair.id:
        raise HTTPException(404, "Письмо не найдено")
    if _aware(letter.deliver_at) <= utcnow():
        raise HTTPException(409, "Письмо уже доставлено")
    await session.delete(letter)
    await session.commit()
    return Response(status_code=204)


# ---------- тихие желания ----------

@router.post("/solo/wishes", response_model=WishOut, status_code=201)
async def add_wish(body: WishIn, ctx: PairContext = Depends(active_pair),
                   session: AsyncSession = Depends(get_session)):
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Пустое желание")
    w = QuietWish(user_id=ctx.user.id, text=text, created_at=utcnow())
    session.add(w)
    await session.commit()
    return WishOut(id=w.id, text=w.text)


@router.delete("/solo/wishes/{wish_id}", status_code=204)
async def delete_wish(wish_id: str, ctx: PairContext = Depends(active_pair),
                      session: AsyncSession = Depends(get_session)):
    w = await session.get(QuietWish, wish_id)
    if w is None or w.user_id != ctx.user.id:
        raise HTTPException(404, "Не найдено")
    await session.delete(w)
    await session.commit()
    return Response(status_code=204)


# ---------- сюрпризы ----------

@router.post("/surprises", response_model=MySurpriseOut, status_code=201)
async def add_surprise(body: SurpriseIn, ctx: PairContext = Depends(active_pair),
                       session: AsyncSession = Depends(get_session)):
    at = _utc(body.at)
    s = Surprise(pair_id=ctx.pair.id, from_id=ctx.user.id, title=body.title.strip(),
                 note=(body.note or "").strip() or None, at=at, created_at=utcnow())
    session.add(s)
    await session.commit()
    await notify(session, ctx.partner_id, "Вечер", f"Тебя ждёт сюрприз — {local_when(at)}",
                 {"open": "today"}, key=f"surprise-new:{s.id}")
    return MySurpriseOut(id=s.id, title=s.title, note=s.note, at=at)


@router.delete("/surprises/{surprise_id}", status_code=204)
async def delete_surprise(surprise_id: str, ctx: PairContext = Depends(active_pair),
                          session: AsyncSession = Depends(get_session)):
    s = await session.get(Surprise, surprise_id)
    if s is None or s.from_id != ctx.user.id or s.pair_id != ctx.pair.id:
        raise HTTPException(404, "Не найдено")
    await session.delete(s)
    await session.commit()
    return Response(status_code=204)


@router.get("/surprises/incoming", response_model=list[IncomingSurpriseOut])
async def incoming_surprises(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    """Сюрпризы от партнёра: до назначенного времени — только «будет сюрприз» и когда."""
    now = utcnow()
    rows = (await session.execute(
        select(Surprise).where(Surprise.pair_id == ctx.pair.id, Surprise.from_id == ctx.partner_id)
        .order_by(Surprise.at)
    )).scalars().all()
    out = []
    for s in rows:
        at = _aware(s.at)
        if at < now - timedelta(hours=12):
            continue  # давно прошедшие не показываем
        revealed = at <= now
        out.append(IncomingSurpriseOut(id=s.id, at=at, revealed=revealed,
                                       title=s.title if revealed else None,
                                       note=s.note if revealed else None))
    return out
