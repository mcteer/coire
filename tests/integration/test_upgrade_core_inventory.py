"""Core inventory requires every durable maintenance row at the exact generation."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from upgrade_fixtures import core_plan

from coire_api.db import (
    Base,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UpgradeReleaseRow,
    UserRow,
)
from coire_api.upgrades.inventory import require_inventory_authority
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.auth import UserRole

pytestmark = pytest.mark.integration


async def test_core_inventory_refuses_partial_and_stale_persisted_scope(
    training_postgres_url: str,
) -> None:
    schema = "upgrade_inventory_" + uuid4().hex
    owner_engine = create_async_engine(training_postgres_url)
    async with owner_engine.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    value = core_plan()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=value.operation.user_id,
                    email="upgrade-inventory@example.test",
                    display_name="Inventory",
                    role=UserRole.ADMIN,
                )
            )
            await session.flush()
            for release in (value.source, value.target):
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
            operation = value.operation
            session.add(
                UpgradeOperationRow(
                    id=operation.job_id,
                    user_id=operation.user_id,
                    source_release_id=operation.source_release_id,
                    target_release_id=operation.target_release_id,
                    idempotency_key=operation.idempotency_key,
                    request_sha256=operation.request_sha256,
                    generation=operation.generation,
                    active_slot=1,
                    phase=operation.phase.value,
                    payload=operation.model_dump(mode="json"),
                    execution_plan=value.model_dump(mode="json"),
                )
            )
            await session.flush()
            session.add(
                UpgradeMaintenanceRow(
                    target=value.maintenance.targets[0],
                    job_id=operation.job_id,
                    generation=operation.generation,
                    released=False,
                    payload=value.maintenance.model_dump(mode="json"),
                )
            )
        async with sessions.begin() as session:
            with pytest.raises(UpgradeUnavailable, match="incomplete"):
                await require_inventory_authority(session, value)
            for target in value.maintenance.targets[1:]:
                session.add(
                    UpgradeMaintenanceRow(
                        target=target,
                        job_id=value.operation.job_id,
                        generation=value.operation.generation,
                        released=False,
                        payload=value.maintenance.model_dump(mode="json"),
                    )
                )
        async with sessions.begin() as session:
            await require_inventory_authority(session, value)
            row = await session.get(UpgradeMaintenanceRow, value.maintenance.targets[-1])
            assert row is not None
            row.payload = value.maintenance.model_copy(
                update={"generation": value.operation.generation + 1}
            ).model_dump(mode="json")
        async with sessions.begin() as session:
            with pytest.raises(UpgradeConflict, match="changed"):
                await require_inventory_authority(session, value)
            await session.execute(delete(UpgradeMaintenanceRow))
    finally:
        await engine.dispose()
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await owner_engine.dispose()


@pytest.mark.parametrize(
    "operation,command_state,blocks",
    [
        ("kill", "succeeded", False),
        ("remove", "succeeded", False),
        ("kill", "pending", True),
        ("kill", "running", True),
        ("kill", "failed", True),
        ("start", "succeeded", True),
    ],
)
async def test_terminal_run_cleanup_requires_completed_node_removal(
    training_postgres_url: str, operation: str, command_state: str, blocks: bool
) -> None:
    from coire_api.db import AgentRunRow, ModelRow, ModelVariantRow, RunCommandRow
    from coire_api.upgrades.inventory import job_inventory
    from coire_core.models.runs import AgentRunState, RunCommandState, RunOperation

    schema = "upgrade_run_cleanup_" + uuid4().hex
    owner_engine = create_async_engine(training_postgres_url)
    async with owner_engine.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user_id, model_id, variant_id, run_id = (uuid4() for _ in range(4))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=user_id,
                    email="cleanup@example.test",
                    display_name="Cleanup",
                    role=UserRole.USER,
                )
            )
            session.add(
                ModelRow(
                    id=model_id,
                    repo_id="fixture/cleanup",
                    slug="cleanup",
                    display_name="Cleanup",
                    precision="4bit",
                    weight_bytes=1,
                    total_bytes=1,
                    file_count=1,
                    memory_estimate_bytes=1,
                )
            )
            await session.flush()
            session.add(
                ModelVariantRow(
                    id=variant_id,
                    model_id=model_id,
                    name="cleanup",
                    slug="cleanup@4bit",
                    source_revision="a" * 40,
                    precision="4bit",
                    state="ready",
                )
            )
            await session.flush()
            session.add(
                AgentRunRow(
                    id=run_id,
                    requester_user_id=user_id,
                    profile="general",
                    primary_model_id=model_id,
                    primary_variant_id=variant_id,
                    container_id="retained-history-container",
                    workspace_ref="cleanup",
                    token_scope={},
                    state=AgentRunState.KILLED,
                    limits={},
                )
            )
            await session.flush()
            session.add(
                RunCommandRow(
                    run_id=run_id,
                    operation=RunOperation(operation),
                    state=RunCommandState(command_state),
                    detail={"removed": True},
                )
            )
        async with sessions.begin() as session:
            owners = await job_inventory(session)
            runs = next(owner for owner in owners if owner.owner == "runs")
            cleanup = [entry for entry in runs.entries if entry.kind == "run-cleanup"]
            assert bool(cleanup) is blocks
            if blocks:
                assert cleanup[0].work_id == str(run_id)
    finally:
        await engine.dispose()
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await owner_engine.dispose()
