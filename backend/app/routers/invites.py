"""Мягкое приглашение провести время вместе.

Ответы без «нет»: «Да!», «Давай в другой день» (можно предложить день), «Не сегодня, но мне приятно».
Приглашение видно на «Сегодня», пока ждёт ответа, сутки после ответа и до назначенного времени,
если договорились.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import PairContext, active_pair
from ..models import Invite, utcnow
from ..push import display_name, notify
from ..realtime import invalidate_pair

router = APIRouter(prefix="/invites", tags=["invites"])

ANSWERS = ("yes", "later", "not_today")
SHOW_AFTER_ANSWER = timedelta(hours=24)
SHOW_AFTER_AT = timedelta(hours=6)


def _aware(dt: datetime | None) -> datetime | None:
    """Время в UTC (SQLite теряет часовой пояс; присланное с поясом приводим к UTC)."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class InviteIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=300)
    at: datetime | None = None


class AnswerIn(BaseModel):
    answer: str
    proposed_at: datetime | None = None  # только для «давай в другой день»


class InviteOut(BaseModel):
    id: str
    mine: bool  # True — это я пригласил(а)
    title: str
    note: str | None
    at: datetime | None
    status: str
    proposed_at: datetime | None
    created_at: datetime


def _out(i: Invite, me: str) -> InviteOut:
    return InviteOut(id=i.id, mine=i.from_id == me, title=i.title, note=i.note, at=_aware(i.at),
                     status=i.status, proposed_at=_aware(i.proposed_at), created_at=_aware(i.created_at))


def _visible(i: Invite, now: datetime) -> bool:
    if i.status == "cancelled":
        return False
    if i.status == "pending":
        return True
    at = _aware(i.at)
    if i.status == "yes" and at is not None:
        return now <= at + SHOW_AFTER_AT
    answered = _aware(i.answered_at) or _aware(i.created_at)
    return now - answered <= SHOW_AFTER_ANSWER


async def _get(session: AsyncSession, ctx: PairContext, invite_id: str) -> Invite:
    i = await session.get(Invite, invite_id)
    if i is None or i.pair_id != ctx.pair.id:
        raise HTTPException(404, "Приглашение не найдено")
    return i


@router.get("", response_model=list[InviteOut])
async def list_invites(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(Invite).where(Invite.pair_id == ctx.pair.id, Invite.created_at >= now - timedelta(days=60))
        .order_by(Invite.created_at.desc())
    )).scalars().all()
    return [_out(i, ctx.user.id) for i in rows if _visible(i, now)]


@router.post("", response_model=InviteOut, status_code=201)
async def create_invite(body: InviteIn, ctx: PairContext = Depends(active_pair),
                        session: AsyncSession = Depends(get_session)):
    pending = (await session.execute(
        select(Invite.id).where(Invite.pair_id == ctx.pair.id, Invite.from_id == ctx.user.id,
                                Invite.status == "pending")
    )).first()
    if pending is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Одно приглашение уже ждёт ответа")
    at = _aware(body.at)
    if at is not None and at < datetime.now(timezone.utc) - timedelta(minutes=5):
        raise HTTPException(422, "Это время уже прошло")
    i = Invite(pair_id=ctx.pair.id, from_id=ctx.user.id, title=body.title.strip(),
               note=(body.note or "").strip() or None, at=at)
    session.add(i)
    await session.flush()
    name = await display_name(session, ctx.user.id)
    await notify(session, ctx.partner_id, "Вечер", f"{name} приглашает тебя провести время вместе",
                 {"open": "today"}, key=f"invite:{i.id}", pair_id=ctx.pair.id)
    await invalidate_pair(session, ctx, ["today"])
    await session.commit()
    return _out(i, ctx.user.id)


@router.post("/{invite_id}/answer", response_model=InviteOut)
async def answer(invite_id: str, body: AnswerIn, ctx: PairContext = Depends(active_pair),
                 session: AsyncSession = Depends(get_session)):
    i = await _get(session, ctx, invite_id)
    if i.from_id == ctx.user.id:
        raise HTTPException(403, "Отвечает тот, кого пригласили")
    if i.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "На это приглашение уже ответили")
    if body.answer not in ANSWERS:
        raise HTTPException(422, "Ответ: yes, later или not_today")
    i.status, i.answered_at = body.answer, utcnow()
    if body.answer == "later":
        i.proposed_at = _aware(body.proposed_at)
    name = await display_name(session, ctx.user.id)
    await notify(session, ctx.partner_id, "Вечер", f"{name} ответил(а) на твоё приглашение",
                 {"open": "today"}, key=f"invite-answer:{i.id}", pair_id=ctx.pair.id)
    await invalidate_pair(session, ctx, ["today"])
    await session.commit()
    return _out(i, ctx.user.id)


@router.post("/{invite_id}/agree", response_model=InviteOut)
async def agree(invite_id: str, ctx: PairContext = Depends(active_pair),
                session: AsyncSession = Depends(get_session)):
    """Пригласивший соглашается на предложенный другой день — договорились."""
    i = await _get(session, ctx, invite_id)
    if i.from_id != ctx.user.id or i.status != "later" or i.proposed_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Здесь не о чем договариваться")
    i.status, i.at, i.answered_at = "yes", i.proposed_at, utcnow()
    name = await display_name(session, ctx.user.id)
    await notify(session, ctx.partner_id, "Вечер", f"{name}: договорились!", {"open": "today"},
                 key=f"invite-agree:{i.id}", pair_id=ctx.pair.id)
    await invalidate_pair(session, ctx, ["today"])
    await session.commit()
    return _out(i, ctx.user.id)


@router.post("/{invite_id}/cancel", response_model=InviteOut)
async def cancel(invite_id: str, ctx: PairContext = Depends(active_pair),
                 session: AsyncSession = Depends(get_session)):
    i = await _get(session, ctx, invite_id)
    if i.from_id != ctx.user.id:
        raise HTTPException(403, "Отменить может тот, кто пригласил")
    i.status = "cancelled"
    await invalidate_pair(session, ctx, ["today"])
    await session.commit()
    return _out(i, ctx.user.id)
