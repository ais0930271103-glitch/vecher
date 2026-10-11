"""Раздел «Мы»: памятка о партнёре, важные даты, напоминания."""
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.profile import PROFILE_KEYS, PROFILE_SECTIONS
from ..db import get_session
from ..deps import PairContext, active_pair, today_date
from ..models import PairDate, PartnerNote, ProfileFact, Reminder, User, utcnow
from ..push import display_name, notify

router = APIRouter(tags=["us"])


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    """Время с клиента → UTC (SQLite не хранит пояс). Время без пояса не принимаем."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise HTTPException(422, "Укажи время с часовым поясом")
    return dt.astimezone(timezone.utc)


# ================= Памятка =================

class FieldOut(BaseModel):
    key: str
    label: str
    hint: str
    value: str | None


class SectionOut(BaseModel):
    id: str
    title: str
    fields: list[FieldOut]


class NoteOut(BaseModel):
    id: str
    text: str
    created_at: datetime


class PersonOut(BaseModel):
    my_name: str | None
    partner_name: str | None
    me: list[SectionOut]          # что я рассказал о себе (редактирую)
    partner: list[SectionOut]     # что партнёр рассказал о себе — моя памятка
    partner_filled: int
    notes: list[NoteOut]          # мои личные заметки о партнёре (видны только мне)


class FactIn(BaseModel):
    key: str
    value: str = Field(default="", max_length=300)


class NameIn(BaseModel):
    name: str = Field(max_length=60)


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


async def _facts(session: AsyncSession, user_id: str) -> dict[str, str]:
    return dict((await session.execute(
        select(ProfileFact.key, ProfileFact.value).where(ProfileFact.user_id == user_id)
    )).all())


def _sections(values: dict[str, str]) -> list[SectionOut]:
    return [
        SectionOut(id=s["id"], title=s["title"], fields=[
            FieldOut(key=k, label=label, hint=hint, value=values.get(k)) for k, label, hint in s["fields"]
        ])
        for s in PROFILE_SECTIONS
    ]


@router.get("/person", response_model=PersonOut)
async def person(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    partner = await session.get(User, ctx.partner_id)
    mine, theirs = await _facts(session, ctx.user.id), await _facts(session, ctx.partner_id)
    notes = (await session.execute(
        select(PartnerNote)
        .where(PartnerNote.pair_id == ctx.pair.id, PartnerNote.author_id == ctx.user.id)
        .order_by(PartnerNote.created_at.desc())
    )).scalars().all()
    return PersonOut(
        my_name=ctx.user.display_name,
        partner_name=partner.display_name if partner else None,
        me=_sections(mine),
        partner=_sections(theirs),
        partner_filled=len(theirs),
        notes=[NoteOut(id=n.id, text=n.text, created_at=_aware(n.created_at)) for n in notes],
    )


@router.put("/person/me")
async def set_fact(body: FactIn, ctx: PairContext = Depends(active_pair),
                   session: AsyncSession = Depends(get_session)) -> dict:
    """Заполнить поле «о себе». Пустое значение — очистить."""
    if body.key not in PROFILE_KEYS:
        raise HTTPException(422, "Нет такого поля")
    value = body.value.strip()
    fact = (await session.execute(
        select(ProfileFact).where(ProfileFact.user_id == ctx.user.id, ProfileFact.key == body.key)
    )).scalar_one_or_none()
    if not value:
        if fact is not None:
            await session.delete(fact)
    elif fact is None:
        session.add(ProfileFact(user_id=ctx.user.id, key=body.key, value=value))
    else:
        fact.value, fact.updated_at = value, utcnow()
    await session.commit()
    return {"ok": True}


@router.put("/person/name")
async def set_name(body: NameIn, ctx: PairContext = Depends(active_pair),
                   session: AsyncSession = Depends(get_session)) -> dict:
    user = await session.get(User, ctx.user.id)
    user.display_name = body.name.strip() or None
    await session.commit()
    return {"ok": True, "name": user.display_name}


@router.post("/person/notes", response_model=NoteOut, status_code=201)
async def add_note(body: NoteIn, ctx: PairContext = Depends(active_pair),
                   session: AsyncSession = Depends(get_session)):
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Пустая заметка")
    note = PartnerNote(pair_id=ctx.pair.id, author_id=ctx.user.id, text=text, created_at=utcnow())
    session.add(note)
    await session.commit()
    return NoteOut(id=note.id, text=note.text, created_at=_aware(note.created_at))


@router.delete("/person/notes/{note_id}", status_code=204)
async def delete_note(note_id: str, ctx: PairContext = Depends(active_pair),
                      session: AsyncSession = Depends(get_session)):
    note = await session.get(PartnerNote, note_id)
    if note is None or note.author_id != ctx.user.id or note.pair_id != ctx.pair.id:
        raise HTTPException(404, "Заметка не найдена")
    await session.delete(note)
    await session.commit()
    return Response(status_code=204)


# ================= Важные даты =================

DATE_KINDS = ("met", "birthday", "anniversary", "other")


def next_occurrence(d: date, today: date, yearly: bool) -> date:
    if not yearly:
        return d

    def in_year(y: int) -> date:
        try:
            return d.replace(year=y)
        except ValueError:  # 29 февраля в невисокосный год
            return date(y, 2, 28)

    nxt = in_year(today.year)
    return nxt if nxt >= today else in_year(today.year + 1)


class DateIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    day: date
    kind: str = "other"
    yearly: bool = True


class DateOut(BaseModel):
    id: str
    title: str
    day: date
    kind: str
    yearly: bool
    next: date
    days_left: int   # 0 — сегодня; отрицательное — разовая дата уже прошла
    years: int | None  # сколько лет исполнится в ближайшую дату


class DatesOut(BaseModel):
    together_days: int | None  # от даты знакомства
    items: list[DateOut]       # ближайшие сначала, прошедшие разовые — в конце


@router.get("/dates", response_model=DatesOut)
async def dates(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session),
                today: date = Depends(today_date)):
    rows = (await session.execute(select(PairDate).where(PairDate.pair_id == ctx.pair.id))).scalars().all()
    items = []
    together = None
    for r in rows:
        nxt = next_occurrence(r.day, today, r.yearly)
        if r.kind == "met" and r.day <= today:
            together = (today - r.day).days
        items.append(DateOut(
            id=r.id, title=r.title, day=r.day, kind=r.kind, yearly=r.yearly, next=nxt,
            days_left=(nxt - today).days,
            years=(nxt.year - r.day.year) if r.yearly and nxt.year > r.day.year else None,
        ))
    items.sort(key=lambda i: (i.days_left < 0, abs(i.days_left)))
    return DatesOut(together_days=together, items=items)


@router.post("/dates", response_model=DatesOut, status_code=201)
async def add_date(body: DateIn, ctx: PairContext = Depends(active_pair),
                   session: AsyncSession = Depends(get_session), today: date = Depends(today_date)):
    if body.kind not in DATE_KINDS:
        raise HTTPException(422, "Тип даты: met, birthday, anniversary или other")
    if body.kind == "met":
        exists = (await session.execute(
            select(PairDate.id).where(PairDate.pair_id == ctx.pair.id, PairDate.kind == "met")
        )).scalar_one_or_none()
        if exists:
            raise HTTPException(409, "Дата знакомства уже есть — удали её, чтобы указать другую")
    session.add(PairDate(pair_id=ctx.pair.id, title=body.title.strip(), day=body.day, kind=body.kind,
                         yearly=body.yearly, created_by=ctx.user.id))
    await session.commit()
    return await dates(ctx, session, today)


@router.delete("/dates/{date_id}", status_code=204)
async def delete_date(date_id: str, ctx: PairContext = Depends(active_pair),
                      session: AsyncSession = Depends(get_session)):
    row = await session.get(PairDate, date_id)
    if row is None or row.pair_id != ctx.pair.id:
        raise HTTPException(404, "Дата не найдена")
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)


# ================= Напоминания =================

TARGETS = ("me", "partner", "both")


class ReminderIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    target: str = "me"
    due_at: datetime | None = None


class ReminderOut(BaseModel):
    id: str
    text: str
    due_at: datetime | None
    done: bool
    mine: bool      # я автор
    for_me: bool    # касается меня
    who: str        # подпись: «Себе», «Партнёру», «Обоим», «От партнёра», «Нам обоим»


def _visible(r: Reminder, ctx: PairContext) -> bool:
    return r.author_id == ctx.user.id or (r.author_id == ctx.partner_id and r.target in ("partner", "both"))


def _out(r: Reminder, ctx: PairContext) -> ReminderOut:
    mine = r.author_id == ctx.user.id
    if mine:
        who = {"me": "Себе", "partner": "Партнёру", "both": "Обоим"}[r.target]
        for_me = r.target in ("me", "both")
    else:
        who = "От партнёра" if r.target == "partner" else "Нам обоим"
        for_me = True
    return ReminderOut(id=r.id, text=r.text, due_at=_aware(r.due_at), done=r.done_at is not None,
                       mine=mine, for_me=for_me, who=who)


@router.get("/reminders", response_model=list[ReminderOut])
async def reminders(ctx: PairContext = Depends(active_pair), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Reminder).where(Reminder.pair_id == ctx.pair.id))).scalars().all()
    visible = [r for r in rows if _visible(r, ctx)]
    far = datetime.max.replace(tzinfo=timezone.utc)
    visible.sort(key=lambda r: (r.done_at is not None, _aware(r.due_at) or far, _aware(r.created_at)))
    return [_out(r, ctx) for r in visible]


@router.post("/reminders", response_model=ReminderOut, status_code=201)
async def add_reminder(body: ReminderIn, ctx: PairContext = Depends(active_pair),
                       session: AsyncSession = Depends(get_session)):
    if body.target not in TARGETS:
        raise HTTPException(422, "Кому: me, partner или both")
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Пустое напоминание")
    r = Reminder(pair_id=ctx.pair.id, author_id=ctx.user.id, target=body.target, text=text,
                 due_at=_utc(body.due_at), created_at=utcnow())
    session.add(r)
    await session.commit()
    if r.target in ("partner", "both"):
        name = await display_name(session, ctx.user.id)
        await notify(session, ctx.partner_id, "Вечер", f"{name}: новое напоминание — {text}",
                     {"open": "reminders"}, key=f"reminder-new:{r.id}")
    return _out(r, ctx)


async def _get_visible(session: AsyncSession, ctx: PairContext, reminder_id: str) -> Reminder:
    r = await session.get(Reminder, reminder_id)
    if r is None or r.pair_id != ctx.pair.id or not _visible(r, ctx):
        raise HTTPException(404, "Напоминание не найдено")
    return r


@router.post("/reminders/{reminder_id}/done", response_model=ReminderOut)
async def toggle_done(reminder_id: str, ctx: PairContext = Depends(active_pair),
                      session: AsyncSession = Depends(get_session)):
    r = await _get_visible(session, ctx, reminder_id)
    r.done_at = None if r.done_at else utcnow()
    await session.commit()
    return _out(r, ctx)


@router.delete("/reminders/{reminder_id}", status_code=204)
async def delete_reminder(reminder_id: str, ctx: PairContext = Depends(active_pair),
                          session: AsyncSession = Depends(get_session)):
    r = await _get_visible(session, ctx, reminder_id)
    if r.author_id != ctx.user.id:
        raise HTTPException(403, "Удалить может только автор")
    await session.delete(r)
    await session.commit()
    return Response(status_code=204)
