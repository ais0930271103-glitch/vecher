"""Экран «Сегодня»: вопрос дня, радар, огонёк пары."""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import distinct, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..content.moods import MOODS
from ..content.questions import QUESTIONS
from ..db import get_session
from ..deps import PairContext, active_pair, prep_pair, today_date
from ..models import Activity, DailyAnswer, DailyMood, Question, RadarSignal, WishSwipe, utcnow
from ..push import display_name, notify

router = APIRouter(tags=["today"])

RADAR_KINDS: dict[str, str] = {
    "thinking": "Думаю о тебе",
    "miss": "Скучаю",
    "plans": "Вечером есть планы…",
    "hug": "Обнимаю",
}


# ---------- helpers ----------

async def ensure_questions(session: AsyncSession) -> None:
    """Заливает стартовые вопросы, если таблица пуста (первый запуск, тесты)."""
    if (await session.execute(select(func.count(Question.id)))).scalar_one() == 0:
        session.add_all(Question(category=c, text=t) for c, t in QUESTIONS)
        await session.flush()


async def question_for_day(session: AsyncSession, pair_id: str, day: date) -> Question:
    """Один и тот же вопрос для обоих партнёров; у разных пар — разные вопросы в один день."""
    existing = (await session.execute(
        select(DailyAnswer.question_id).where(DailyAnswer.pair_id == pair_id, DailyAnswer.day == day).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return await session.get(Question, existing)
    await ensure_questions(session)
    ids = (await session.execute(
        select(Question.id).where(Question.active.is_(True)).order_by(Question.id)
    )).scalars().all()
    offset = int(pair_id.replace("-", "")[:8], 16)
    return await session.get(Question, ids[(day.toordinal() + offset) % len(ids)])


async def mark_activity(session: AsyncSession, ctx: PairContext, day: date) -> None:
    exists = (await session.execute(
        select(Activity.id).where(
            Activity.pair_id == ctx.pair.id, Activity.user_id == ctx.user.id, Activity.day == day
        )
    )).scalar_one_or_none()
    if exists is None:
        session.add(Activity(pair_id=ctx.pair.id, user_id=ctx.user.id, day=day))


async def pair_streak(session: AsyncSession, pair_id: str, today: date) -> tuple[int, bool]:
    """Огонёк: сколько дней подряд что-то сделали ОБА партнёра.

    Если сегодня ещё не все отметились, серия считается до вчера — она не сгорает днём.
    """
    rows = (await session.execute(
        select(Activity.day, func.count(distinct(Activity.user_id)))
        .where(Activity.pair_id == pair_id, Activity.day >= today - timedelta(days=400))
        .group_by(Activity.day)
    )).all()
    full_days = {d for d, users in rows if users >= 2}
    today_complete = today in full_days
    cursor = today if today_complete else today - timedelta(days=1)
    streak = 0
    while cursor in full_days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak, today_complete


# ---------- schemas ----------

class QuestionOut(BaseModel):
    id: int
    text: str
    category: str


class RadarOut(BaseModel):
    id: str
    kind: str
    label: str
    created_at: datetime


class MoodOut(BaseModel):
    kind: str
    label: str
    hint: str | None = None  # только для настроения партнёра: как поддержать


class MoodOption(BaseModel):
    kind: str
    label: str


class MoodIn(BaseModel):
    kind: str | None  # None — убрать


def _mood(kind: str | None, day: date, with_hint: bool) -> MoodOut | None:
    if kind not in MOODS:
        return None
    m = MOODS[kind]
    hint = m["hints"][day.toordinal() % len(m["hints"])] if with_hint else None
    return MoodOut(kind=kind, label=m["label"], hint=hint)


class TodayOut(BaseModel):
    day: date
    question: QuestionOut
    my_answer: str | None
    partner_answered: bool
    partner_answer: str | None  # открывается только после собственного ответа
    streak: int
    today_complete: bool
    radar_incoming: list[RadarOut]
    my_mood: MoodOut | None = None
    partner_mood: MoodOut | None = None
    mood_options: list[MoodOption] = []
    my_swiped: int = 0       # сколько карточек я пролистал(а)
    partner_swiped: int = 0  # и партнёр — для подсказки «у вас могут быть совпадения» (без самих выборов)


class AnswerIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class RadarIn(BaseModel):
    kind: str


# ---------- routes ----------

@router.get("/today", response_model=TodayOut)
async def get_today(
    ctx: PairContext = Depends(prep_pair),
    session: AsyncSession = Depends(get_session),
    day: date = Depends(today_date),
) -> TodayOut:
    q = await question_for_day(session, ctx.pair.id, day)
    answers = {
        a.user_id: a.text
        for a in (await session.execute(
            select(DailyAnswer).where(DailyAnswer.pair_id == ctx.pair.id, DailyAnswer.day == day)
        )).scalars()
    }
    mine = answers.get(ctx.user.id)
    partners = answers.get(ctx.partner_id)
    incoming = (await session.execute(
        select(RadarSignal)
        .where(RadarSignal.pair_id == ctx.pair.id, RadarSignal.from_user_id == ctx.partner_id,
               RadarSignal.seen_at.is_(None))
        .order_by(RadarSignal.created_at.desc())
        .limit(10)
    )).scalars().all()
    moods = {
        m.user_id: m.kind
        for m in (await session.execute(
            select(DailyMood).where(DailyMood.pair_id == ctx.pair.id, DailyMood.day == day)
        )).scalars()
    }
    swiped = dict((await session.execute(
        select(WishSwipe.user_id, func.count(WishSwipe.id)).where(WishSwipe.pair_id == ctx.pair.id)
        .group_by(WishSwipe.user_id)
    )).all())
    streak, complete = await pair_streak(session, ctx.pair.id, day)
    await session.commit()  # ensure_questions мог залить стартовые вопросы
    return TodayOut(
        day=day,
        question=QuestionOut(id=q.id, text=q.text, category=q.category),
        my_answer=mine,
        partner_answered=partners is not None,
        partner_answer=partners if mine is not None else None,
        streak=streak,
        today_complete=complete,
        radar_incoming=[
            RadarOut(id=r.id, kind=r.kind, label=RADAR_KINDS.get(r.kind, r.kind), created_at=r.created_at)
            for r in incoming
        ],
        my_mood=_mood(moods.get(ctx.user.id), day, with_hint=False),
        partner_mood=_mood(moods.get(ctx.partner_id), day, with_hint=True),
        mood_options=[MoodOption(kind=k, label=v["label"]) for k, v in MOODS.items()],
        my_swiped=swiped.get(ctx.user.id, 0),
        partner_swiped=swiped.get(ctx.partner_id, 0),
    )


@router.post("/today/answer", response_model=TodayOut)
async def answer_today(
    body: AnswerIn,
    ctx: PairContext = Depends(prep_pair),
    session: AsyncSession = Depends(get_session),
    day: date = Depends(today_date),
) -> TodayOut:
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Ответ пустой")
    already = (await session.execute(
        select(DailyAnswer.id).where(
            DailyAnswer.pair_id == ctx.pair.id, DailyAnswer.user_id == ctx.user.id, DailyAnswer.day == day
        )
    )).scalar_one_or_none()
    if already is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сегодня ты уже ответил(а)")
    q = await question_for_day(session, ctx.pair.id, day)
    session.add(DailyAnswer(pair_id=ctx.pair.id, user_id=ctx.user.id, day=day, question_id=q.id, text=text))
    await mark_activity(session, ctx, day)
    await session.commit()
    name = await display_name(session, ctx.user.id)
    if ctx.partner_id:
        await notify(session, ctx.partner_id, "Вечер", f"{name} ответил(а) на вопрос дня",
                 {"open": "today"}, key=f"answer:{ctx.pair.id}:{day}:{ctx.user.id}")
    return await get_today(ctx, session, day)


@router.post("/radar", status_code=201)
async def send_radar(
    body: RadarIn,
    ctx: PairContext = Depends(active_pair),
    session: AsyncSession = Depends(get_session),
    day: date = Depends(today_date),
) -> dict:
    if body.kind not in RADAR_KINDS:
        raise HTTPException(422, "Неизвестный сигнал")
    sent_today = (await session.execute(
        select(func.count(RadarSignal.id)).where(
            RadarSignal.from_user_id == ctx.user.id, RadarSignal.day == day
        )
    )).scalar_one()
    if sent_today >= get_settings().radar_daily_limit:
        raise HTTPException(429, "На сегодня сигналов достаточно")
    session.add(RadarSignal(pair_id=ctx.pair.id, from_user_id=ctx.user.id, kind=body.kind, day=day))
    await mark_activity(session, ctx, day)
    await session.commit()
    name = await display_name(session, ctx.user.id)
    await notify(session, ctx.partner_id, "Вечер", f"{name}: «{RADAR_KINDS[body.kind]}»", {"open": "today"})
    return {"ok": True, "label": RADAR_KINDS[body.kind]}


@router.post("/radar/seen")
async def radar_seen(
    ctx: PairContext = Depends(active_pair),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Отмечает все входящие сигналы партнёра как просмотренные."""
    await session.execute(
        update(RadarSignal)
        .where(RadarSignal.pair_id == ctx.pair.id, RadarSignal.from_user_id == ctx.partner_id,
               RadarSignal.seen_at.is_(None))
        .values(seen_at=datetime.now(timezone.utc))
    )
    await session.commit()
    return {"ok": True}


@router.put("/today/mood", response_model=TodayOut)
async def set_mood(
    body: MoodIn,
    ctx: PairContext = Depends(active_pair),
    session: AsyncSession = Depends(get_session),
    day: date = Depends(today_date),
) -> TodayOut:
    """«Состояние сегодня». Партнёр видит его и подсказку, как поддержать; завтра — с чистого листа."""
    if body.kind is not None and body.kind not in MOODS:
        raise HTTPException(422, "Неизвестное состояние")
    row = (await session.execute(
        select(DailyMood).where(DailyMood.pair_id == ctx.pair.id, DailyMood.user_id == ctx.user.id,
                                DailyMood.day == day)
    )).scalar_one_or_none()
    if body.kind is None:
        if row is not None:
            await session.delete(row)
        await session.commit()
        return await get_today(ctx, session, day)
    if row is None:
        session.add(DailyMood(pair_id=ctx.pair.id, user_id=ctx.user.id, day=day, kind=body.kind))
    else:
        row.kind, row.updated_at = body.kind, utcnow()
    await mark_activity(session, ctx, day)
    await session.commit()
    # В уведомлении — без самого состояния (телефон может лежать экраном вверх). Раз в день.
    name = await display_name(session, ctx.user.id)
    await notify(session, ctx.partner_id, "Вечер", f"{name} поделился(ась) настроением дня",
                 {"open": "today"}, key=f"mood:{ctx.pair.id}:{day}:{ctx.user.id}")
    return await get_today(ctx, session, day)
