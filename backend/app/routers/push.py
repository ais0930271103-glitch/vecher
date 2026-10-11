"""Регистрация устройства для push-уведомлений."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import DeviceToken, User
from ..security import current_user

router = APIRouter(prefix="/push", tags=["push"])


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=512)
    platform: str = "android"


@router.post("/register")
async def register(body: TokenIn, user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)) -> dict:
    """Привязать токен к текущему человеку (если токен был у другого — переносим: телефон сменил хозяина)."""
    if body.platform not in ("android", "ios", "rustore"):
        raise HTTPException(422, "Платформа: android, ios или rustore")
    row = (await session.execute(select(DeviceToken).where(DeviceToken.token == body.token))).scalar_one_or_none()
    if row is None:
        session.add(DeviceToken(user_id=user.id, token=body.token, platform=body.platform))
    else:
        row.user_id, row.platform = user.id, body.platform
    await session.commit()
    return {"ok": True}


@router.post("/unregister")
async def unregister(body: TokenIn, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)) -> dict:
    """При выходе из аккаунта — чтобы уведомления больше не приходили на этот телефон."""
    await session.execute(delete(DeviceToken).where(DeviceToken.token == body.token, DeviceToken.user_id == user.id))
    await session.commit()
    return {"ok": True}
