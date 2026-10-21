"""A schema-changing release must not silently depend on an incompatible image."""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

spec = importlib.util.spec_from_file_location(
    "deploy_schema_guard", Path(__file__).with_name("check-deploy-schema.py")
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.asyncio
@pytest.mark.parametrize("current,allowed", [("0002_outbox", False), ("0003_evenings", True), ("future", False)])
async def test_reads_marker_without_upgrading_or_rewriting_data(tmp_path, current, allowed):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO alembic_version VALUES (:version)"), {"version": current})
            await connection.execute(text("CREATE TABLE saved_data (value TEXT)"))
            await connection.execute(text("INSERT INTO saved_data VALUES ('keep-me')"))
        if allowed:
            await guard.check_engine(engine, ("0003_evenings",))
        else:
            with pytest.raises(guard.RecoveryPlanRequired, match="DEPLOY_SCHEMA_RECOVERY_REQUIRED"):
                await guard.check_engine(engine, ("0003_evenings",))
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == current
            assert await connection.scalar(text("SELECT value FROM saved_data")) == "keep-me"
            assert await connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='evenings'")) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unversioned_database_is_refused_without_creating_marker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unversioned.db'}")
    try:
        with pytest.raises(guard.RecoveryPlanRequired):
            await guard.check_engine(engine, ("0003_evenings",))
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='alembic_version'")) == 0
    finally:
        await engine.dispose()


def test_guard_runs_before_live_api_interruption_or_migration():
    script = Path(__file__).with_name("deploy-realtime.sh").read_text()
    guard_at = script.index("python - < check-deploy-schema.py")
    # Only consider the main sequence, excluding the defined rollback function.
    backup_at = script.index("bash backup.sh --consistent")
    assert guard_at < backup_at < script.index("python -m app.migrate upgrade")
    assert "VECHER_CHANGED=1" not in script[script.index("bash prepare-storage.sh"):guard_at]
