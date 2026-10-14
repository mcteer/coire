"""Real PostgreSQL retains one migration attempt across a lost runner response."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from upgrade_fixtures import core_plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    Base,
    UpgradeGrantRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UpgradeReceiptRow,
    UpgradeReleaseRow,
    UserRow,
)
from coire_api.upgrades.migration import claim_migration, fence_not_started, record_migration
from coire_api.upgrades.schema_identity import inspect_schema
from coire_core.errors import UpgradeConflict
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import (
    UpgradeMigrationExecution,
    UpgradeMigrationNotStarted,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "scenario", ["completed", "fenced", "race", "transaction_failure", "partial"]
)
async def test_one_migration_attempt_survives_restart_and_returns_verified_schema(
    training_postgres_url: str,
    scenario: str,
) -> None:
    schema = "upgrade_migration_execution_" + uuid4().hex
    owner_engine = create_async_engine(training_postgres_url)
    async with owner_engine.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(upgrades_enabled=True)
    original = core_plan(schema_changed=True)
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text("CREATE TABLE alembic_version (version_num varchar(64) PRIMARY KEY)")
            )
            await connection.execute(text("INSERT INTO alembic_version VALUES ('0034_upgrades')"))
        async with sessions.begin() as session:
            head, digest = await inspect_schema(session)
            plan = original.model_copy(
                update={"initial_schema_head": head, "initial_schema_sha256": digest}
            )
            operation = plan.operation.model_copy(update={"phase": UpgradePhase.MIGRATING})
            session.add(
                UserRow(
                    id=operation.user_id,
                    email=f"{operation.user_id}@example.test",
                    display_name="Migration test",
                    role=UserRole.ADMIN,
                )
            )
            await session.flush()
            for release in (plan.source, plan.target):
                session.add(
                    UpgradeReleaseRow(
                        id=release.release_id,
                        manifest_sha256=release.manifest_sha256,
                        prepared_artifact_id=release.prepared_artifact_id,
                        prepared_by=release.prepared_by,
                        payload=release.model_dump(mode="json"),
                    )
                )
            await session.flush()
            session.add(
                UpgradeOperationRow(
                    id=operation.job_id,
                    user_id=operation.user_id,
                    source_release_id=operation.source_release_id,
                    target_release_id=operation.target_release_id,
                    idempotency_key=operation.idempotency_key,
                    request_sha256="f" * 64,
                    generation=operation.generation,
                    active_slot=1,
                    phase=operation.phase.value,
                    payload=operation.model_dump(mode="json"),
                    execution_plan=plan.model_dump(mode="json"),
                    executor_identity=settings.upgrade_executor_id,
                )
            )
            await session.flush()
            fence = plan.maintenance.model_copy(
                update={
                    "state": "active",
                    "acknowledged_generations": {"coire-edge-a": 1, "coire-edge-b": 1},
                }
            )
            for target in fence.targets:
                session.add(
                    UpgradeMaintenanceRow(
                        target=target,
                        job_id=operation.job_id,
                        generation=1,
                        released=False,
                        payload=fence.model_dump(mode="json"),
                    )
                )
            session.add(
                UpgradeGrantRow(
                    id=uuid4(),
                    job_id=operation.job_id,
                    generation=1,
                    boundary="migration",
                    expires_at=now + timedelta(seconds=30),
                    consumed_at=now,
                )
            )
            proof = UpgradeReceipt(
                job_id=operation.job_id,
                generation=1,
                sequence=1,
                effect_id="migration-intent",
                phase=UpgradePhase.MIGRATING,
                occurred_at=now,
                outcome="intent",
                observed_sha256=operation.source_sha256,
            )
            session.add(
                UpgradeReceiptRow(
                    job_id=operation.job_id,
                    generation=1,
                    sequence=1,
                    receipt_sha256=proof.sha256(),
                    payload=proof.model_dump(mode="json"),
                )
            )

        async def claim() -> UpgradeMigrationExecution | None:
            async with sessions.begin() as session:
                try:
                    return (await claim_migration(session, operation.job_id, 1, settings))[1]
                except UpgradeConflict:
                    return None

        async def fence_attempt() -> UpgradeMigrationNotStarted | None:
            async with sessions.begin() as session:
                try:
                    return await fence_not_started(
                        session,
                        plan,
                        Principal(
                            kind=PrincipalKind.UPGRADE_EXECUTOR,
                            subject=settings.upgrade_executor_id,
                            scopes=frozenset({"upgrades:execute"}),
                        ),
                        settings,
                    )
                except UpgradeConflict:
                    return None

        if scenario in {"fenced", "race"}:
            if scenario == "fenced":
                fenced = await fence_attempt()
                assert fenced is not None
                assert await claim() is None
            else:
                fenced, started = await asyncio.gather(fence_attempt(), claim())
                assert (fenced is None) != (started is None)
            async with sessions.begin() as session:
                retained = await session.get(UpgradeOperationRow, operation.job_id)
                assert retained is not None
                assert retained.migration_execution["state"] == (
                    "not_started" if fenced is not None else "intent"
                )
                assert await inspect_schema(session) == (head, digest)
            if fenced is not None:
                assert await fence_attempt() == fenced
                assert await claim() is None
            return

        results = await asyncio.gather(claim(), claim())
        attempts = [result for result in results if result is not None]
        assert len(attempts) == 1
        intent = attempts[0]
        async with sessions.begin() as session:
            with pytest.raises(UpgradeConflict, match="re-execution"):
                await claim_migration(session, operation.job_id, 1, settings)
        if scenario in {"transaction_failure", "partial"}:
            if scenario == "transaction_failure":
                from sqlalchemy.exc import DBAPIError

                with pytest.raises(DBAPIError):
                    async with engine.begin() as connection:
                        await connection.execute(
                            text("CREATE TABLE migration_preserved (id integer PRIMARY KEY)")
                        )
                        await connection.execute(text("SELECT 1 / 0"))
            else:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("CREATE TABLE migration_preserved (id integer PRIMARY KEY)")
                    )
            async with sessions.begin() as session:
                observed = await record_migration(session, plan, intent, exit_code=1)
                assert observed.state == "uncertain"
                assert observed.schema_after_head == head
                assert (observed.schema_after_sha256 == digest) == (
                    scenario == "transaction_failure"
                )
            async with sessions.begin() as session:
                with pytest.raises(UpgradeConflict):
                    await claim_migration(session, operation.job_id, 1, settings)
                retained = await session.get(UpgradeOperationRow, operation.job_id)
                assert retained is not None and retained.active_slot == 1
                assert retained.phase == UpgradePhase.MIGRATING.value
                assert retained.migration_execution == observed.model_dump(mode="json")
                from coire_api.upgrades.grants import verify_current_schema

                with pytest.raises(UpgradeConflict, match="completed migration"):
                    await verify_current_schema(session, operation, plan)
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM upgrade_activation_grants WHERE boundary='runtime'"
                        )
                    )
                    == 0
                )
                assert await session.scalar(
                    text("SELECT to_regclass('migration_preserved') IS NOT NULL")
                ) == (scenario == "partial")
                assert (await inspect_schema(session))[0] == head
            return
        # A small real additive schema effect stands in for the future candidate migration.
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE migration_preserved (id integer PRIMARY KEY)")
            )
            await connection.execute(
                text("UPDATE alembic_version SET version_num='0035_candidate'")
            )
        async with sessions.begin() as session:
            completed = await record_migration(session, plan, intent, exit_code=0)
            assert completed.state == "complete" and completed.schema_after_sha256 != digest
        async with sessions.begin() as session:
            _, replay = await claim_migration(session, operation.job_id, 1, settings)
            assert replay == completed
            assert await session.scalar(
                text("SELECT to_regclass('migration_preserved') IS NOT NULL")
            )
    finally:
        await engine.dispose()
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await owner_engine.dispose()
