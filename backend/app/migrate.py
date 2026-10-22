"""Explicit, transactional schema upgrade and read-only startup gate.

Run from backend (or /srv in the API image):
    python -m app.migrate check
    python -m app.migrate upgrade

Existing 0.2.1 databases are validated and adopted by the frozen first revision.
No .env files, keys, existing tables or user rows are recreated. Take a consistent
backup before running upgrade on a server; a failed migration rolls back.
"""
import argparse
import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .config import get_settings

ROOT = Path(__file__).resolve().parent.parent


class SchemaNotReady(RuntimeError):
    pass


def migration_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def expected_heads() -> tuple[str, ...]:
    return tuple(ScriptDirectory.from_config(migration_config()).get_heads())


def check_connection(connection) -> str:
    expected = expected_heads()
    current = MigrationContext.configure(connection).get_current_heads()
    if set(current) != set(expected):
        raise SchemaNotReady("SCHEMA_NOT_READY: run python -m app.migrate upgrade after a verified backup")
    tables = set(inspect(connection).get_table_names())
    if not {"users", "pairs", "delivery_outbox", "delivery_receipts", "evenings", "evening_feedback",
            "video_preferences", "shared_video_preferences", "video_states"}.issubset(tables):
        raise SchemaNotReady("SCHEMA_NOT_READY: migration marker and tables disagree")
    return ",".join(expected)


async def check(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return await connection.run_sync(check_connection)


def upgrade_connection(connection) -> str:
    # Explicit preflight happens before Alembic creates even its version table.
    config = migration_config()
    script = ScriptDirectory.from_config(config)
    current = MigrationContext.configure(connection).get_current_heads()
    if not current:
        legacy = script.get_revision("0001_legacy").module
        legacy.validate_legacy(connection)
        if {"delivery_outbox", "delivery_receipts", "evenings", "evening_feedback",
                "video_preferences", "shared_video_preferences", "video_states"} & set(inspect(connection).get_table_names()):
            raise SchemaNotReady("SCHEMA_NOT_READY: unversioned application tables require manual inspection")
    from .crypto import encrypt
    encrypt("")  # Fail on missing/invalid DATA_KEYS before any persistent change.
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
    return check_connection(connection)


async def upgrade(engine: AsyncEngine) -> str:
    async with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("BEGIN")
        elif connection.dialect.name == "postgresql":
            # Serialize concurrent installers; API workers never take this lock.
            await connection.execute(text("SELECT pg_advisory_xact_lock(726834902135)"))
        return await connection.run_sync(upgrade_connection)


async def _main(action: str) -> None:
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        head = await (upgrade(engine) if action == "upgrade" else check(engine))
        print(("MIGRATION_OK=" if action == "upgrade" else "SCHEMA_HEAD_OK=") + head)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "upgrade"])
    args = parser.parse_args()
    try:
        asyncio.run(_main(args.action))
    except Exception as error:
        # Driver tracebacks can include bind values/URLs; expose only known safe
        # schema diagnostics, never secret settings or private migration values.
        if isinstance(error, SchemaNotReady) or str(error).startswith("LEGACY_SCHEMA_INCOMPATIBLE:"):
            message = str(error)
        else:
            message = f"MIGRATION_FAILED: {type(error).__name__}; transaction rolled back; inspect schema and DATA_KEYS locally"
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
