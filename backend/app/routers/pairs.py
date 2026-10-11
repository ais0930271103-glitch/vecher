import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Pair, PairMember, User
from ..security import current_user
from ..push import notify

router = APIRouter(prefix="/pairs", tags=["pairs"])

# Без похожих символов (0/O, 1/I/L), чтобы код было легко продиктовать.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_invite_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(8))


class PairOut(BaseModel):
    pair_id: str
    status: str
    invite_code: str | None
    partner_id: str | None
    my_name: str | None = None
    partner_name: str | None = None


class JoinIn(BaseModel):
    code: str


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
    return PairOut(pair_id=pair.id, status=pair.status, invite_code=pair.invite_code, partner_id=partner,
                   my_name=me_user.display_name if me_user else None,
                   partner_name=partner_user.display_name if partner_user else None)


@router.get("/me", response_model=PairOut | None)
async def my_pair(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    m = await _membership(session, user.id)
    if m is None:
        return None
    return await _out(session, await session.get(Pair, m.pair_id), user.id)


@router.post("/invite", response_model=PairOut)
async def create_invite(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
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
async def join(body: JoinIn, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    code = body.code.strip().upper().replace("-", "").replace(" ", "")
    pair = (await session.execute(select(Pair).where(Pair.invite_code == code))).scalar_one_or_none()
    if pair is None or pair.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Приглашение не найдено или уже использовано")
    if await _membership(session, user.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ты уже в паре или создал(а) своё приглашение")
    session.add(PairMember(pair_id=pair.id, user_id=user.id, role="invited"))
    pair.status = "active"
    pair.invite_code = None  # одноразовый код
    await session.commit()
    inviter = (await session.execute(
        select(PairMember.user_id).where(PairMember.pair_id == pair.id, PairMember.user_id != user.id)
    )).scalar_one_or_none()
    if inviter:
        name = user.display_name or "Партнёр"
        await notify(session, inviter, "Вечер", f"{name} с тобой — «Вечер» открыт для вас двоих",
                     {"open": "today"}, key=f"joined:{pair.id}")
    return await _out(session, pair, user.id)
