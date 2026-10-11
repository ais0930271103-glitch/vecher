"""Регистрация устройства для push-уведомлений и проверка доставки."""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import DeviceToken, User
from ..push import get_sender
from ..security import current_user

router = APIRouter(prefix="/push", tags=["push"])
log = logging.getLogger("vecher.push")
_test_tasks: set[asyncio.Task] = set()


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


@router.post("/test")
async def test(delay: int = 5, user: User = Depends(current_user),
               session: AsyncSession = Depends(get_session)) -> dict:
    """Проверка: через `delay` секунд прислать тестовое уведомление на мои устройства.

    Пауза нужна, чтобы успеть свернуть приложение — иначе Android не покажет системное уведомление.
    Результат отправки пишется в лог сервера.
    """
    delay = max(0, min(delay, 30))
    tokens = (await session.execute(select(DeviceToken.token).where(DeviceToken.user_id == user.id))).scalars().all()
    sender = get_sender()
    result = {"firebase": sender.project_id if sender else None, "devices": len(tokens), "delay": delay}
    if sender is None or not tokens:
        return result

    async def later() -> None:
        await asyncio.sleep(delay)
        for i, t in enumerate(tokens, 1):
            try:
                status, text = await sender.send_raw(t, "Вечер", "Проверка уведомлений: всё работает 🌙", {"open": "today"})
                log.info("PUSH TEST %s/%s → FCM %s %s", i, len(tokens), status, text[:300])
            except Exception as e:  # noqa: BLE001
                log.error("PUSH TEST %s/%s не отправлен: %s", i, len(tokens), e)

    task = asyncio.create_task(later())
    _test_tasks.add(task)
    task.add_done_callback(_test_tasks.discard)
    return result
