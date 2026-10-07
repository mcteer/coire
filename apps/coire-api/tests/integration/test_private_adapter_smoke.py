"""Persisted private smoke routing through runtime, placement and finalization.

Node receipts are simulated; this suite makes no bare-engine acceptance claim.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_training_runtime_postgres import RuntimeDatabase, runtime_db  # noqa: F401
from test_training_transactions import manifest as checkpoint_manifest
from training_measurement_fixtures import ATTEMPT, JOB

from coire_api.auth import ADMIN, Principal, PrincipalKind
from coire_api.db import (
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeRow,
    PlacementCommandRow,
    PlacementDecisionRow,
    TrainingAdapterRow,
    TrainingArtifactCopyRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingJobRow,
    UserRow,
    VariantCopyRow,
)
from coire_api.gateway.targets import ModelNotFoundError, resolve_target, resolve_validation_target
from coire_api.instance.service import (
    InvalidInstanceTransition,
    project_instance,
    resolve_instance_target,
    transition,
)
from coire_api.training.adapters import finalize_serving_adapter, stage_serving_adapter
from coire_api.training.checkpoints import record_verified_copy
from coire_api.training.runtime import TrainingRuntime
from coire_api.training.service import payload_digest
from coire_core.models.engine import EngineState, EngineStatus
from coire_core.models.instance import InstanceState
from coire_core.models.node import Reachability
from coire_core.models.placement import MemoryReservationState, PlacementState
from coire_core.models.training_node import (
    TrainingArtifactManifest,
    TrainingArtifactVerificationReceipt,
    TrainingArtifactVerifyRequest,
)
from coire_core.settings import Settings
from coire_scheduler import placement

pytestmark = pytest.mark.integration


@pytest.fixture
async def candidate(
    runtime_db: RuntimeDatabase,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TrainingRuntime, uuid.UUID, RuntimeDatabase]:
    factory, prepare = runtime_db
    settings = Settings(training_enabled=True)
    monkeypatch.setattr("coire_core.settings.get_settings", lambda: settings)
    monkeypatch.setattr(placement, "get_settings", lambda: settings)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr(placement, "session_scope", sessions)
    checkpoint = checkpoint_manifest(2).model_copy(
        update={"resolved_spec_sha256": payload_digest(prepare.resolved)}
    )
    adapter_manifest = TrainingArtifactManifest.model_validate(
        {
            **checkpoint.model_dump(mode="json", exclude={"ranks"}),
            "kind": "adapter",
            "artifact_id": uuid.uuid4(),
        }
    )
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        hold = await session.get(MemoryReservationRow, prepare.reservation_id)
        assert job and attempt and hold
        job.state, attempt.state, hold.state = (
            "finalizing",
            "stopped",
            MemoryReservationState.RELEASED,
        )
        session.add(
            TrainingCheckpointRow(
                id=checkpoint.artifact_id,
                job_id=JOB,
                attempt_id=ATTEMPT,
                fence=1,
                completed_update=2,
                manifest_sha256=checkpoint.canonical_sha256(),
                manifest=checkpoint.model_dump(mode="json"),
                total_bytes=checkpoint.total_bytes,
                state="committed",
            )
        )
        await session.flush()
        for node in ("coire-edge-a", "coire-edge-b"):
            await record_verified_copy(
                session,
                checkpoint,
                TrainingArtifactVerificationReceipt(
                    command_id=uuid.uuid4(),
                    artifact_id=checkpoint.artifact_id,
                    manifest_sha256=checkpoint.canonical_sha256(),
                    node=node,
                    verified_bytes=checkpoint.total_bytes,
                ),
                checkpoint_id=checkpoint.artifact_id,
            )
        adapter = await stage_serving_adapter(
            session,
            Principal.model_validate(job.authorization_snapshot),
            checkpoint.artifact_id,
            adapter_manifest,
            slug="probe",
            automatic=True,
        )
        for node in ("coire-edge-a", "coire-edge-b"):
            await record_verified_copy(
                session,
                adapter_manifest,
                TrainingArtifactVerificationReceipt(
                    command_id=uuid.uuid4(),
                    artifact_id=adapter.id,
                    manifest_sha256=adapter_manifest.canonical_sha256(),
                    node=node,
                    verified_bytes=adapter_manifest.total_bytes,
                ),
                adapter_id=adapter.id,
            )
        for node_row in await session.scalars(select(NodeRow)):
            node_row.reachability, node_row.last_seen_at = Reachability.HEALTHY, datetime.now(UTC)

    class NodeReceipts:
        async def verify_training_artifact(
            self, node: str, artifact_id: uuid.UUID, request: TrainingArtifactVerifyRequest
        ) -> TrainingArtifactVerificationReceipt:
            return TrainingArtifactVerificationReceipt(
                command_id=request.command_id,
                artifact_id=artifact_id,
                manifest_sha256=adapter_manifest.canonical_sha256(),
                node=node,
                verified_bytes=adapter_manifest.total_bytes,
            )

        async def get_engine(self, node: str, engine_id: uuid.UUID) -> EngineStatus:
            async with factory.begin() as session:
                engine = await session.get(EngineProcessRow, engine_id)
                assert engine and engine.instance_id
                target = (await resolve_validation_target(session, engine.instance_id)).identity
                now = datetime.now(UTC)
                return EngineStatus(
                    engine_id=engine_id,
                    target=target,
                    port=engine.port,
                    pid=123,
                    process_create_time=now.timestamp(),
                    state=EngineState.READY,
                    started_at=now,
                    last_health_at=now,
                )

    runtime = TrainingRuntime(settings, NodeReceipts())  # type: ignore[arg-type]
    assert await runtime.prepare_adapter(adapter.id) is None
    return runtime, adapter.id, runtime_db


@pytest.mark.parametrize("adapter_state", ["validating", "replicating"])
@pytest.mark.parametrize("unconfirmed", [False, True])
async def test_runtime_smoke_placement_emits_exact_target_then_fenced_finalizer(
    candidate: tuple[TrainingRuntime, uuid.UUID, RuntimeDatabase],
    monkeypatch: pytest.MonkeyPatch,
    adapter_state: str,
    unconfirmed: bool,
) -> None:
    runtime, adapter_id, (factory, _) = candidate
    instance_id = uuid.uuid5(adapter_id, "validation-smoke")
    async with factory.begin() as session:
        instance = await session.get(ModelInstanceRow, instance_id)
        assert instance
        adapter = await session.get(TrainingAdapterRow, adapter_id)
        assert adapter
        adapter.state = adapter_state
        await session.flush()
        selected = await resolve_instance_target(session, instance)
        assert selected.identity and selected.identity.adapter_id == adapter_id
        assert (await project_instance(session, instance)).target == selected.identity
        decision = PlacementDecisionRow(
            model_id=instance.model_id,
            variant_id=instance.variant_id,
            policy=instance.policy,
            required_bytes=1024,
            state=PlacementState.REQUESTED,
        )
        session.add(decision)
        await session.flush()
        instance.placement_decision_id = decision.id
        decision_id, identity = decision.id, selected.identity

    async def acknowledge(command_id: uuid.UUID) -> dict[str, object]:
        async with factory.begin() as session:
            command = await session.get(PlacementCommandRow, command_id)
            assert command and command.operation == "load"
            assert command.payload["target"] == identity.model_dump(mode="json")
            hold = await session.get(MemoryReservationRow, command.reservation_id)
            engine = await session.get(EngineProcessRow, command.engine_id)
            assert hold and engine
            assert hold.holder_id == str(instance_id)
            assert engine.adapter_id == adapter_id and engine.instance_id == instance_id
            hold.state, engine.state, engine.port = (
                MemoryReservationState.HELD,
                EngineState.READY,
                8000,
            )
            command.state = "succeeded"
            result: dict[str, object] = {
                "target": (
                    identity.model_copy(update={"adapter_manifest_sha256": "f" * 64})
                    if unconfirmed
                    else identity
                ).model_dump(mode="json")
            }
            command.result = result
            return result

    monkeypatch.setattr(placement, "_wait_command", acknowledge)
    if unconfirmed:
        with pytest.raises(placement.ExactTargetUnconfirmed):
            await placement._run_decision(decision_id)
        async with factory.begin() as session:
            hold = await session.scalar(
                select(MemoryReservationRow).where(
                    MemoryReservationRow.holder_id == str(instance_id)
                )
            )
            observed_decision = await session.get(PlacementDecisionRow, decision_id)
            assert hold and hold.state is MemoryReservationState.HELD
            assert observed_decision and observed_decision.state is PlacementState.LOADING
        return
    await placement._run_decision(decision_id)
    async with factory.begin() as session:
        observed_decision = await session.get(PlacementDecisionRow, decision_id)
        assert observed_decision and observed_decision.state is PlacementState.READY
        engine = await session.scalar(
            select(EngineProcessRow).where(EngineProcessRow.instance_id == instance_id)
        )
        hold = await session.scalar(
            select(MemoryReservationRow).where(MemoryReservationRow.holder_id == str(instance_id))
        )
        assert engine and hold
        session.add(
            InstanceMemberRow(
                instance_id=instance_id,
                node_id=engine.node_id,
                rank=0,
                engine_id=engine.id,
                reservation_id=hold.id,
                host="coire-edge-a",
                port=8000,
            )
        )
        for state in (
            InstanceState.RESERVING,
            InstanceState.LAUNCHING,
            InstanceState.WARMING,
        ):
            await transition(session, instance_id, state)
        # A model-level legacy hold may not stand in for the smoke instance.
        hold.holder_id = str(identity.model_id)
        await session.flush()
        with pytest.raises(InvalidInstanceTransition):
            await transition(session, instance_id, InstanceState.READY)
        hold.holder_id = str(instance_id)
        await session.flush()
        await transition(session, instance_id, InstanceState.READY)
        adapter = await session.get(TrainingAdapterRow, adapter_id)
        assert adapter and adapter.state == adapter_state and not adapter.verified
        for principal in (
            ADMIN,
            Principal(kind=PrincipalKind.USER),
            Principal(
                kind=PrincipalKind.RUN,
                permitted_model_ids=frozenset({identity.model_id}),
                permitted_targets=(identity,),
            ),
        ):
            with pytest.raises(ModelNotFoundError):
                await resolve_target(session, adapter.selector, principal)
    smoke = await runtime.prepare_adapter(adapter_id)
    assert smoke and smoke[0] == instance_id and smoke[1].target == identity
    async with factory.begin() as session:
        adapter = await finalize_serving_adapter(
            session, adapter_id, smoke[1], instance_id=smoke[0]
        )
        assert (
            adapter.state == "ready" and not adapter.verified and adapter.visibility == "admin_only"
        )
        assert (await resolve_target(session, adapter.selector, ADMIN)).identity == identity


async def test_private_smoke_authority_set_order_survives_serialization(
    candidate: tuple[TrainingRuntime, uuid.UUID, RuntimeDatabase],
) -> None:
    _, adapter_id, (factory, _) = candidate
    instance_id = uuid.uuid5(adapter_id, "validation-smoke")
    async with factory.begin() as session:
        adapter = await session.get(TrainingAdapterRow, adapter_id)
        assert adapter
        authority = Principal.model_validate(adapter.metadata_record["authority"]).model_copy(
            update={
                "scopes": frozenset({"admin", "chat", "mcp"}),
                "entitlements": frozenset({"development", "acceptance"}),
            }
        )
        serialized = authority.model_dump(mode="json")
        serialized["scopes"] = list(reversed(serialized["scopes"]))
        serialized["entitlements"] = list(reversed(serialized["entitlements"]))
        adapter.metadata_record = {**adapter.metadata_record, "authority": serialized}
        await session.flush()
        resolved = await resolve_validation_target(session, instance_id)
        assert resolved.identity and resolved.identity.adapter_id == adapter_id


@pytest.mark.parametrize(
    "tamper",
    [
        "forged_instance",
        "metadata",
        "revoked_admin",
        "base_digest",
        "artifact_copy",
        "checkpoint",
        "cancel",
        "disabled",
    ],
)
async def test_private_smoke_refuses_forgery_and_stale_authority(
    candidate: tuple[TrainingRuntime, uuid.UUID, RuntimeDatabase],
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    _, adapter_id, (factory, _) = candidate
    instance_id = uuid.uuid5(adapter_id, "validation-smoke")
    async with factory.begin() as session:
        adapter = await session.get(TrainingAdapterRow, adapter_id)
        assert adapter
        if tamper == "forged_instance":
            forged = ModelInstanceRow(
                id=uuid.uuid4(),
                model_id=adapter.model_id,
                variant_id=adapter.base_variant_id,
                adapter_id=adapter.id,
                policy="single:auto",
                state=InstanceState.REQUESTED,
            )
            session.add(forged)
            instance_id = forged.id
        elif tamper == "metadata":
            adapter.metadata_record = {
                **adapter.metadata_record,
                "smoke_instance_id": str(uuid.uuid4()),
            }
        elif tamper == "revoked_admin":
            owner = await session.scalar(select(UserRow))
            assert owner
            owner.active = False
        elif tamper == "base_digest":
            copy = await session.scalar(select(VariantCopyRow))
            assert copy
            copy.manifest_sha256 = "f" * 64
        elif tamper == "artifact_copy":
            copy = await session.scalar(
                select(TrainingArtifactCopyRow).where(
                    TrainingArtifactCopyRow.adapter_id == adapter.id
                )
            )
            assert copy
            copy.total_bytes += 1
        elif tamper == "checkpoint":
            checkpoint = await session.get(TrainingCheckpointRow, adapter.source_checkpoint_id)
            assert checkpoint
            checkpoint.purged_at = datetime.now(UTC)
        elif tamper == "cancel":
            job = await session.get(TrainingJobRow, JOB)
            assert job
            job.state = "cancelling"
        elif tamper == "disabled":
            monkeypatch.setattr(
                "coire_core.settings.get_settings", lambda: Settings(training_enabled=False)
            )
        await session.flush()
        with pytest.raises(ModelNotFoundError):
            await resolve_validation_target(session, instance_id)
        instance = await session.get(ModelInstanceRow, instance_id)
        assert instance
        with pytest.raises(ModelNotFoundError):
            await resolve_instance_target(session, instance)
        for principal in (ADMIN, Principal(kind=PrincipalKind.USER)):
            with pytest.raises(ModelNotFoundError):
                await resolve_target(session, adapter.selector, principal)
        assert adapter.state == "validating" and not adapter.verified
