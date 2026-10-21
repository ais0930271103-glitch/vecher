"""Each durability test gets a fresh SQLite file or an isolated PostgreSQL schema."""
from contextlib import asynccontextmanager
import os
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


@asynccontextmanager
async def isolated_engine(tmp_path):
    url = os.environ.get("VECHER_OUTBOX_TEST_DATABASE_URL", "sqlite+aiosqlite:///" + str(tmp_path / "isolated.db"))
    admin = None
    schema = None
    kwargs = {"poolclass": NullPool}
    if url.startswith("postgresql"):
        # Never drop a supplied database or somebody else's tables. Only this
        # test's random schema is created and removed, even on a shared server.
        schema = "vecher_test_" + uuid.uuid4().hex
        admin = create_async_engine(url, poolclass=NullPool)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        kwargs["connect_args"] = {"server_settings": {"search_path": schema}}
    engine = create_async_engine(url, **kwargs)
    try:
        yield engine
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()
