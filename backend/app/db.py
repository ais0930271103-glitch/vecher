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


def _add_missing_columns(conn) -> None:
    """Временная «мини-миграция» для разработки: добавляет новые колонки в уже созданные таблицы
    (create_all умеет только новые таблицы). Колонке нужен server_default или nullable.
    На сервере это заменит Alembic."""
    from sqlalchemy import inspect

    insp = inspect(conn)
    existing_tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(conn.dialect)}'
            if col.server_default is not None:
                ddl += f" DEFAULT '{col.server_default.arg}'"
            conn.exec_driver_sql(ddl)


async def create_all() -> None:
    """Dev/staging bootstrap. Alembic migrations replace this before prod."""
    from . import models  # noqa: F401  (register tables)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
