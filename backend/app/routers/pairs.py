import secrets
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..deps import PairContext, active_pair, consented_user, prep_pair, today_date
from ..erase import erase_pair
from ..models import DailyAnswer, Letter, Pair, PairMember, PairPause, ProfileFact, User, WishSwipe
from ..security import current_user
from ..push import notify
from ..pair_lock import pair_lock

router = APIRouter(prefix="/pairs", tags=["pairs"])

# Без похожих символов (0/O, 1/I/L), чтобы код было легко продиктовать.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_invite_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(8))


class PairOut(BaseModel):
    pair_id: str
    status: str
    invite_code: str | None
    invite_url: str | None = None
    partner_id: str | None
    my_name: str | None = None
    partner_name: str | None = None
    paused: bool = False
    paused_by_me: bool = False
    paused_since: datetime | None = None


class PauseIn(BaseModel):
    on: bool


class JoinIn(BaseModel):
    code: str


class InvitePreview(BaseModel):
    inviter_name: str | None


def _normalize(code: str) -> str:
    return code.strip().upper().replace("-", "").replace(" ", "")


async def _membership(session: AsyncSession, user_id: str) -> PairMember | None:
    return (await session.execute(
        select(PairMember).where(PairMember.user_id == user_id)
    )).scalar_one_or_none()


async def _out(session: AsyncSession, pair: Pair, me: str) -> PairOut:
    partner = (await session.execute(
        select(PairMember.user_id).where(PairMember.pair_id == pair.id, PairMember.user_id != me)
    )).scalar_one_or_none()
    me_user = await session.get(User, me)
    partner_user = await session.get(User, partner) if partner else None
    pause = (await session.execute(select(PairPause).where(PairPause.pair_id == pair.id))).scalar_one_or_none()
    url = f"{get_settings().public_url.rstrip('/')}/i/{pair.invite_code}" if pair.invite_code else None
    return PairOut(pair_id=pair.id, status=pair.status, invite_code=pair.invite_code, invite_url=url, partner_id=partner,
                   my_name=me_user.display_name if me_user else None,
                   partner_name=partner_user.display_name if partner_user else None,
                   paused=pause is not None, paused_by_me=bool(pause and pause.by_user_id == me),
                   paused_since=pause.since if pause else None)


@router.get("/me", response_model=PairOut | None)
async def my_pair(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    m = await _membership(session, user.id)
    if m is None:
        return None
    return await _out(session, await session.get(Pair, m.pair_id), user.id)


@router.post("/invite", response_model=PairOut)
async def create_invite(user: User = Depends(consented_user), session: AsyncSession = Depends(get_session)):
    """Создаёт пару и код приглашения (или возвращает уже созданный)."""
    m = await _membership(session, user.id)
    if m is not None:
        pair = await session.get(Pair, m.pair_id)
        if pair.status == "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "Ты уже в паре")
        return await _out(session, pair, user.id)
    pair = Pair(invite_code=new_invite_code())
    session.add(pair)
    await session.flush()
    session.add(PairMember(pair_id=pair.id, user_id=user.id, role="initiator"))
    await session.commit()
    return await _out(session, pair, user.id)


@router.post("/join", response_model=PairOut)
async def join(body: JoinIn, user: User = Depends(consented_user), session: AsyncSession = Depends(get_session)):
    code = _normalize(body.code)
    pair = (await session.execute(select(Pair).where(Pair.invite_code == code))).scalar_one_or_none()
    if pair is None or pair.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Приглашение не найдено или уже использовано")
    async with pair_lock(session, pair.id, active=False) as pair:
        if pair.status != "pending" or pair.invite_code != code:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Приглашение не найдено или уже использовано")
        if await _membership(session, user.id) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Ты уже в паре или создал(а) своё приглашение")
        session.add(PairMember(pair_id=pair.id, user_id=user.id, role="invited"))
        pair.status = "active"
        pair.invite_code = None  # одноразовый код, захвачен под блокировкой пары
        await session.commit()
    inviter = (await session.execute(
        select(PairMember.user_id).where(PairMember.pair_id == pair.id, PairMember.user_id != user.id)
    )).scalar_one_or_none()
    if inviter:
        name = user.display_name or "Партнёр"
        await notify(session, inviter, "Вечер", f"{name} с тобой — «Вечер» открыт для вас двоих",
                     {"open": "today"}, key=f"joined:{pair.id}")
    return await _out(session, pair, user.id)


@router.post("/pause", response_model=PairOut)
async def pause(body: PauseIn, ctx: PairContext = Depends(active_pair),
                session: AsyncSession = Depends(get_session)):
    """Пауза: уведомления друг другу не приходят, всё остальное сохраняется. Снимает тот, кто поставил."""
    row = (await session.execute(select(PairPause).where(PairPause.pair_id == ctx.pair.id))).scalar_one_or_none()
    name = ctx.user.display_name or "Партнёр"
    if body.on and row is None:
        session.add(PairPause(pair_id=ctx.pair.id, by_user_id=ctx.user.id))
        await session.commit()
        await notify(session, ctx.partner_id, "Вечер",
                     f"{name} поставил(а) «Вечер» на паузу. Всё сохранится — вернётесь, когда будете готовы",
                     {"open": "today"}, force=True)
    elif not body.on and row is not None:
        if row.by_user_id != ctx.user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Паузу снимает тот, кто её поставил")
        await session.delete(row)
        await session.commit()
        await notify(session, ctx.partner_id, "Вечер", f"{name} снял(а) паузу — «Вечер» снова с вами",
                     {"open": "today"})
    return await _out(session, ctx.pair, ctx.user.id)


@router.post("/leave")
async def leave(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict:
    """Завершить пару. Общее (ответы, совпадения, планы, даты, письма) удаляется у обоих,
    партнёр получает уведомление. Анкета «о себе» и тихие желания остаются у каждого."""
    m = await _membership(session, user.id)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ты не в паре")
    partner = (await session.execute(
        select(PairMember.user_id).where(PairMember.pair_id == m.pair_id, PairMember.user_id != user.id)
    )).scalar_one_or_none()
    async with pair_lock(session, m.pair_id, active=False):
        await erase_pair(session, m.pair_id)
        await session.commit()
    if partner:
        name = user.display_name or "Партнёр"
        await notify(session, partner, "Вечер", f"{name} завершил(а) вашу пару в «Вечере»", {"open": "today"},
                     force=True)
    return {"ok": True}


@router.get("/invite/{code}", response_model=InvitePreview)
async def preview_invite(code: str, user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)):
    """Кто приглашает — для экрана «Аня приглашает тебя» (по ссылке или коду). Только имя."""
    pair = (await session.execute(select(Pair).where(Pair.invite_code == _normalize(code)))).scalar_one_or_none()
    if pair is None or pair.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Приглашение не найдено или уже использовано")
    inviter = (await session.execute(
        select(User).join(PairMember, PairMember.user_id == User.id).where(PairMember.pair_id == pair.id)
    )).scalars().first()
    if inviter is not None and inviter.id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Это твоё собственное приглашение")
    return InvitePreview(inviter_name=inviter.display_name if inviter else None)


class PrepOut(BaseModel):
    swiped: int        # карточек пролистано
    about_me: int      # полей «о себе» заполнено
    answered: bool     # ответ на вопрос дня есть
    letter: bool       # письмо партнёру написано
    done: int          # шагов из 4 выполнено


@router.get("/prep", response_model=PrepOut)
async def prep(ctx: PairContext = Depends(prep_pair), session: AsyncSession = Depends(get_session),
               day: date = Depends(today_date)):
    """«Подготовь „Вечер“ для вас двоих» — пока партнёр не присоединился."""
    swiped = (await session.execute(select(func.count(WishSwipe.id)).where(
        WishSwipe.pair_id == ctx.pair.id, WishSwipe.user_id == ctx.user.id))).scalar_one()
    about = (await session.execute(select(func.count(ProfileFact.id)).where(
        ProfileFact.user_id == ctx.user.id))).scalar_one()
    answered = (await session.execute(select(DailyAnswer.id).where(
        DailyAnswer.pair_id == ctx.pair.id, DailyAnswer.user_id == ctx.user.id, DailyAnswer.day == day))).first() is not None
    letter = (await session.execute(select(Letter.id).where(
        Letter.pair_id == ctx.pair.id, Letter.from_id == ctx.user.id))).first() is not None
    done = sum([swiped >= 5, about >= 3, answered, letter])
    return PrepOut(swiped=swiped, about_me=about, answered=answered, letter=letter, done=done)
