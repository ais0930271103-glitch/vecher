"""Adopt real legacy DBs, preserve rows, reject drift and roll back failed DDL."""
from datetime import datetime, timezone

from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa

from app.crypto import decrypt
from app.db import Base
from app.migrate import SchemaNotReady, check, migration_config, upgrade
from delivery_test_support import isolated_engine


def legacy_revision():
    return ScriptDirectory.from_config(migration_config()).get_revision("0001_legacy").module


@pytest.fixture
async def migration_db(tmp_path):
    async with isolated_engine(tmp_path) as engine:
        yield engine


async def create_legacy(engine, metadata=None):
    metadata = metadata or legacy_revision().frozen_metadata()
    now = datetime.now(timezone.utc)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(metadata.tables["users"].insert().values(
            id="legacy-user", email="legacy@example.test", display_name="Saved name", created_at=now))
        await connection.execute(metadata.tables["pairs"].insert().values(
            id="legacy-pair", status="active", created_at=now))
        await connection.execute(metadata.tables["questions"].insert().values(
            id=1, text="Question", category="warm", active=True))
        await connection.execute(metadata.tables["daily_answers"].insert().values(
            id="legacy-answer", pair_id="legacy-pair", user_id="legacy-user", day=now.date(),
            question_id=1, text="legacy private answer", created_at=now))
    return metadata


async def table_names(engine):
    async with engine.connect() as connection:
        return set(await connection.run_sync(lambda c: sa.inspect(c).get_table_names()))


async def test_fresh_schema_upgrade_matches_models_and_is_idempotent(migration_db):
    assert await upgrade(migration_db) == "0002_outbox"
    assert await check(migration_db) == "0002_outbox"
    assert await upgrade(migration_db) == "0002_outbox"
    assert await table_names(migration_db) == set(Base.metadata.tables) | {"alembic_version"}
    async with migration_db.connect() as connection:
        columns = await connection.run_sync(lambda c: {name: {x["name"] for x in sa.inspect(c).get_columns(name)}
                                                        for name in Base.metadata.tables})
    assert columns == {name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()}
    # The historical baseline stays 38 tables even as current models gain tables.
    assert len(legacy_revision().frozen_metadata().tables) == 38
    assert "delivery_outbox" not in legacy_revision().frozen_metadata().tables


async def test_existing_legacy_data_is_adopted_without_recreation(migration_db):
    metadata = legacy_revision().frozen_metadata()
    # Actual legacy ALTER TABLE omitted NOT NULL for this added column.
    metadata.tables["wish_cards"].c.level.nullable = True
    await create_legacy(migration_db, metadata)
    with pytest.raises(SchemaNotReady):
        await check(migration_db)
    assert "delivery_outbox" not in await table_names(migration_db)
    assert await upgrade(migration_db) == "0002_outbox"
    async with migration_db.connect() as connection:
        name = await connection.scalar(sa.text("SELECT display_name FROM users WHERE id='legacy-user'"))
        raw = await connection.scalar(sa.text("SELECT text FROM daily_answers WHERE id='legacy-answer'"))
    assert name == "Saved name" and raw.startswith("enc1:")
    assert decrypt(raw) == "legacy private answer"
    await upgrade(migration_db)
    async with migration_db.connect() as connection:
        assert await connection.scalar(sa.text("SELECT text FROM daily_answers WHERE id='legacy-answer'")) == raw


async def test_incompatible_known_column_refuses_adoption_before_changes(migration_db):
    metadata = legacy_revision().frozen_metadata()
    metadata.tables["users"].c.email.type = sa.Integer()
    async with migration_db.begin() as connection:
        await connection.run_sync(metadata.create_all)
    before = await table_names(migration_db)
    with pytest.raises(RuntimeError, match="LEGACY_SCHEMA_INCOMPATIBLE: column/type users.email"):
        await upgrade(migration_db)
    assert await table_names(migration_db) == before
    assert "alembic_version" not in before and "delivery_outbox" not in before


async def test_partial_legacy_schema_refuses_adoption(migration_db):
    async with migration_db.begin() as connection:
        await connection.execute(sa.text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(254))"))
    with pytest.raises(RuntimeError, match="LEGACY_SCHEMA_INCOMPATIBLE: missing tables"):
        await upgrade(migration_db)
    assert await table_names(migration_db) == {"users"}


async def test_failed_later_revision_rolls_back_baseline_and_plaintext_backfill(migration_db, monkeypatch):
    await create_legacy(migration_db)
    before = await table_names(migration_db)
    real_create_table = Operations.create_table

    def fail_outbox(self, name, *args, **kwargs):
        if name == "delivery_outbox":
            raise RuntimeError("injected failure in second revision")
        return real_create_table(self, name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_table", fail_outbox)
    with pytest.raises(RuntimeError, match="injected failure"):
        await upgrade(migration_db)
    assert await table_names(migration_db) == before
    async with migration_db.connect() as connection:
        raw = await connection.scalar(sa.text("SELECT text FROM daily_answers WHERE id='legacy-answer'"))
        assert raw == "legacy private answer"
        assert await connection.scalar(sa.text("SELECT display_name FROM users WHERE id='legacy-user'")) == "Saved name"
    monkeypatch.setattr(Operations, "create_table", real_create_table)
    assert await upgrade(migration_db) == "0002_outbox"


async def test_required_null_legacy_column_is_not_silently_adopted(migration_db):
    metadata = legacy_revision().frozen_metadata()
    metadata.tables["wish_cards"].c.level.nullable = True
    await create_legacy(migration_db, metadata)
    async with migration_db.begin() as connection:
        await connection.execute(metadata.tables["wish_cards"].insert().values(
            id=1, deck="romance", text="Card", active=True, level=None))
    with pytest.raises(RuntimeError, match="null values in wish_cards.level"):
        await upgrade(migration_db)
    assert "alembic_version" not in await table_names(migration_db)


async def test_server_startup_is_a_read_only_gate(migration_db, monkeypatch):
    from app import db, migrate
    from types import SimpleNamespace

    await create_legacy(migration_db)
    before = await table_names(migration_db)
    monkeypatch.setattr(db, "engine", migration_db)
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(is_dev=False))
    with pytest.raises(SchemaNotReady):
        await db.create_all()
    assert await table_names(migration_db) == before
    await upgrade(migration_db)

    async def forbidden_upgrade(*args, **kwargs):
        pytest.fail("Server startup must not run migrations")

    monkeypatch.setattr(migrate, "upgrade", forbidden_upgrade)
    await db.create_all()
