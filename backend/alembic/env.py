"""Online migrations use the same async URL as the application.

`python -m app.migrate upgrade` also performs strict legacy-schema preflight.
There is deliberately no literal database URL (or secret) in alembic.ini.
"""
import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings

config = context.config


def run_with_connection(connection):
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            if connection.dialect.name == "sqlite":
                # SQLite's legacy transaction mode otherwise auto-commits DDL.
                await connection.exec_driver_sql("BEGIN")
            await connection.run_sync(run_with_connection)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Legacy adoption requires an online schema check; use python -m app.migrate upgrade")
elif config.attributes.get("connection") is not None:
    run_with_connection(config.attributes["connection"])
else:
    asyncio.run(run_async())
