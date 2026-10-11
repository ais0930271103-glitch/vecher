from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session
from .consent import CONSENT_VERSION
from .models import Consent, Pair, PairMember, User
from .security import current_user


def today_date() -> date:
    """«Сегодня» в часовом поясе проекта. В тестах подменяется через dependency_overrides."""
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


async def has_consent(session: AsyncSession, user_id: str, kinds: tuple[str, ...] = ("personal",)) -> bool:
    """personal — общее согласие (при регистрации); special — на сведения об интимной жизни и здоровье
    (спрашиваем, когда человек впервые открывает карточки желаний или анкету «о себе»)."""
    have = (await session.execute(
        select(Consent.kind).where(Consent.user_id == user_id, Consent.version == CONSENT_VERSION)
    )).scalars().all()
    return set(kinds) <= set(have)


async def consented_user(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Без согласия на обработку данных (152-ФЗ) личные данные пары не принимаем и не показываем."""
    if not await has_consent(session, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужно согласие на обработку данных — открой приложение заново")
    return user


@dataclass
class PairContext:
    user: User
    pair: Pair
    partner_id: str


async def active_pair(
    user: User = Depends(consented_user),
    session: AsyncSession = Depends(get_session),
) -> PairContext:
    member = (await session.execute(
        select(PairMember).where(PairMember.user_id == user.id)
    )).scalar_one_or_none()
    pair = await session.get(Pair, member.pair_id) if member else None
    if pair is None or pair.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала соединись с партнёром")
    partner_id = (await session.execute(
        select(PairMember.user_id).where(PairMember.pair_id == pair.id, PairMember.user_id != user.id)
    )).scalar_one()
    return PairContext(user=user, pair=pair, partner_id=partner_id)


SPECIAL_CONSENT_DETAIL = "Нужно согласие на сохранение ответов — открой раздел «Вместе»"


async def special_pair(
    ctx: PairContext = Depends(active_pair),
    session: AsyncSession = Depends(get_session),
) -> PairContext:
    """Карточки желаний, планы и анкета «о себе» — особая категория данных (ст. 10 152-ФЗ)."""
    if not await has_consent(session, ctx.user.id, ("special",)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, SPECIAL_CONSENT_DETAIL)
    return ctx


async def prep_pair(
    user: User = Depends(consented_user),
    session: AsyncSession = Depends(get_session),
) -> PairContext:
    """Пара или приглашение, которое ждёт партнёра («Подготовь „Вечер“ для вас двоих»).

    partner_id = "" пока партнёра нет: запросы по нему ничего не находят, уведомления не уходят.
    """
    member = (await session.execute(
        select(PairMember).where(PairMember.user_id == user.id)
    )).scalar_one_or_none()
    pair = await session.get(Pair, member.pair_id) if member else None
    if pair is None or pair.status not in ("active", "pending"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала создай приглашение или соединись с партнёром")
    partner_id = (await session.execute(
        select(PairMember.user_id).where(PairMember.pair_id == pair.id, PairMember.user_id != user.id)
    )).scalar_one_or_none()
    return PairContext(user=user, pair=pair, partner_id=partner_id or "")


async def prep_special_pair(
    ctx: PairContext = Depends(prep_pair),
    session: AsyncSession = Depends(get_session),
) -> PairContext:
    if not await has_consent(session, ctx.user.id, ("special",)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, SPECIAL_CONSENT_DETAIL)
    return ctx
