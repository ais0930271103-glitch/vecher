"""Экран «Сегодня»: вопрос дня, радар, огонёк пары."""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import distinct, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..content.questions import QUESTIONS
from ..db import get_session
from ..deps import PairContext, active_pair, today_date
from ..models import Activity, DailyAnswer, Question, RadarSignal

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


class TodayOut(BaseModel):
    day: date
    question: QuestionOut
    my_answer: str | None
    partner_answered: bool
    partner_answer: str | None  # открывается только после собственного ответа
    streak: int
    today_complete: bool
    radar_incoming: list[RadarOut]


class AnswerIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class RadarIn(BaseModel):
    kind: str


# ---------- routes ----------

@router.get("/today", response_model=TodayOut)
async def get_today(
    ctx: PairContext = Depends(active_pair),
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
    )


@router.post("/today/answer", response_model=TodayOut)
async def answer_today(
    body: AnswerIn,
    ctx: PairContext = Depends(active_pair),
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
        raise HTTPException(status.HTTP_409_CONFLICT, "Сегодня вы уже ответили")
    q = await question_for_day(session, ctx.pair.id, day)
    session.add(DailyAnswer(pair_id=ctx.pair.id, user_id=ctx.user.id, day=day, question_id=q.id, text=text))
    await mark_activity(session, ctx, day)
    await session.commit()
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
