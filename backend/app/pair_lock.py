"""Сериализация операций пары; PostgreSQL lock переживает несколько API workers.

Для SQLite в разработке дополнительно удерживаем локальный asyncio.Lock: SQLite
не поддерживает SELECT FOR UPDATE. Рабочий сервер использует PostgreSQL.
"""
import asyncio
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Pair

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@asynccontextmanager
async def pair_lock(session: AsyncSession, pair_id: str, *, active: bool = True):
    lock = _locks.setdefault(pair_id, asyncio.Lock())
    async with lock:
        pair = (await session.execute(
            select(Pair).where(Pair.id == pair_id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if pair is None or (active and pair.status != "active"):
            raise HTTPException(409, "Пара больше не активна")
        yield pair
