"""Adopt real legacy DBs, preserve rows, reject drift and roll back failed DDL."""
from datetime import datetime, timezone

from alembic import command
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
    assert await upgrade(migration_db) == "0004_videos"
    assert await check(migration_db) == "0004_videos"
    assert await upgrade(migration_db) == "0004_videos"
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
    assert await upgrade(migration_db) == "0004_videos"
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
    assert await upgrade(migration_db) == "0004_videos"


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


async def upgrade_to_outbox(engine):
    """Build the actual frozen 0.3.x schema, without current model metadata."""
    def run(connection):
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0002_outbox")
    async with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("BEGIN")
        await connection.run_sync(run)


async def test_outbox_upgrade_keeps_legacy_plans_and_rejects_old_api_head(migration_db, monkeypatch):
    from app import migrate

    metadata = await create_legacy(migration_db)
    await upgrade_to_outbox(migration_db)
    now = datetime.now(timezone.utc)
    async with migration_db.begin() as connection:
        await connection.execute(metadata.tables["wish_cards"].insert().values(
            id=1, deck="home", text="Existing matched card", level="warm", active=True))
        await connection.execute(metadata.tables["plan_unlocks"].insert().values(
            id="old-open", pair_id="legacy-pair", card_id=1, free=False, scheduled_at=now, created_at=now))
    assert await upgrade(migration_db) == "0004_videos"
    async with migration_db.connect() as connection:
        row = (await connection.execute(sa.text("SELECT id, free, scheduled_at FROM plan_unlocks"))).one()
        assert row.id == "old-open" and not row.free and row.scheduled_at
        assert await connection.scalar(sa.text("SELECT count(*) FROM evenings")) == 0
        inspector = await connection.run_sync(lambda c: sa.inspect(c).get_unique_constraints("evenings"))
        assert {tuple(c["column_names"]) for c in inspector} >= {("pair_id", "client_id"), ("active_pair_id",)}
    # The previous binary really expects 0002: additive DDL does not make an
    # automatic image rollback safe. Never silently stamp the schema backwards.
    monkeypatch.setattr(migrate, "expected_heads", lambda: ("0002_outbox",))
    with pytest.raises(SchemaNotReady):
        await check(migration_db)


async def test_failed_evening_revision_rolls_back_to_outbox_without_data_loss(migration_db, monkeypatch):
    await create_legacy(migration_db)
    await upgrade_to_outbox(migration_db)
    before = await table_names(migration_db)
    real_create_table = Operations.create_table

    def fail_feedback(self, name, *args, **kwargs):
        if name == "evening_feedback":
            raise RuntimeError("injected failure in third revision")
        return real_create_table(self, name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_table", fail_feedback)
    with pytest.raises(RuntimeError, match="injected failure in third revision"):
        await upgrade(migration_db)
    assert await table_names(migration_db) == before
    async with migration_db.connect() as connection:
        assert await connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0002_outbox"
        assert await connection.scalar(sa.text("SELECT display_name FROM users WHERE id='legacy-user'")) == "Saved name"
    monkeypatch.setattr(Operations, "create_table", real_create_table)
    assert await upgrade(migration_db) == "0004_videos"


async def test_legacy_pair_delete_cascades_new_evening_tables_with_real_foreign_keys(migration_db):
    from app.models import Evening, EveningFeedback

    metadata = await create_legacy(migration_db)
    await upgrade(migration_db)
    now = datetime.now(timezone.utc)
    async with migration_db.connect() as connection:
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert await connection.scalar(sa.text("PRAGMA foreign_keys")) == 1
            await connection.commit()
        async with connection.begin():
            await connection.execute(metadata.tables["wish_cards"].insert().values(
                id=1, deck="home", text="Card", level="warm", active=True))
            await connection.execute(Evening.__table__.insert().values(
                id="new-evening", pair_id="legacy-pair", active_pair_id="legacy-pair", client_id="request",
                card_id=1, started_by="legacy-user", option_index=0, mode="together", status="active",
                revision=1, snapshot="{}", progress="[]", started_at=now, updated_at=now))
            await connection.execute(EveningFeedback.__table__.insert().values(
                evening_id="new-evening", user_id="legacy-user", data="{}", updated_at=now))
            # An old SQL delete does not know these tables; FKs still erase both.
            await connection.execute(sa.text("DELETE FROM pairs WHERE id='legacy-pair'"))
            assert await connection.scalar(sa.text("SELECT count(*) FROM evenings")) == 0
            assert await connection.scalar(sa.text("SELECT count(*) FROM evening_feedback")) == 0


async def upgrade_to_evenings(engine):
    """Actual previous release, without the new tables or current ORM metadata."""
    def run(connection):
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0003_evenings")
    async with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("BEGIN")
        await connection.run_sync(run)


async def test_video_upgrade_preserves_previous_rows_rejects_old_head_and_old_delete_cascades(migration_db, monkeypatch):
    from app import migrate
    from app.models import Evening, SharedVideoPreferences, VideoPreferences, VideoState

    metadata = await create_legacy(migration_db)
    await upgrade_to_evenings(migration_db)
    now = datetime.now(timezone.utc)
    async with migration_db.begin() as connection:
        await connection.execute(metadata.tables["wish_cards"].insert().values(
            id=1, deck="home", text="Card", level="warm", active=True))
        await connection.execute(Evening.__table__.insert().values(
            id="retained-evening", pair_id="legacy-pair", active_pair_id="legacy-pair", client_id="request",
            card_id=1, started_by="legacy-user", option_index=0, mode="together", status="active",
            revision=3, snapshot='{"private":"unchanged"}', progress='["done"]', started_at=now, updated_at=now))
        before = (await connection.execute(sa.text("SELECT snapshot, progress, revision FROM evenings"))).one()
    assert await upgrade(migration_db) == "0004_videos"
    assert await upgrade(migration_db) == "0004_videos"
    async with migration_db.connect() as connection:
        assert (await connection.execute(sa.text("SELECT snapshot, progress, revision FROM evenings"))).one() == before
        assert await connection.scalar(sa.text("SELECT count(*) FROM video_states")) == 0
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert await connection.scalar(sa.text("PRAGMA foreign_keys")) == 1
        await connection.commit()
        async with connection.begin():
            await connection.execute(VideoPreferences.__table__.insert().values(
                user_id="legacy-user", topics='["animals"]', revision=1, updated_at=now))
            await connection.execute(VideoState.__table__.insert().values(
                user_id="legacy-user", video_id="cat", data='{"saved":true}', updated_at=now))
            await connection.execute(SharedVideoPreferences.__table__.insert().values(
                pair_id="legacy-pair", topics='["travel"]', revision=1, updated_at=now))
            # A compatible recovery image knows only old models; the new FKs
            # still guarantee privacy if an account/pair is erased in recovery.
            await connection.execute(sa.text("DELETE FROM pairs WHERE id='legacy-pair'"))
            assert await connection.scalar(sa.text("SELECT count(*) FROM shared_video_preferences")) == 0
            assert await connection.scalar(sa.text("SELECT count(*) FROM video_states")) == 1
            await connection.execute(sa.text("DELETE FROM users WHERE id='legacy-user'"))
            assert await connection.scalar(sa.text("SELECT count(*) FROM video_states")) == 0
            assert await connection.scalar(sa.text("SELECT count(*) FROM video_preferences")) == 0
    monkeypatch.setattr(migrate, "expected_heads", lambda: ("0003_evenings",))
    with pytest.raises(SchemaNotReady):
        await check(migration_db)


async def test_failed_video_revision_rolls_back_to_evenings_with_existing_rows(migration_db, monkeypatch):
    await create_legacy(migration_db)
    await upgrade_to_evenings(migration_db)
    before = await table_names(migration_db)
    real_create_table = Operations.create_table

    def fail_states(self, name, *args, **kwargs):
        if name == "video_states":
            raise RuntimeError("injected failure in fourth revision")
        return real_create_table(self, name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_table", fail_states)
    with pytest.raises(RuntimeError, match="injected failure in fourth revision"):
        await upgrade(migration_db)
    assert await table_names(migration_db) == before
    async with migration_db.connect() as connection:
        assert await connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0003_evenings"
        assert await connection.scalar(sa.text("SELECT display_name FROM users WHERE id='legacy-user'")) == "Saved name"
    monkeypatch.setattr(Operations, "create_table", real_create_table)
    assert await upgrade(migration_db) == "0004_videos"
