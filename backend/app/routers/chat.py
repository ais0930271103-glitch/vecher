"""Чат пары: текст, приватные файлы, «Обсудить», ответы, реакции и прочитано.

Клиент получает историю через REST; realtime сообщает об изменениях, push — о новых сообщениях.
Сигналинг приватных видеозвонков находится в routers/calls.py.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import PairContext, active_pair
from ..models import ChatAttachment, ChatMessage, ChatRead, ChatSendKey, utcnow
from ..attachments import AttachmentOut, _expired, attachment_out, delete_attachments
from ..pair_lock import pair_lock
from ..push import display_name, notify
from ..realtime import invalidate_pair
from ..video_catalogue import find_video

router = APIRouter(prefix="/chat", tags=["chat"])

REF_KINDS = ("match", "plan", "invite", "letter", "video")
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
    attachment_ids: list[str] | None = Field(default=None, max_length=5)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=64)


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
    attachments: list[AttachmentOut] = Field(default_factory=list)
    client_message_id: str | None = None


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


def _out(m: ChatMessage, me: str, replies: dict[str, ChatMessage],
         attachments: list[ChatAttachment] | None = None, client_message_id: str | None = None) -> MessageOut:
    return MessageOut(
        id=m.id, mine=m.from_id == me,
        text=None if m.deleted else m.text,
        ref_kind=None if m.deleted else m.ref_kind,
        ref_id=None if m.deleted else m.ref_id,
        ref_title=None if m.deleted else m.ref_title,
        reply_to=m.reply_to_id, reply_text=_preview(replies.get(m.reply_to_id)) if m.reply_to_id else None,
        reaction=m.reaction, deleted=m.deleted, created_at=_aware(m.created_at),
        attachments=[attachment_out(a) for a in (attachments or [])] if not m.deleted else [],
        client_message_id=client_message_id if m.from_id == me else None,
    )


async def _message_out(session: AsyncSession, m: ChatMessage, me: str) -> MessageOut:
    files = (await session.execute(select(ChatAttachment).where(ChatAttachment.message_id == m.id,
                                                               ChatAttachment.pair_id == m.pair_id))).scalars().all()
    key = (await session.execute(select(ChatSendKey.client_id).where(
        ChatSendKey.message_id == m.id, ChatSendKey.from_id == me))).scalar_one_or_none()
    reply = await session.get(ChatMessage, m.reply_to_id) if m.reply_to_id else None
    replies = {reply.id: reply} if reply and reply.pair_id == m.pair_id else {}
    return _out(m, me, replies, list(files), key)


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
            select(ChatMessage).where(ChatMessage.id.in_(reply_ids), ChatMessage.pair_id == ctx.pair.id)
        )).scalars()}
    files: dict[str, list[ChatAttachment]] = {}
    keys: dict[str, str] = {}
    if rows:
        ids = [m.id for m in rows]
        for file in (await session.execute(select(ChatAttachment).where(
            ChatAttachment.pair_id == ctx.pair.id, ChatAttachment.message_id.in_(ids)))).scalars():
            files.setdefault(file.message_id, []).append(file)
        keys = dict((await session.execute(select(ChatSendKey.message_id, ChatSendKey.client_id).where(
            ChatSendKey.message_id.in_(ids), ChatSendKey.from_id == ctx.user.id))).all())
    return ChatOut(
        messages=[_out(m, ctx.user.id, replies, files.get(m.id), keys.get(m.id)) for m in rows],
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
    attachment_ids = body.attachment_ids or []
    if len(set(attachment_ids)) != len(attachment_ids):
        raise HTTPException(422, "Файл указан дважды")
    if text is None and body.ref_kind is None and not attachment_ids:
        raise HTTPException(422, "Пустое сообщение")
    async with pair_lock(session, ctx.pair.id):
        if body.client_message_id:
            previous = await session.get(ChatSendKey, (ctx.pair.id, ctx.user.id, body.client_message_id))
            if previous:
                old = await session.get(ChatMessage, previous.message_id)
                out = await _message_out(session, old, ctx.user.id)
                await session.commit()
                return out
        ref_title = (body.ref_title or None) if body.ref_kind else None
        if body.ref_kind == "video":
            video = find_video(body.ref_id)
            if video is None:
                raise HTTPException(404, "Этот ролик сейчас недоступен")
            ref_title = video.title
        reply = None
        if body.reply_to:
            reply = await session.get(ChatMessage, body.reply_to)
            if reply is None or reply.pair_id != ctx.pair.id:
                raise HTTPException(404, "Сообщение для ответа не найдено")
        files = []
        for file_id in attachment_ids:
            file = await session.get(ChatAttachment, file_id)
            if (file is None or file.pair_id != ctx.pair.id or file.from_id != ctx.user.id or _expired(file)):
                raise HTTPException(404, "Файл не найден — прикрепи его заново")
            if file.message_id is not None:
                raise HTTPException(409, "Файл уже отправлен")
            files.append(file)
        m = ChatMessage(pair_id=ctx.pair.id, from_id=ctx.user.id, text=text, ref_kind=body.ref_kind,
                        ref_id=body.ref_id, ref_title=ref_title,
                        reply_to_id=reply.id if reply else None)
        session.add(m)
        await session.flush()
        for file in files:
            file.message_id = m.id
        if body.client_message_id:
            session.add(ChatSendKey(pair_id=ctx.pair.id, from_id=ctx.user.id,
                                    client_id=body.client_message_id, message_id=m.id))
        await _mark_read(session, ctx)  # своё сообщение = всё до него прочитано
        name = await display_name(session, ctx.user.id)
        # Не чаще одного push в минуту на серию; текст сообщения остаётся в REST.
        minute = utcnow().strftime("%Y%m%d%H%M")
        await notify(session, ctx.partner_id, "Вечер", f"{name} написал(а) тебе", {"open": "chat"},
                     key=f"chat:{ctx.pair.id}:{ctx.partner_id}:{minute}", pair_id=ctx.pair.id)
        await invalidate_pair(session, ctx, ["chat"])
        await session.commit()
        out = _out(m, ctx.user.id, {reply.id: reply} if reply else {}, files, body.client_message_id)
    return out


async def _mark_read(session: AsyncSession, ctx: PairContext) -> bool:
    # Advance to a known incoming message, not the clock. Repeated REST refreshes
    # must not produce a new read receipt and another invalidation indefinitely.
    latest = (await session.execute(select(func.max(ChatMessage.created_at)).where(
        ChatMessage.pair_id == ctx.pair.id, ChatMessage.from_id == ctx.partner_id
    ))).scalar_one_or_none()
    if latest is None:
        return False
    row = (await session.execute(
        select(ChatRead).where(ChatRead.pair_id == ctx.pair.id, ChatRead.user_id == ctx.user.id)
    )).scalar_one_or_none()
    if row is not None and _aware(row.read_at) >= _aware(latest):
        return False
    if row is None:
        session.add(ChatRead(pair_id=ctx.pair.id, user_id=ctx.user.id, read_at=latest))
    else:
        row.read_at = latest
    return True


@router.post("/read")
async def mark_read(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)) -> dict:
    async with pair_lock(session, ctx.pair.id):
        if await _mark_read(session, ctx):
            await invalidate_pair(session, ctx, ["chat"])
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
    await invalidate_pair(session, ctx, ["chat"])
    await session.commit()
    return await _message_out(session, m, ctx.user.id)


@router.delete("/{message_id}", response_model=MessageOut)
async def delete(message_id: str, ctx: PairContext = Depends(active_pair),
                 session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        m = await _message(session, ctx, message_id)
        if m.from_id != ctx.user.id:
            raise HTTPException(403, "Удалить можно только своё сообщение")
        m.deleted, m.text, m.ref_title = True, None, None
        await delete_attachments(session, ChatAttachment.message_id == m.id, ChatAttachment.pair_id == ctx.pair.id)
        await invalidate_pair(session, ctx, ["chat"])
        await session.commit()
    return _out(m, ctx.user.id, {})
