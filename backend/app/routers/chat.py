"""Чат пары: текст, «Обсудить» (совпадение, план, приглашение, письмо), ответы, реакции, прочитано.

Мгновенная доставка — на этапе сервера (Centrifugo); пока приложение подтягивает новое раз в несколько секунд.
Голосовые и звонки — тоже на этапе сервера.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import PairContext, active_pair
from ..models import ChatMessage, ChatRead, utcnow
from ..push import display_name, notify

router = APIRouter(prefix="/chat", tags=["chat"])

REF_KINDS = ("match", "plan", "invite", "letter")
REACTIONS = ("❤️", "😂", "😮", "😢", "👍", "🔥")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class MessageIn(BaseModel):
    text: str | None = Field(default=None, max_length=2000)
    ref_kind: str | None = None
    ref_id: str | None = Field(default=None, max_length=64)
    ref_title: str | None = Field(default=None, max_length=300)
    reply_to: str | None = None


class ReactIn(BaseModel):
    emoji: str | None


class MessageOut(BaseModel):
    id: str
    mine: bool
    text: str | None
    ref_kind: str | None
    ref_id: str | None
    ref_title: str | None
    reply_to: str | None
    reply_text: str | None  # кусочек сообщения, на которое ответили
    reaction: str | None
    deleted: bool
    created_at: datetime


class ChatOut(BaseModel):
    messages: list[MessageOut]  # по времени, старые сверху
    partner_read_at: datetime | None
    has_more: bool


def _preview(m: ChatMessage | None) -> str | None:
    if m is None:
        return None
    if m.deleted:
        return "Сообщение удалено"
    return (m.text or m.ref_title or "")[:120]


async def _read_at(session: AsyncSession, pair_id: str, user_id: str) -> datetime | None:
    v = (await session.execute(
        select(ChatRead.read_at).where(ChatRead.pair_id == pair_id, ChatRead.user_id == user_id)
    )).scalar_one_or_none()
    return _aware(v)


def _out(m: ChatMessage, me: str, replies: dict[str, ChatMessage]) -> MessageOut:
    return MessageOut(
        id=m.id, mine=m.from_id == me,
        text=None if m.deleted else m.text,
        ref_kind=None if m.deleted else m.ref_kind,
        ref_id=None if m.deleted else m.ref_id,
        ref_title=None if m.deleted else m.ref_title,
        reply_to=m.reply_to_id, reply_text=_preview(replies.get(m.reply_to_id)) if m.reply_to_id else None,
        reaction=m.reaction, deleted=m.deleted, created_at=_aware(m.created_at),
    )


@router.get("", response_model=ChatOut)
async def history(
    before: datetime | None = None,
    limit: int = Query(50, ge=1, le=100),
    ctx: PairContext = Depends(active_pair),
    session: AsyncSession = Depends(get_session),
):
    q = select(ChatMessage).where(ChatMessage.pair_id == ctx.pair.id)
    if before is not None:
        q = q.where(ChatMessage.created_at < _aware(before))
    rows = (await session.execute(q.order_by(ChatMessage.created_at.desc()).limit(limit + 1))).scalars().all()
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    reply_ids = {m.reply_to_id for m in rows if m.reply_to_id}
    replies = {}
    if reply_ids:
        replies = {m.id: m for m in (await session.execute(
            select(ChatMessage).where(ChatMessage.id.in_(reply_ids))
        )).scalars()}
    return ChatOut(
        messages=[_out(m, ctx.user.id, replies) for m in rows],
        partner_read_at=await _read_at(session, ctx.pair.id, ctx.partner_id),
        has_more=has_more,
    )


@router.get("/unread")
async def unread(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)) -> dict:
    since = await _read_at(session, ctx.pair.id, ctx.user.id)
    q = select(func.count(ChatMessage.id)).where(
        ChatMessage.pair_id == ctx.pair.id, ChatMessage.from_id == ctx.partner_id, ChatMessage.deleted.is_(False))
    if since is not None:
        q = q.where(ChatMessage.created_at > since)
    return {"count": (await session.execute(q)).scalar_one()}


@router.post("", response_model=MessageOut, status_code=201)
async def send(body: MessageIn, ctx: PairContext = Depends(active_pair),
               session: AsyncSession = Depends(get_session)):
    text = (body.text or "").strip() or None
    if body.ref_kind is not None and body.ref_kind not in REF_KINDS:
        raise HTTPException(422, "Неизвестный тип вложения")
    if text is None and body.ref_kind is None:
        raise HTTPException(422, "Пустое сообщение")
    reply = None
    if body.reply_to:
        reply = await session.get(ChatMessage, body.reply_to)
        if reply is None or reply.pair_id != ctx.pair.id:
            raise HTTPException(404, "Сообщение для ответа не найдено")
    m = ChatMessage(pair_id=ctx.pair.id, from_id=ctx.user.id, text=text, ref_kind=body.ref_kind,
                    ref_id=body.ref_id, ref_title=(body.ref_title or None) if body.ref_kind else None,
                    reply_to_id=reply.id if reply else None)
    session.add(m)
    await _mark_read(session, ctx)  # своё сообщение = всё до него прочитано
    await session.commit()
    name = await display_name(session, ctx.user.id)
    # Не чаще одного уведомления в минуту на серию сообщений; текст сообщения в уведомление не кладём.
    minute = utcnow().strftime("%Y%m%d%H%M")
    await notify(session, ctx.partner_id, "Вечер", f"{name} написал(а) тебе", {"open": "chat"},
                 key=f"chat:{ctx.pair.id}:{ctx.partner_id}:{minute}")
    return _out(m, ctx.user.id, {reply.id: reply} if reply else {})


async def _mark_read(session: AsyncSession, ctx: PairContext) -> None:
    row = (await session.execute(
        select(ChatRead).where(ChatRead.pair_id == ctx.pair.id, ChatRead.user_id == ctx.user.id)
    )).scalar_one_or_none()
    if row is None:
        session.add(ChatRead(pair_id=ctx.pair.id, user_id=ctx.user.id, read_at=utcnow()))
    else:
        row.read_at = utcnow()


@router.post("/read")
async def mark_read(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)) -> dict:
    await _mark_read(session, ctx)
    await session.commit()
    return {"ok": True}


async def _message(session: AsyncSession, ctx: PairContext, message_id: str) -> ChatMessage:
    m = await session.get(ChatMessage, message_id)
    if m is None or m.pair_id != ctx.pair.id:
        raise HTTPException(404, "Сообщение не найдено")
    return m


@router.post("/{message_id}/react", response_model=MessageOut)
async def react(message_id: str, body: ReactIn, ctx: PairContext = Depends(active_pair),
                session: AsyncSession = Depends(get_session)):
    m = await _message(session, ctx, message_id)
    if m.from_id == ctx.user.id:
        raise HTTPException(403, "Реакцию ставят на сообщения партнёра")
    if body.emoji is not None and body.emoji not in REACTIONS:
        raise HTTPException(422, "Такой реакции нет")
    m.reaction = body.emoji
    await session.commit()
    return _out(m, ctx.user.id, {})


@router.delete("/{message_id}", response_model=MessageOut)
async def delete(message_id: str, ctx: PairContext = Depends(active_pair),
                 session: AsyncSession = Depends(get_session)):
    m = await _message(session, ctx, message_id)
    if m.from_id != ctx.user.id:
        raise HTTPException(403, "Удалить можно только своё сообщение")
    m.deleted, m.text, m.ref_title = True, None, None
    await session.commit()
    return _out(m, ctx.user.id, {})
