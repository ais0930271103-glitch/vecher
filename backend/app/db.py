from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def create_all() -> None:
    """Compatibility name for the migration-aware startup gate.

    Development may initialize a fresh DB. Staging/prod never ALTER or backfill
    data at startup: the updater must run the explicit migration after backup.
    """
    from . import models  # noqa: F401  (register tables)
    from .crypto import encrypt
    from .migrate import check, upgrade

    encrypt("")
    if get_settings().is_dev:
        await upgrade(engine)
    else:
        await check(engine)
