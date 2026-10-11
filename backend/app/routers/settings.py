"""Настройки человека, которые хранит сервер: уведомления."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import User, UserSettings
from ..security import current_user

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsOut(BaseModel):
    push_enabled: bool
    push_private: bool


class SettingsIn(BaseModel):
    push_enabled: bool | None = None
    push_private: bool | None = None


def _out(row: UserSettings | None) -> SettingsOut:
    return SettingsOut(push_enabled=row.push_enabled if row else True,
                       push_private=row.push_private if row else False)


@router.get("", response_model=SettingsOut)
async def get_settings_(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return _out(await session.get(UserSettings, user.id))


@router.put("", response_model=SettingsOut)
async def update_settings(body: SettingsIn, user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    row = await session.get(UserSettings, user.id)
    if row is None:
        row = UserSettings(user_id=user.id, push_enabled=True, push_private=False)
        session.add(row)
    if body.push_enabled is not None:
        row.push_enabled = body.push_enabled
    if body.push_private is not None:
        row.push_private = body.push_private
    await session.commit()
    return _out(row)
