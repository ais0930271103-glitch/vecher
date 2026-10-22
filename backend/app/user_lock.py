"""Serialize account-owned preferences; PostgreSQL protects multiple API workers."""
import asyncio
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import User

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@asynccontextmanager
async def user_lock(session: AsyncSession, user_id: str):
    lock = _locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        user = (await session.execute(select(User).where(User.id == user_id)
                                     .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if user is None:
            raise HTTPException(401, "Войди в приложение заново")
        yield user
