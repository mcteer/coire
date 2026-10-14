"""Actual PostgreSQL constraint preimages for the separate supersession state."""

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from coire_api.db import Base
from coire_api.upgrades.cold_continuation_schema_checks import (
    RECOVERY_PHASES,
    continuation_ddl,
    known_checks,
)


async def test_actual_continuation_constraints_match_exact_known_shapes(training_postgres_url):
    namespace = "cold_continuation_" + uuid4().hex
    root = create_async_engine(training_postgres_url)
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": namespace}}
    )
    names = tuple(known_checks(continuation=True))

    async def definitions(conn):
        rows = await conn.execute(
            text(
                "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid=to_regclass('upgrade_expired_recoveries') "
                "AND conname = ANY(:names)"
            ),
            {"names": list(names)},
        )
        return dict(rows.all())

    try:
        async with root.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {namespace}"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            assert await definitions(conn) == known_checks(continuation=True)
            for name in names:
                await conn.execute(
                    text(f"ALTER TABLE upgrade_expired_recoveries DROP CONSTRAINT {name}")
                )
            phases = ",".join(f"'{phase}'" for phase in RECOVERY_PHASES)
            await conn.execute(
                text(
                    "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_phase "
                    f"CHECK (phase IN ({phases}))"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_terminal_owner "
                    "CHECK ((active_slot IS NULL) = (phase IN ('recovered','cancelled_unchanged')))"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_cancel_retained "
                    "CHECK (cancellation_payload IS NULL OR phase IN ('recovery_required','recovered'))"
                )
            )
            assert await definitions(conn) == known_checks(continuation=False)
            original_checks = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=to_regclass('upgrade_operations')"
                        )
                    )
                ).all()
            )
            for statement in continuation_ddl():
                await conn.execute(text(statement))
            assert await definitions(conn) == known_checks(continuation=True)
            assert (
                dict(
                    (
                        await conn.execute(
                            text(
                                "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=to_regclass('upgrade_operations')"
                            )
                        )
                    ).all()
                )
                == original_checks
            )
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {namespace} CASCADE"))
        await root.dispose()
