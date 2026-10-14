"""Run actual Alembic DDL in an isolated schema, including retained-history guards."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine


async def test_actual_migration_and_history_guards(training_postgres_url: str) -> None:
    source = Path(__file__).parents[2] / "apps/coire-api/alembic/versions/0034_upgrades.py"
    spec = importlib.util.spec_from_file_location("upgrade_migration", source)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_async_engine(training_postgres_url)
    schema = "upgrade_test_" + uuid4().hex

    def upgrade(conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()

    def downgrade(conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()

    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await conn.execute(text("CREATE TABLE users (id UUID PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE api_keys (id UUID PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE nodes (id UUID PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE link_observations (id UUID PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE model_instances (id UUID PRIMARY KEY)"))
            await conn.execute(text("CREATE TABLE harness_evaluations (id UUID PRIMARY KEY)"))
            await conn.run_sync(upgrade)
            await conn.run_sync(downgrade)
            assert await conn.scalar(text("SELECT to_regclass('upgrade_operations') IS NULL"))
            await conn.run_sync(upgrade)
            owner, release = uuid4(), uuid4()
            await conn.execute(text("INSERT INTO users(id) VALUES (:id)"), {"id": owner})
            await conn.execute(
                text(
                    "INSERT INTO upgrade_releases(id,manifest_sha256,prepared_artifact_id,prepared_by,payload) "
                    "VALUES (:id,:sha,'candidate',:owner,'{}'::jsonb)"
                ),
                {"id": release, "sha": "a" * 64, "owner": owner},
            )
            with pytest.raises(RuntimeError, match="refuses retained"):
                await conn.run_sync(downgrade)
            for statement in (
                "UPDATE upgrade_releases SET payload='{}'::jsonb",
                "DELETE FROM upgrade_releases",
            ):
                with pytest.raises(DBAPIError, match="history is immutable"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
            assert await conn.scalar(text("SELECT count(*) FROM upgrade_releases")) == 1
            handover_id = uuid4()
            await conn.execute(
                text(
                    "INSERT INTO upgrade_handovers "
                    "(id,project,user_id,idempotency_key,preparation_sha256,generation,active_slot,phase,preparation_payload) "
                    "VALUES (:id,'coire',:owner,:key,:sha,2,1,'accepted','{}'::jsonb)"
                ),
                {"id": handover_id, "owner": owner, "key": uuid4(), "sha": "b" * 64},
            )
            for statement in (
                'UPDATE upgrade_handovers SET preparation_payload=\'{"actor":"other"}\'::jsonb',
                "UPDATE upgrade_handovers SET generation=3",
                "DELETE FROM upgrade_handovers",
            ):
                with pytest.raises(DBAPIError, match="handover authority is immutable"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
            await conn.execute(
                text("UPDATE upgrade_handovers SET phase='failed_unchanged',active_slot=NULL")
            )
            assert await conn.scalar(text("SELECT count(*) FROM upgrade_handovers")) == 1
            transfer_id = uuid4()
            await conn.execute(
                text(
                    "INSERT INTO upgrade_handovers "
                    "(id,project,user_id,idempotency_key,preparation_sha256,generation,active_slot,phase,preparation_payload) "
                    "VALUES (:id,'coire',:owner,:key,:sha,3,1,'passive','{}'::jsonb)"
                ),
                {"id": transfer_id, "owner": owner, "key": uuid4(), "sha": "c" * 64},
            )
            with pytest.raises(DBAPIError, match="transfer_required"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE upgrade_handovers SET phase='transfer_consumed' WHERE id=:id"),
                        {"id": transfer_id},
                    )
            await conn.execute(
                text(
                    "UPDATE upgrade_handovers SET phase='transfer_consumed',"
                    "transfer_payload=CAST(:payload AS jsonb) WHERE id=:id"
                ),
                {"id": transfer_id, "payload": '{"intent":1}'},
            )
            with pytest.raises(DBAPIError, match="transfer intent is immutable"):
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            "UPDATE upgrade_handovers SET transfer_payload=CAST(:payload AS jsonb) WHERE id=:id"
                        ),
                        {"id": transfer_id, "payload": '{"intent":2}'},
                    )
            await conn.execute(
                text(
                    "UPDATE upgrade_handovers SET phase='committing', commit_payload=CAST(:payload AS jsonb) WHERE id=:id"
                ),
                {"id": transfer_id, "payload": '{"decision":1}'},
            )
            for payload in ('{"decision":2}', "null"):
                with pytest.raises(DBAPIError, match="commit decision is immutable"):
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "UPDATE upgrade_handovers SET commit_payload=CAST(:payload AS jsonb) WHERE id=:id"
                            ),
                            {"id": transfer_id, "payload": payload},
                        )
            await conn.execute(
                text("""INSERT INTO upgrade_operations
                (id,user_id,source_release_id,target_release_id,idempotency_key,
                 request_sha256,generation,active_slot,phase,payload)
                VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAV',:owner,:release,:release,:key,
                        :sha,7,1,'recovery_required','{}'::jsonb)"""),
                {"owner": owner, "release": release, "key": uuid4(), "sha": "d" * 64},
            )
            await conn.execute(
                text("""INSERT INTO upgrade_expired_recoveries
                (id,original_job_id,user_id,idempotency_key,request_sha256,intent_sha256,
                 intent_payload,active_slot,phase)
                VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAW','01ARZ3NDEKTSV4RRFFQ69G5FAV',
                        :owner,'recovery-key',:sha,:sha,'{"deadline":"captured"}'::jsonb,1,'accepted')"""),
                {"owner": owner, "sha": "e" * 64},
            )
            for statement in (
                "UPDATE upgrade_expired_recoveries SET intent_payload='{}'::jsonb",
                "UPDATE upgrade_expired_recoveries SET request_sha256=repeat('f',64)",
                "UPDATE upgrade_expired_recoveries SET idempotency_key='renewed'",
                "DELETE FROM upgrade_expired_recoveries",
            ):
                with pytest.raises(DBAPIError, match="recovery authority is immutable"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
            with pytest.raises(DBAPIError, match="consumption_required"):
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            "UPDATE upgrade_expired_recoveries SET phase='recovered',active_slot=NULL"
                        )
                    )
            await conn.execute(
                text(
                    "UPDATE upgrade_expired_recoveries SET phase='transferring',transfer_payload='{}'::jsonb"
                )
            )
            with pytest.raises(DBAPIError, match="recovery transfer is immutable"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE upgrade_expired_recoveries SET transfer_payload=NULL")
                    )
            with pytest.raises(DBAPIError, match="consumption_required"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE upgrade_expired_recoveries SET phase='transfer_consumed'")
                    )
            await conn.execute(
                text(
                    "UPDATE upgrade_expired_recoveries SET phase='transfer_consumed',consumption_payload='{}'::jsonb"
                )
            )
            with pytest.raises(DBAPIError, match="recovery consumption is immutable"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE upgrade_expired_recoveries SET consumption_payload=NULL")
                    )
            with pytest.raises(DBAPIError, match="owner_required"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE upgrade_expired_recoveries SET phase='recovering'")
                    )
            # Opaque preimages test persistence, not physical owner qualification.
            await conn.execute(
                text("UPDATE upgrade_expired_recoveries SET owner_claim_payload='{}'::jsonb")
            )
            with pytest.raises(DBAPIError, match="recovery owner claim is immutable"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE upgrade_expired_recoveries SET owner_claim_payload=NULL")
                    )
            with pytest.raises(DBAPIError, match="resolution_required"):
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            "UPDATE upgrade_expired_recoveries SET phase='recovered',active_slot=NULL"
                        )
                    )
            await conn.execute(
                text(
                    "UPDATE upgrade_expired_recoveries SET phase='verifying',resolution_payload='{}'::jsonb"
                )
            )
            with pytest.raises(DBAPIError, match="recovery resolution is immutable"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE upgrade_expired_recoveries SET resolution_payload=NULL")
                    )
            await conn.execute(
                text("""INSERT INTO upgrade_recovery_successions
                (id,recovery_id,predecessor_sha256,grant_sha256,grant_payload)
                VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAX','01ARZ3NDEKTSV4RRFFQ69G5FAW',repeat('a',64),repeat('b',64),'{}'::jsonb)""")
            )
            for statement in (
                "UPDATE upgrade_recovery_successions SET grant_payload='{}'::jsonb",
                "DELETE FROM upgrade_recovery_successions",
            ):
                with pytest.raises(DBAPIError, match="succession grant is immutable"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
            with pytest.raises(DBAPIError, match="uq_recovery_succession_predecessor"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("""INSERT INTO upgrade_recovery_successions
                        (id,recovery_id,predecessor_sha256,grant_sha256,grant_payload)
                        VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAY','01ARZ3NDEKTSV4RRFFQ69G5FAW',repeat('a',64),repeat('c',64),'{}'::jsonb)""")
                    )
            await conn.execute(
                text("""INSERT INTO upgrade_recovery_successor_claims
                (id,claim_sha256,claim_payload)
                VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAX',repeat('d',64),'{}'::jsonb)""")
            )
            for statement in (
                "UPDATE upgrade_recovery_successor_claims SET claim_payload='{}'::jsonb",
                "DELETE FROM upgrade_recovery_successor_claims",
            ):
                with pytest.raises(DBAPIError, match="successor claim is immutable"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
            assert (
                await conn.scalar(text("SELECT phase FROM upgrade_operations"))
                == "recovery_required"
            )
            assert (
                await conn.scalar(
                    text("SELECT intent_payload->>'deadline' FROM upgrade_expired_recoveries")
                )
                == "captured"
            )
            with pytest.raises(DBAPIError, match="cancel_retained"):
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            "UPDATE upgrade_expired_recoveries SET cancellation_payload='{}'::jsonb"
                        )
                    )
            await conn.execute(
                text(
                    "UPDATE upgrade_expired_recoveries SET phase='recovery_required',"
                    "cancellation_payload='{}'::jsonb"
                )
            )
            for statement in (
                "UPDATE upgrade_expired_recoveries SET cancellation_payload=NULL",
                "UPDATE upgrade_expired_recoveries SET cancellation_payload='{\"changed\": true}'::jsonb",
            ):
                with pytest.raises(DBAPIError, match="recovery cancellation is immutable"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
            for statement in (
                "UPDATE upgrade_expired_recoveries SET phase='recovering'",
                "UPDATE upgrade_expired_recoveries SET phase='transferring'",
            ):
                with pytest.raises(DBAPIError, match="cancel_retained"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
            # Cancellation stays immutable through a future supported resolution.
            # Opaque SQL metadata is constraint coverage, not native qualification.
            await conn.execute(
                text("UPDATE upgrade_expired_recoveries SET phase='recovered',active_slot=NULL")
            )
            assert await conn.scalar(
                text("SELECT cancellation_payload IS NOT NULL FROM upgrade_expired_recoveries")
            )
            await conn.execute(
                text("""INSERT INTO upgrade_expired_recoveries
                (id,original_job_id,user_id,idempotency_key,request_sha256,intent_sha256,
                 intent_payload,active_slot,phase,cold_claim_payload,cold_smoke_payload)
                SELECT '01ARZ3NDEKTSV4RRFFQ69G5FAY',original_job_id,user_id,'cold-smoke',
                request_sha256,intent_sha256,'{"request":{"recovery_mode":"cold"}}'::jsonb,
                1,'cold_recovering','{}'::jsonb,'{}'::jsonb FROM upgrade_expired_recoveries
                WHERE id='01ARZ3NDEKTSV4RRFFQ69G5FAW'""")
            )
            for statement in (
                "UPDATE upgrade_expired_recoveries SET cold_smoke_payload=NULL WHERE cold_claim_payload IS NOT NULL",
                "UPDATE upgrade_expired_recoveries SET cold_smoke_payload='{\"changed\": true}'::jsonb WHERE cold_claim_payload IS NOT NULL",
            ):
                with pytest.raises(DBAPIError, match="cold recovery smoke authority is immutable"):
                    async with conn.begin_nested():
                        await conn.execute(text(statement))
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
