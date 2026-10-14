"""Known PostgreSQL constraint transition preserves rows and rejects effects."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from coire_api.upgrades.recovery_unstarted_schema import prepare_cancelled_unstarted_constraint
from coire_core.errors import UpgradeConflict


@pytest.mark.parametrize("fault", [None, "unknown"])
async def test_cancelled_unstarted_schema_preserves_data_and_retains_no_effect_guard(
    training_postgres_url: str, fault: str | None
) -> None:
    schema = "cancelled_unstarted_" + uuid4().hex
    root = create_async_engine(training_postgres_url)
    async with root.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE upgrade_expired_recoveries (id text PRIMARY KEY, phase varchar, "
                    "cancellation_payload jsonb, transfer_payload jsonb, consumption_payload jsonb, "
                    "owner_claim_payload jsonb, cold_claim_payload jsonb, cold_smoke_payload jsonb, "
                    "cold_verification_payload jsonb, cold_handback_intent_payload jsonb, "
                    "cold_handback_receipt_payload jsonb, resolution_payload jsonb, "
                    "CONSTRAINT ck_expired_recovery_cancel_retained CHECK "
                    "(cancellation_payload IS NULL OR phase IN ('recovery_required','recovered','superseded')))"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO upgrade_expired_recoveries (id,phase,cancellation_payload) "
                    "VALUES ('retained','recovery_required','{\"intent_sha256\":\"retained exact bytes\"}'::jsonb)"
                )
            )
            if fault:
                await connection.execute(
                    text(
                        "ALTER TABLE upgrade_expired_recoveries DROP CONSTRAINT ck_expired_recovery_cancel_retained"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_cancel_retained CHECK (true)"
                    )
                )
        async with sessions.begin() as session:
            before = await session.scalar(
                text("SELECT to_jsonb(r) FROM upgrade_expired_recoveries r")
            )
            if fault:
                with pytest.raises(UpgradeConflict, match="exact known preimage"):
                    await prepare_cancelled_unstarted_constraint(session)
            else:
                await prepare_cancelled_unstarted_constraint(session)
                assert (
                    await session.scalar(
                        text("SELECT to_jsonb(r) FROM upgrade_expired_recoveries r")
                    )
                    == before
                )
                await session.execute(
                    text("UPDATE upgrade_expired_recoveries SET phase='cancelled_unchanged'")
                )
        if not fault:
            from sqlalchemy.exc import IntegrityError

            async with sessions.begin() as session:
                with pytest.raises(IntegrityError, match="no_effect_authority"):
                    await session.execute(
                        text("UPDATE upgrade_expired_recoveries SET cold_claim_payload='{}'::jsonb")
                    )
    finally:
        await engine.dispose()
        async with root.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await root.dispose()
