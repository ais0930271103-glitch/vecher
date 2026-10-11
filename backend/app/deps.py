from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_session
from .models import Pair, PairMember, User
from .security import current_user


def today_date() -> date:
    """«Сегодня» в часовом поясе проекта. В тестах подменяется через dependency_overrides."""
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


@dataclass
class PairContext:
    user: User
    pair: Pair
    partner_id: str


async def active_pair(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PairContext:
    member = (await session.execute(
        select(PairMember).where(PairMember.user_id == user.id)
    )).scalar_one_or_none()
    pair = await session.get(Pair, member.pair_id) if member else None
    if pair is None or pair.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала соединитесь с партнёром")
    partner_id = (await session.execute(
        select(PairMember.user_id).where(PairMember.pair_id == pair.id, PairMember.user_id != user.id)
    )).scalar_one()
    return PairContext(user=user, pair=pair, partner_id=partner_id)
