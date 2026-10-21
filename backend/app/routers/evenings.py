"""Shared, resumable occurrences of a mutually selected free plan.

The snapshot survives content edits, but it never bypasses current consent,
mutuality or content-level restrictions. Feedback belongs only to its author.
"""
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import PairContext, has_consent, special_pair
from ..models import Evening, EveningFeedback, utcnow
from ..pair_lock import pair_lock
from ..realtime import enqueue_invalidation, invalidate_pair
from .plans import _matched_plan, _require_unlocked, _votes
from .wishes import _allowed, _aware, _effective, _levels, match_for_card

router = APIRouter(tags=["evenings"])

Mode = Literal["together", "apart"]
StepStatus = Literal["pending", "done", "skipped"]
EveningStatus = Literal["active", "paused", "completed", "cancelled"]
Feeling = Literal["liked", "neutral", "not_for_me"]
Revision = Annotated[int, Field(strict=True, ge=1)]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartIn(StrictInput):
    client_id: UUID
    option: Annotated[int, Field(strict=True, ge=0)]
    mode: Mode


class RevisionIn(StrictInput):
    revision: Revision


class StepIn(RevisionIn):
    index: Annotated[int, Field(strict=True, ge=0)]
    status: StepStatus


class FeedbackIn(StrictInput):
    feeling: Feeling
    want_repeat: Annotated[bool, Field(strict=True)]


class StepOut(BaseModel):
    index: int
    text: str
    status: StepStatus


class EveningOut(BaseModel):
    id: str
    card_id: int
    title: str
    option_index: int
    option_title: str
    mode: Mode
    status: EveningStatus
    revision: int
    steps: list[StepOut]
    items: list[str]
    items_title: str | None
    note: str
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    my_feedback: FeedbackIn | None


class HistoryOut(BaseModel):
    items: list[EveningOut]
    next_before: str | None


async def _visible(session: AsyncSession, ctx: PairContext, card_id: int) -> bool:
    # A saved snapshot cannot resurrect a withdrawn match or a higher level.
    # Check both consents: the own consent is also checked by special_pair.
    if not await has_consent(session, ctx.partner_id, ("personal", "special")):
        return False
    found = await match_for_card(session, ctx, card_id)
    if found is None:
        return False
    card, _ = found
    mine, theirs = await _levels(session, ctx)
    return card.active and card.level in _allowed(_effective(mine, theirs))


async def _visible_evening(session: AsyncSession, ctx: PairContext, row: Evening) -> bool:
    if not await _visible(session, ctx, row.card_id):
        return False
    mine, theirs = await _levels(session, ctx)
    # Reclassifying a catalogue card must not expose an older, higher-level snapshot.
    return json.loads(row.snapshot)["level"] in _allowed(_effective(mine, theirs))


async def _get(session: AsyncSession, ctx: PairContext, evening_id: str) -> Evening:
    row = (await session.execute(
        select(Evening).where(Evening.id == evening_id, Evening.pair_id == ctx.pair.id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if row is None or not await _visible_evening(session, ctx, row):
        raise HTTPException(404, "Этот вечер сейчас недоступен")
    return row


async def _out(session: AsyncSession, ctx: PairContext, row: Evening) -> EveningOut:
    snapshot = json.loads(row.snapshot)
    progress = json.loads(row.progress)
    feedback = await session.get(EveningFeedback, (row.id, ctx.user.id))
    return EveningOut(
        id=row.id, card_id=row.card_id, title=snapshot["title"], option_index=row.option_index,
        option_title=snapshot["option_title"], mode=row.mode, status=row.status, revision=row.revision,
        steps=[StepOut(index=i, text=text, status=progress[i]) for i, text in enumerate(snapshot["steps"])],
        items=snapshot["items"], items_title=snapshot["items_title"], note=snapshot["note"],
        started_at=_aware(row.started_at), updated_at=_aware(row.updated_at),
        completed_at=_aware(row.completed_at) if row.completed_at else None,
        my_feedback=FeedbackIn(**json.loads(feedback.data)) if feedback else None,
    )


def _revision(row: Evening, body: RevisionIn) -> None:
    if row.revision != body.revision:
        raise HTTPException(409, "Вечер уже изменился. Обнови его и попробуй снова")


async def _changed(session: AsyncSession, ctx: PairContext, row: Evening) -> EveningOut:
    row.revision += 1
    row.updated_at = utcnow()
    await invalidate_pair(session, ctx, ["plans", "today", "us"])
    await session.commit()
    return await _out(session, ctx, row)


@router.post("/plans/{card_id}/start", response_model=EveningOut)
async def start(card_id: int, body: StartIn, ctx: PairContext = Depends(special_pair),
                session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        if not await _visible(session, ctx, card_id):
            raise HTTPException(404, "Этот план сейчас недоступен")
        # A retry has to reuse the same request and never creates another occurrence.
        previous = (await session.execute(select(Evening).where(
            Evening.pair_id == ctx.pair.id, Evening.client_id == str(body.client_id)
        ))).scalar_one_or_none()
        if previous:
            if (previous.card_id, previous.option_index, previous.mode) != (card_id, body.option, body.mode):
                raise HTTPException(409, "Этот запрос уже использован для другого вечера")
            if not await _visible_evening(session, ctx, previous):
                raise HTTPException(404, "Этот вечер сейчас недоступен")
            return await _out(session, ctx, previous)

        unlock = await _require_unlocked(session, ctx, card_id)
        card, _, plan = await _matched_plan(session, ctx, card_id)
        mine, theirs = await _votes(session, ctx, card_id)
        if mine != body.option or theirs != body.option or body.option >= len(plan["options"]):
            raise HTTPException(409, "Сначала выберите этот вариант вдвоём")

        current = (await session.execute(select(Evening).where(
            Evening.active_pair_id == ctx.pair.id
        ))).scalar_one_or_none()
        if current:
            if await _visible_evening(session, ctx, current):
                raise HTTPException(409, "У вас уже есть начатый вечер. Продолжите или завершите его")
            # A hidden/withdrawn plan must neither leak nor block future evenings.
            # Retire it only during this explicit mutation, never from a GET.
            current.status, current.active_pair_id = "cancelled", None
            current.revision += 1
            current.updated_at = current.completed_at = utcnow()
            await session.flush()

        option = plan["options"][body.option]
        snapshot = {
            "title": card.text, "option_title": option["title"], "steps": list(option.get("steps", [])),
            "items": list(option.get("items", [])), "items_title": option.get("items_title"),
            "note": option.get("note", ""), "level": card.level,
        }
        now = utcnow()
        row = Evening(
            pair_id=ctx.pair.id, active_pair_id=ctx.pair.id, client_id=str(body.client_id),
            card_id=card_id, started_by=ctx.user.id, option_index=body.option, mode=body.mode,
            status="active", revision=1, snapshot=json.dumps(snapshot, ensure_ascii=False),
            progress=json.dumps(["pending"] * len(snapshot["steps"])), started_at=now, updated_at=now,
        )
        # This occurrence consumes the existing appointment. Clearing it in
        # this transaction prevents an already-started plan staying "upcoming".
        # The idempotent branch above must not consume a later appointment.
        unlock.scheduled_at = None
        session.add(row)
        await invalidate_pair(session, ctx, ["plans", "today", "us", "wishes"])
        await session.commit()
        return await _out(session, ctx, row)


@router.get("/evenings/current", response_model=EveningOut | None)
async def current(ctx: PairContext = Depends(special_pair), session: AsyncSession = Depends(get_session)):
    row = (await session.execute(select(Evening).where(Evening.active_pair_id == ctx.pair.id))).scalar_one_or_none()
    if row is None or not await _visible_evening(session, ctx, row):
        return None
    return await _out(session, ctx, row)


@router.get("/evenings/history", response_model=HistoryOut)
async def history(limit: int = Query(20, ge=1, le=50), before: UUID | None = None,
                  ctx: PairContext = Depends(special_pair), session: AsyncSession = Depends(get_session)):
    anchor = None
    if before:
        # The cursor itself must belong to the current pair. A guessed UUID
        # cannot act as an oracle for another pair's timing or history.
        anchor = (await session.execute(select(Evening).where(
            Evening.id == str(before), Evening.pair_id == ctx.pair.id,
            Evening.status.in_(("completed", "cancelled"))
        ))).scalar_one_or_none()
        if anchor is None:
            raise HTTPException(404, "Страница истории сейчас недоступна")
    found = []
    # Keyset paging stays stable while new evenings are completed. Batched
    # filtering avoids returning hidden entries or truncating visible history.
    while len(found) <= limit:
        query = select(Evening).where(
            Evening.pair_id == ctx.pair.id, Evening.status.in_(("completed", "cancelled"))
        )
        if anchor:
            query = query.where(or_(Evening.started_at < anchor.started_at,
                                    and_(Evening.started_at == anchor.started_at, Evening.id < anchor.id)))
        rows = (await session.execute(query.order_by(Evening.started_at.desc(), Evening.id.desc()).limit(100))).scalars().all()
        if not rows:
            break
        for row in rows:
            if await _visible_evening(session, ctx, row):
                found.append(row)
                if len(found) > limit:
                    break
        anchor = rows[-1]
        if len(rows) < 100:
            break
    page = found[:limit]
    return HistoryOut(items=[await _out(session, ctx, row) for row in page],
                      next_before=page[-1].id if len(found) > limit else None)


@router.get("/evenings/{evening_id}", response_model=EveningOut)
async def detail(evening_id: UUID, ctx: PairContext = Depends(special_pair),
                 session: AsyncSession = Depends(get_session)):
    return await _out(session, ctx, await _get(session, ctx, str(evening_id)))


@router.post("/evenings/{evening_id}/step", response_model=EveningOut)
async def step(evening_id: UUID, body: StepIn, ctx: PairContext = Depends(special_pair),
               session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        row = await _get(session, ctx, str(evening_id))
        _revision(row, body)
        if row.status != "active":
            raise HTTPException(409, "Чтобы изменить шаг, сначала продолжи вечер")
        progress = json.loads(row.progress)
        if body.index >= len(progress):
            raise HTTPException(422, "В этом вечере нет такого шага")
        if progress[body.index] == body.status:
            return await _out(session, ctx, row)
        progress[body.index] = body.status
        row.progress = json.dumps(progress)
        return await _changed(session, ctx, row)


async def _transition(session: AsyncSession, ctx: PairContext, evening_id: UUID,
                      body: RevisionIn, target: EveningStatus) -> EveningOut:
    async with pair_lock(session, ctx.pair.id):
        row = await _get(session, ctx, str(evening_id))
        _revision(row, body)
        allowed = {"paused": ("active",), "active": ("paused",),
                   "completed": ("active", "paused"), "cancelled": ("active", "paused")}
        if row.status not in allowed[target]:
            raise HTTPException(409, "Это действие сейчас недоступно. Обнови вечер")
        row.status = target
        if target in ("completed", "cancelled"):
            row.active_pair_id = None
            row.completed_at = utcnow()
            if target == "completed":
                row.progress = json.dumps(["skipped" if item == "pending" else item
                                           for item in json.loads(row.progress)])
        return await _changed(session, ctx, row)


@router.post("/evenings/{evening_id}/pause", response_model=EveningOut)
async def pause(evening_id: UUID, body: RevisionIn, ctx: PairContext = Depends(special_pair),
                session: AsyncSession = Depends(get_session)):
    return await _transition(session, ctx, evening_id, body, "paused")


@router.post("/evenings/{evening_id}/resume", response_model=EveningOut)
async def resume(evening_id: UUID, body: RevisionIn, ctx: PairContext = Depends(special_pair),
                 session: AsyncSession = Depends(get_session)):
    return await _transition(session, ctx, evening_id, body, "active")


@router.post("/evenings/{evening_id}/complete", response_model=EveningOut)
async def complete(evening_id: UUID, body: RevisionIn, ctx: PairContext = Depends(special_pair),
                   session: AsyncSession = Depends(get_session)):
    return await _transition(session, ctx, evening_id, body, "completed")


@router.post("/evenings/{evening_id}/cancel", response_model=EveningOut)
async def cancel(evening_id: UUID, body: RevisionIn, ctx: PairContext = Depends(special_pair),
                 session: AsyncSession = Depends(get_session)):
    return await _transition(session, ctx, evening_id, body, "cancelled")


@router.post("/evenings/{evening_id}/feedback", response_model=EveningOut)
async def feedback(evening_id: UUID, body: FeedbackIn, ctx: PairContext = Depends(special_pair),
                   session: AsyncSession = Depends(get_session)):
    async with pair_lock(session, ctx.pair.id):
        row = await _get(session, ctx, str(evening_id))
        if row.status != "completed":
            raise HTTPException(409, "Оценку можно оставить после завершения вечера")
        own = await session.get(EveningFeedback, (row.id, ctx.user.id))
        data = json.dumps(body.model_dump())
        if own and own.data == data:
            return await _out(session, ctx, row)
        if own is None:
            session.add(EveningFeedback(evening_id=row.id, user_id=ctx.user.id, data=data))
        else:
            own.data, own.updated_at = data, utcnow()
        # Do not modify the shared revision/timestamps or notify the partner.
        await enqueue_invalidation(session, [ctx.user.id], ["plans"], pair_id=ctx.pair.id)
        await session.commit()
        return await _out(session, ctx, row)
