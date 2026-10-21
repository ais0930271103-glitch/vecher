#!/usr/bin/env python3
"""Read-only guard for an update whose fallback is the previous API image.

Run inside the new API image before stopping the running API. A schema-changing
release needs its own verified recovery procedure; additive tables alone do not
make a previous strict-head API compatible with the new migration marker.
"""
import asyncio


class RecoveryPlanRequired(RuntimeError):
    pass


def require_unchanged_schema(current: tuple[str, ...], target: tuple[str, ...]) -> None:
    if not current or not target or set(current) != set(target):
        raise RecoveryPlanRequired(
            "DEPLOY_SCHEMA_RECOVERY_REQUIRED: this update changes the database schema; "
            "the previous API image is not a verified fallback. "
            "Use the release-specific recovery procedure before migration. "
            "The running API and database were not changed."
        )


async def check_engine(engine, target: tuple[str, ...]) -> None:
    from alembic.runtime.migration import MigrationContext

    async with engine.connect() as connection:
        current = await connection.run_sync(
            lambda sync: MigrationContext.configure(sync).get_current_heads()
        )
    require_unchanged_schema(current, target)


async def main() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config import get_settings
    from app.migrate import expected_heads

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        await check_engine(engine, expected_heads())
    finally:
        await engine.dispose()
    print("DEPLOY_SCHEMA_UNCHANGED_OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RecoveryPlanRequired as error:
        raise SystemExit(str(error)) from None
    except Exception:
        # Database exceptions may contain credentials or bind values.
        raise SystemExit("DEPLOY_SCHEMA_CHECK_FAILED: inspect the database connection locally") from None
