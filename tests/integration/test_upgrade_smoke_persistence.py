"""Private smoke authority, cleanup ordering and receipt replay on real PostgreSQL.

The node cleanup transport and registry qualification are separate tested boundaries;
this test runs no engine and does not claim native or Docker acceptance.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from upgrade_fixtures import core_plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    Base,
    EngineProcessRow,
    InstanceTransitionRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
)
from coire_api.nodes_client import NodeClient
from coire_api.upgrades import smoke_instance as service
from coire_core.errors import UpgradeConflict
from coire_core.models.acquisition import VariantState
from coire_core.models.engine import EngineState
from coire_core.models.instance import InstanceState
from coire_core.models.node import NodeRole
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.registry import ModelState
from coire_core.models.upgrades import UpgradeSmokeRequest, UpgradeSmokeResult
from coire_core.settings import Settings


async def test_private_smoke_replay_and_positive_cleanup_preserve_other_holds(
    training_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = f"upgrade_smoke_{uuid4().hex}"
    owner = create_async_engine(training_postgres_url)
    async with owner.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    plan = core_plan()
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR, subject="coire", scopes=frozenset({"upgrades:execute"})
    )
    settings = Settings(upgrades_enabled=True)
    request = UpgradeSmokeRequest(generation=plan.operation.generation, check="gateway")
    deadline = datetime.now(UTC) + timedelta(seconds=90)
    node_id, other_hold = uuid4(), uuid4()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add(
                NodeRow(
                    id=node_id,
                    name="coire-edge-a",
                    role=NodeRole.STUDIO,
                    memory_total_bytes=8 * 1024**3,
                    disk_total_bytes=10 * 1024**3,
                    agent_version="test",
                    health_observed_at=datetime.now(UTC),
                    runtime_sha256="a" * 64,
                )
            )
            session.add(
                ModelRow(
                    id=plan.model_id,
                    repo_id="fixture/tiny",
                    slug=plan.model_slug,
                    display_name="Tiny",
                    state=ModelState.READY,
                    precision="4bit",
                    weight_bytes=1024,
                    total_bytes=1024,
                    file_count=1,
                    memory_estimate_bytes=1024,
                )
            )
            await session.flush()
            session.add(
                ModelVariantRow(
                    id=plan.model_variant_id,
                    model_id=plan.model_id,
                    name="tiny",
                    slug=plan.model_slug,
                    source_revision="fixture",
                    precision="4bit",
                    state=VariantState.READY,
                    validated=True,
                    harness_verified=True,
                    is_default=True,
                    byte_size=1024,
                )
            )
        monkeypatch.setattr(service, "session_scope", sessions.begin)
        monkeypatch.setattr(
            service, "scoped_operation", AsyncMock(return_value=(SimpleNamespace(), plan))
        )
        monkeypatch.setattr(
            service,
            "smoke_asset",
            AsyncMock(
                return_value=(
                    plan.model_id,
                    plan.model_variant_id,
                    plan.model_slug,
                    plan.model_manifest_sha256,
                )
            ),
        )
        prepared = await asyncio.gather(
            *(
                service.prepare_smoke_instance(plan, request, principal, settings, deadline)
                for _ in range(2)
            )
        )
        assert prepared[0] == prepared[1]
        identity, scope = prepared[0]
        async with sessions.begin() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(InstanceTransitionRow)
                    .where(InstanceTransitionRow.instance_id == identity)
                )
                == 1
            )
            instance = await session.get(ModelInstanceRow, identity)
            assert instance is not None
            instance.state = InstanceState.READY
            session.add(
                EngineProcessRow(
                    id=scope.engine_id,
                    model_id=plan.model_id,
                    variant_id=plan.model_variant_id,
                    instance_id=identity,
                    node_id=node_id,
                    port=39101,
                    state=EngineState.READY,
                )
            )
            session.add(
                MemoryReservationRow(
                    node_id=node_id,
                    holder_type=ReservationHolder.MODEL,
                    holder_id=str(identity),
                    bytes=1024,
                    state=MemoryReservationState.HELD,
                )
            )
            session.add(
                MemoryReservationRow(
                    id=other_hold,
                    node_id=node_id,
                    holder_type=ReservationHolder.RUN,
                    holder_id="unrelated-user-run",
                    bytes=2048,
                    state=MemoryReservationState.HELD,
                )
            )

        async def cleanup(_client: NodeClient, observed: object) -> None:
            if observed != scope:
                return
            async with sessions.begin() as session:
                instance = await session.get(ModelInstanceRow, identity)
                assert instance is not None and instance.state is InstanceState.DRAINING
                hold = await session.scalar(
                    select(MemoryReservationRow).where(
                        MemoryReservationRow.holder_id == str(identity)
                    )
                )
                assert hold is not None and hold.state is MemoryReservationState.HELD
                assert instance.upgrade_smoke_result is None

        monkeypatch.setattr(NodeClient, "cleanup_core_smoke", cleanup)
        await service.cleanup_smoke_instance(settings, identity, scope, deadline)
        # A process can die after positive cleanup but before result publication.
        # Retry gets a distinct engine within the same captured deadline.
        retry_id, retry_scope = await service.prepare_smoke_instance(
            plan, request, principal, settings, deadline
        )
        assert retry_id != identity and retry_scope.engine_id != scope.engine_id
        assert retry_scope.deadline_at == deadline
        await service.cleanup_smoke_instance(settings, retry_id, retry_scope, deadline)
        now = datetime.now(UTC)
        result = UpgradeSmokeResult(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            check="gateway",
            runtime_sha256=plan.target.manifest_sha256,
            model_id=plan.model_id,
            model_variant_id=plan.model_variant_id,
            model_manifest_sha256=plan.model_manifest_sha256,
            generated_tokens=64,
            passed=True,
            started_at=now - timedelta(seconds=1),
            finished_at=now,
            output_sha256="c" * 64,
        )
        await service.retain_smoke_result(identity, scope, result)
        assert await service.retained_smoke_result(plan, request, deadline) == result
        await service.retain_smoke_result(identity, scope, result)
        with pytest.raises(UpgradeConflict):
            await service.retain_smoke_result(
                identity, scope, result.model_copy(update={"output_sha256": "d" * 64})
            )
        async with sessions.begin() as session:
            hold = await session.get(MemoryReservationRow, other_hold)
            assert hold is not None and hold.state is MemoryReservationState.HELD
            instance = await session.get(ModelInstanceRow, identity)
            assert instance is not None and instance.state is InstanceState.STOPPED
            assert instance.upgrade_smoke_result == result.model_dump(mode="json")
            own_hold = await session.scalar(
                select(MemoryReservationRow).where(MemoryReservationRow.holder_id == str(identity))
            )
            assert own_hold is not None and own_hold.state is MemoryReservationState.RELEASED
        next_id, next_scope = await service.prepare_smoke_instance(
            plan, request, principal, settings, deadline
        )
        assert next_id != identity and next_scope.engine_id != scope.engine_id
        assert next_scope.deadline_at == deadline
    finally:
        await engine.dispose()
        async with owner.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await owner.dispose()
