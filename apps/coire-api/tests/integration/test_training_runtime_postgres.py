"""Disposable Postgres runtime/input authority tests; synthetic node protocol, no engines."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from training_measurement_fixtures import ATTEMPT, DIGEST, JOB, experiment

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    Base,
    MemoryReservationRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    TrainingAttemptRow,
    TrainingCommandRow,
    TrainingDatasetAnalysisRow,
    TrainingDatasetGrantRow,
    TrainingDatasetRevisionRow,
    TrainingJobRow,
    TrainingParticipantRow,
    UserRow,
    VariantCopyRow,
)
from coire_api.training.input_grants import authorized_input_source, mint_attempt_inputs
from coire_api.training.service import payload_digest
from coire_api.training_executor import TrainingNodeClient, dispatch_training_command
from coire_core.errors import TrainingConflict, TrainingNotFound
from coire_core.models.auth import UserRole
from coire_core.models.placement import MemoryReservationState
from coire_core.models.training_node import (
    TrainingInputsRequest,
    TrainingPrepared,
    TrainingPrepareRequest,
)
from coire_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1", reason="requires disposable PostgreSQL"
    ),
]

type RuntimeDatabase = tuple[async_sessionmaker[AsyncSession], TrainingPrepareRequest]


async def test_pending_unsent_prepare_is_not_misclassified_as_node_loss(
    runtime_db: RuntimeDatabase,
) -> None:
    from coire_core.models.training_node import NodeTrainingEventPage, NodeTrainingStatus
    from coire_scheduler.training_controller import TrainingController

    factory, prepared = runtime_db

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        async with factory.begin() as session:
            yield session

    class UnpreparedNode:
        def __init__(self) -> None:
            self.calls = 0

        async def training_events(
            self, node: str, attempt: str, cursor: int
        ) -> NodeTrainingEventPage:
            self.calls += 1
            raise RuntimeError("prepare has not reached the node")

        async def training_status(self, node: str, attempt: str) -> NodeTrainingStatus:
            self.calls += 1
            raise RuntimeError("prepare has not reached the node")

    node = UnpreparedNode()
    controller = TrainingController(node, sessions=sessions)  # type: ignore[arg-type]
    await controller._observe_node(prepared.node, prepared.attempt_id)
    assert node.calls == 0
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        command = await session.get(TrainingCommandRow, prepared.command_id)
        assert job is not None and attempt is not None and command is not None
        assert job.state == "reserving" and attempt.state == "preparing"
        command.state = "dispatching"  # Lost acknowledgements must remain uncertain.
    await controller._observe_node(prepared.node, prepared.attempt_id)
    assert node.calls == 2
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        hold = await session.get(MemoryReservationRow, prepared.reservation_id)
        assert job is not None and attempt is not None and hold is not None
        assert job.state == "recovering" and attempt.state == "unknown"
        assert hold.state is MemoryReservationState.PENDING


@pytest.mark.parametrize("expired", [False, True])
async def test_stop_rebind_requires_exact_expired_prepare_and_positive_native_receipt(
    runtime_db: RuntimeDatabase, monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_api.training_executor import stop_with_rejected_prepare
    from coire_core.models.training_node import TrainingStopReceipt, TrainingStopRequest

    factory, prepared = runtime_db
    if expired:
        prepared.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        async with factory.begin() as session:
            command = await session.get(TrainingCommandRow, prepared.command_id)
            assert command is not None
            command.payload, command.request_sha256 = (
                prepared.model_dump(mode="json"),
                payload_digest(prepared),
            )

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr("coire_api.training_executor.session_scope", sessions)

    class Node:
        def __init__(self) -> None:
            self.stops: list[TrainingStopRequest] = []
            self.prepares: list[TrainingPrepareRequest] = []

        async def stop_training(self, command: TrainingStopRequest) -> TrainingStopReceipt:
            self.stops.append(command)
            if len(self.stops) == 1:
                raise NodeError(NodeErrorKind.CONFLICT, command.node)
            return TrainingStopReceipt(
                attempt_id=command.attempt_id,
                fence=command.fence,
                node=command.node,
                stopped=True,
                observed_at=datetime.now(UTC),
            )

        async def prepare_training(self, command: TrainingPrepareRequest) -> TrainingPrepared:
            self.prepares.append(command)
            raise NodeError(NodeErrorKind.CONFLICT, command.node)

    fields = {
        k: getattr(prepared, k)
        for k in ("job_id", "attempt_id", "fence", "node", "rank", "world_size", "request_sha256")
    }
    stop = TrainingStopRequest.model_validate(
        {
            **fields,
            "command_id": uuid.uuid4(),
            "lease_expires_at": datetime.now(UTC) + timedelta(seconds=29),
            "reason": "cancelled",
        }
    )
    node = Node()
    if not expired:
        with pytest.raises(TrainingConflict, match="expired"):
            await stop_with_rejected_prepare(node, stop)  # type: ignore[arg-type]
        assert node.prepares == [] and len(node.stops) == 1
    else:
        proof = await stop_with_rejected_prepare(node, stop)  # type: ignore[arg-type]
        assert proof.stopped and proof.pid is None
        assert node.prepares == [prepared] and node.stops == [stop, stop]


@pytest.fixture
async def runtime_db(
    training_postgres_url: str, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[RuntimeDatabase]:
    engine = create_async_engine(training_postgres_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr("coire_api.training_executor.session_scope", sessions)
    monkeypatch.setattr("coire_api.training.runtime.session_scope", sessions)
    monkeypatch.setattr("coire_scheduler.training_guard.session_scope", sessions)
    monkeypatch.setattr("coire_scheduler.training_components.session_scope", sessions)
    _, dispatch = experiment()
    if getattr(request, "param", None) == "preference":
        from preference_measurement_fixtures import preference_experiment

        _, dispatch, _ = preference_experiment()
    prepare = dispatch.commands[0].prepare
    frozen = dispatch.sources[0]
    async with factory.begin() as session:
        owner = UserRow(
            id=uuid.uuid4(),
            email="runtime@training.test",
            display_name="test",
            role=UserRole.ADMIN,
            active=True,
        )
        model = ModelRow(
            id=prepare.resolved.spec.model.model_id,
            repo_id="synthetic/runtime",
            slug="runtime",
            display_name="test",
            state="ready",
            visibility="admin_only",
            placement_policy="single:auto",
            memory_estimate_bytes=100,
            idle_ttl_seconds=900,
            precision="bf16",
            weight_bytes=100,
            total_bytes=100,
            file_count=1,
        )
        session.add_all([owner, model])
        await session.flush()
        variant = ModelVariantRow(
            id=prepare.resolved.spec.model.variant_id,
            model_id=model.id,
            name="test",
            slug="fixture",
            precision="bf16",
            state="ready",
            source_revision="fixture",
            validated=True,
        )
        nodes = [
            NodeRow(
                id=uuid.uuid4(),
                name=name,
                role="studio",
                memory_total_bytes=10000,
                disk_total_bytes=10000,
                agent_version="fixture",
            )
            for name in ("coire-edge-a", "coire-edge-b")
        ]
        session.add_all([variant, *nodes])
        await session.flush()
        for node in nodes:
            session.add(
                VariantCopyRow(
                    variant_id=variant.id,
                    node_id=node.id,
                    verified=True,
                    manifest_sha256=DIGEST,
                    path="/synthetic/fixture",
                    role="origin" if node.name == "coire-edge-a" else "replica",
                )
            )
        dataset = TrainingDatasetRevisionRow(
            id=frozen.binding.dataset_id,
            owner_user_id=owner.id,
            name="fixture",
            format=frozen.binding.format.value,
            state="ready",
            source_sha256=DIGEST,
            source_bytes=10,
            storage_key=str(frozen.binding.dataset_id),
            provenance={},
            row_count=2,
            split_seed=0,
            validation_fraction=0.05,
            split_manifest=frozen.split.model_dump(mode="json"),
            split_sha256=frozen.binding.split_sha256,
        )
        session.add(dataset)
        await session.flush()
        analysis_command = TrainingCommandRow(
            id=uuid.uuid4(),
            actor_user_id=owner.id,
            idempotency_key="analysis",
            operation="dataset.register",
            subject_id=str(dataset.id),
            request_sha256=DIGEST,
            payload={"analysis": frozen.binding.model_dump(mode="json")},
            state="succeeded",
        )
        session.add(analysis_command)
        await session.flush()
        session.add(
            TrainingDatasetAnalysisRow(
                id=frozen.analysis.id,
                dataset_id=dataset.id,
                model_id=model.id,
                variant_id=variant.id,
                command_id=analysis_command.id,
                identity_sha256=payload_digest(frozen.binding),
                state="succeeded",
                result=frozen.analysis.model_dump(mode="json"),
            )
        )
        principal = Principal(
            kind=PrincipalKind.USER, subject=str(owner.id), user_id=owner.id, role=UserRole.ADMIN
        )
        session.add(
            TrainingJobRow(
                id=JOB,
                owner_user_id=owner.id,
                model_id=model.id,
                base_variant_id=variant.id,
                idempotency_key="runtime",
                output_slug="probe",
                source_yaml="{}",
                source_sha256=DIGEST,
                intent_sha256=DIGEST,
                submitted_spec=prepare.resolved.spec.model_dump(mode="json"),
                resolved_sha256=payload_digest(prepare.resolved),
                resolved_spec=prepare.resolved.model_dump(mode="json"),
                authorization_snapshot=principal.model_dump(mode="json"),
                state="reserving",
                fence=1,
                queue_deadline_at=datetime.now(UTC) + timedelta(hours=1),
                execution_deadline_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.flush()
        session.add(
            TrainingAttemptRow(
                id=ATTEMPT,
                job_id=JOB,
                generation=1,
                fence=1,
                world_size=1,
                runtime_sha256=DIGEST,
                state="preparing",
                lease_expires_at=prepare.lease_expires_at,
            )
        )
        session.add(
            MemoryReservationRow(
                id=prepare.reservation_id,
                node_id=nodes[0].id,
                holder_type="training",
                holder_id=ATTEMPT,
                bytes=prepare.resolved.resource_envelope.memory_bytes,
                pinned=True,
                state="pending",
            )
        )
        await session.flush()
        session.add(
            TrainingParticipantRow(
                attempt_id=ATTEMPT,
                node_id=nodes[0].id,
                rank=0,
                reservation_id=prepare.reservation_id,
                disk_reservation_id=prepare.disk_reservation_id,
                command_id=prepare.command_id,
                request_sha256=prepare.request_sha256,
                spawn_nonce=uuid.uuid4(),
            )
        )
        session.add(
            TrainingCommandRow(
                id=prepare.command_id,
                actor_user_id=owner.id,
                idempotency_key="prepare",
                operation="node.training.prepare",
                subject_id=prepare.node,
                job_id=JOB,
                attempt_id=ATTEMPT,
                request_sha256=payload_digest(prepare),
                payload=prepare.model_dump(mode="json"),
                state="pending",
            )
        )
    try:
        yield factory, prepare
    finally:
        await engine.dispose()


class SimulatedNode(TrainingNodeClient):
    def __init__(self) -> None:
        self._settings = Settings(training_enabled=True)
        self.inputs: list[TrainingInputsRequest] = []
        self.ready = False
        self.entered, self.resume = asyncio.Event(), asyncio.Event()
        self.block = False

    async def prepare_training(self, command: TrainingPrepareRequest) -> TrainingPrepared:
        return TrainingPrepared(
            attempt_id=command.attempt_id,
            fence=command.fence,
            node=command.node,
            reservation_id=command.reservation_id,
            runtime_sha256=DIGEST,
            ready=self.ready,
            reason=None if self.ready else "analysis_pending",
        )

    async def deliver_training_inputs(self, command: TrainingInputsRequest) -> TrainingPrepared:
        self.inputs.append(command)
        self.entered.set()
        if self.block:
            await self.resume.wait()
        return TrainingPrepared(
            attempt_id=command.attempt_id,
            fence=command.fence,
            node=command.node,
            reservation_id=uuid.uuid4(),
            runtime_sha256=DIGEST,
            ready=False,
            reason="analysis_pending",
        )


async def test_prepare_delivers_refreshes_and_only_journals_ready(
    runtime_db: RuntimeDatabase,
) -> None:
    factory, prepare = runtime_db
    node = SimulatedNode()
    await dispatch_training_command(prepare.command_id, node)
    await dispatch_training_command(prepare.command_id, node)
    assert len(node.inputs) == 2
    first, second = (c.sources[0].grant for c in node.inputs)
    assert first.secret != second.secret
    async with factory.begin() as session:
        row = await session.get(TrainingCommandRow, prepare.command_id)
        assert row is not None
        assert row.state == "dispatching" and row.receipt is None
        source = await authorized_input_source(
            session, first.dataset_id, prepare.node, first.secret
        )
        assert source.source_sha256 == DIGEST
        grants = list(await session.scalars(select(TrainingDatasetGrantRow)))
        assert len(grants) == 2 and all(g.secret_hash != first.secret for g in grants)
        commands = list(await session.scalars(select(TrainingCommandRow)))
        assert all(first.secret not in str(c.payload) for c in commands)
    node.ready = True
    await dispatch_training_command(prepare.command_id, node)
    async with factory.begin() as session:
        row = await session.get(TrainingCommandRow, prepare.command_id)
        assert row is not None
        assert row.state == "succeeded" and TrainingPrepared.model_validate(row.receipt).ready


@pytest.mark.parametrize("runtime_db", ["preference"], indirect=True)
async def test_preference_prepare_replay_uses_renewed_attempt_authority(
    runtime_db: RuntimeDatabase,
) -> None:
    factory, prepare = runtime_db
    prepare.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    async with factory.begin() as session:
        command = await session.get(TrainingCommandRow, prepare.command_id)
        attempt = await session.get(TrainingAttemptRow, prepare.attempt_id)
        assert command is not None and attempt is not None
        command.payload = prepare.model_dump(mode="json")
        command.request_sha256 = payload_digest(prepare)
        command.state = "dispatching"
        attempt.lease_expires_at = datetime.now(UTC) + timedelta(seconds=29)
    node = SimulatedNode()
    await dispatch_training_command(prepare.command_id, node)
    assert len(node.inputs) == 1
    assert node.inputs[0].lease_expires_at > datetime.now(UTC)
    assert node.inputs[0].sources[0].grant.expires_at == node.inputs[0].lease_expires_at
    node.ready = True
    await dispatch_training_command(prepare.command_id, node)
    async with factory.begin() as session:
        command = await session.get(TrainingCommandRow, prepare.command_id)
        assert command is not None and command.state == "succeeded"
        assert command.payload == prepare.model_dump(mode="json")


@pytest.mark.parametrize("runtime_db", ["preference"], indirect=True)
@pytest.mark.parametrize("mutation", ["expiry", "cancel", "fence", "intent"])
async def test_renewed_preference_preparation_cannot_widen_authority(
    runtime_db: RuntimeDatabase, mutation: str
) -> None:
    factory, prepare = runtime_db
    prepare.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    async with factory.begin() as session:
        command = await session.get(TrainingCommandRow, prepare.command_id)
        attempt = await session.get(TrainingAttemptRow, prepare.attempt_id)
        job = await session.get(TrainingJobRow, prepare.job_id)
        assert command is not None and attempt is not None and job is not None
        command.payload, command.request_sha256 = (
            prepare.model_dump(mode="json"),
            payload_digest(prepare),
        )
        command.state = "dispatching"
        attempt.lease_expires_at = datetime.now(UTC) + timedelta(seconds=29)
        if mutation == "expiry":
            attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif mutation == "cancel":
            job.state = "cancelling"
        elif mutation == "fence":
            attempt.fence += 1
        else:
            command.request_sha256 = "f" * 64
    node = SimulatedNode()
    with pytest.raises(TrainingConflict):
        await dispatch_training_command(prepare.command_id, node)
    assert not node.inputs
    async with factory.begin() as session:
        assert not list(await session.scalars(select(TrainingDatasetGrantRow)))


@pytest.mark.parametrize(
    "mutation", ["cancel", "fence", "revoke", "retire", "participant", "expiry"]
)
async def test_source_grants_recheck_current_authority(
    runtime_db: RuntimeDatabase, mutation: str
) -> None:
    factory, prepare = runtime_db
    settings = Settings(training_enabled=True)
    async with factory.begin() as session:
        inputs = await mint_attempt_inputs(session, prepare, settings)
    grant = inputs.sources[0].grant
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        if mutation == "cancel":
            job.state = "cancelling"
        elif mutation == "fence":
            job.fence += 1
        elif mutation == "revoke":
            owner = await session.get(UserRow, job.owner_user_id)
            assert owner is not None
            owner.active = False
        elif mutation == "retire":
            source = await session.get(TrainingDatasetRevisionRow, grant.dataset_id)
            assert source is not None
            source.purged_at = datetime.now(UTC)
        elif mutation == "participant":
            participant = await session.scalar(select(TrainingParticipantRow))
            assert participant is not None
            participant.request_sha256 = "b" * 64
        else:
            row = await session.get(TrainingDatasetGrantRow, grant.grant_id)
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    from coire_core.errors import TrainingForbidden

    async with factory.begin() as session:
        with pytest.raises((TrainingNotFound, TrainingForbidden)):
            await authorized_input_source(session, grant.dataset_id, prepare.node, grant.secret)


async def test_cancel_during_network_preparation_cannot_mint_more_inputs(
    runtime_db: RuntimeDatabase,
) -> None:
    factory, prepare = runtime_db
    node = SimulatedNode()
    node.block = True
    task = asyncio.create_task(dispatch_training_command(prepare.command_id, node))
    await asyncio.wait_for(node.entered.wait(), timeout=5)
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB, with_for_update=True)
        assert job is not None
        job.state = "cancelling"
    node.resume.set()
    await task
    with pytest.raises(TrainingConflict):
        await dispatch_training_command(prepare.command_id, node)
    async with factory.begin() as session:
        command = await session.get(TrainingCommandRow, prepare.command_id)
        assert command is not None
        assert command.receipt is None and command.state == "dispatching"


async def test_wrong_node_and_unknown_grant_do_not_fallback(runtime_db: RuntimeDatabase) -> None:
    factory, prepare = runtime_db
    async with factory.begin() as session:
        inputs = await mint_attempt_inputs(session, prepare, Settings())
    grant = inputs.sources[0].grant
    async with factory.begin() as session:
        with pytest.raises(TrainingNotFound):
            await authorized_input_source(session, grant.dataset_id, "coire-edge-b", grant.secret)
        with pytest.raises(TrainingNotFound):
            await authorized_input_source(session, grant.dataset_id, prepare.node, "x" * 43)


@pytest.mark.parametrize("cancel_before_finalize", [False, True])
async def test_adapter_replication_and_exact_reserved_finalization(
    runtime_db: RuntimeDatabase, cancel_before_finalize: bool
) -> None:
    """Real transactions/reducers around synthetic immutable node receipts."""
    from coire_api.db import (
        EngineProcessRow,
        InstanceMemberRow,
        ModelInstanceRow,
        TrainingAdapterRow,
        TrainingArtifactCopyRow,
        TrainingCheckpointRow,
    )
    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_api.training.adapters import finalize_serving_adapter
    from coire_api.training.runtime import TrainingRuntime
    from coire_core.models.adapters import InferenceTarget
    from coire_core.models.engine import EngineState, EngineStatus
    from coire_core.models.instance import InstanceState
    from coire_core.models.training_node import (
        TrainingArtifactGrantIssued,
        TrainingArtifactGrantRequest,
        TrainingArtifactImportRequest,
        TrainingArtifactImportStatus,
        TrainingArtifactManifest,
        TrainingArtifactVerificationReceipt,
        TrainingArtifactVerifyRequest,
    )

    factory, prepare = runtime_db
    artifact_id, checkpoint_id = uuid.uuid4(), uuid.uuid4()
    manifest = TrainingArtifactManifest.model_validate(
        {
            "artifact_id": artifact_id,
            "kind": "adapter",
            "files": [
                {"id": "adapter", "name": "adapters.safetensors", "bytes": 10, "sha256": DIGEST}
            ],
            "total_bytes": 10,
            "job_id": JOB,
            "attempt_id": ATTEMPT,
            "fence": 1,
            "update": 2,
            "runtime_sha256": DIGEST,
            "resolved_spec_sha256": payload_digest(prepare.resolved),
        }
    )
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        job.state = "finalizing"
        # Smoke admission follows confirmed trainer teardown; a pending trainer
        # hold must never be bypassed by synthesizing an inference reservation.
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        training_hold = await session.get(MemoryReservationRow, prepare.reservation_id)
        assert attempt is not None and training_hold is not None
        attempt.state = "stopped"
        training_hold.state = MemoryReservationState.RELEASED
        session.add(
            TrainingCheckpointRow(
                id=checkpoint_id,
                job_id=JOB,
                attempt_id=ATTEMPT,
                fence=1,
                completed_update=2,
                manifest_sha256=DIGEST,
                manifest={},
                total_bytes=10,
                state="committed",
            )
        )
        await session.flush()
        session.add(
            TrainingAdapterRow(
                id=artifact_id,
                model_id=job.model_id,
                base_variant_id=job.base_variant_id,
                source_job_id=JOB,
                source_checkpoint_id=checkpoint_id,
                slug="probe",
                selector=f"{job.model_id}@probe",
                base_manifest_sha256=DIGEST,
                manifest_sha256=manifest.canonical_sha256(),
                resolved_spec_sha256=job.resolved_sha256,
                parameterization="lora",
                state="validating",
                visibility="admin_only",
                verified=False,
                metadata_record={
                    "manifest": manifest.model_dump(mode="json"),
                    "automatic": True,
                    "authority": job.authorization_snapshot,
                },
            )
        )

    class ArtifactNode(SimulatedNode):
        def __init__(self) -> None:
            super().__init__()
            self.imported = False
            self.verifications: list[str] = []
            self.smoke: EngineStatus | None = None

        async def verify_training_artifact(
            self, node: str, artifact_id: uuid.UUID, request: TrainingArtifactVerifyRequest
        ) -> TrainingArtifactVerificationReceipt:
            self.verifications.append(node)
            return TrainingArtifactVerificationReceipt(
                command_id=request.command_id,
                artifact_id=artifact_id,
                manifest_sha256=manifest.canonical_sha256(),
                node=node,
                verified_bytes=10,
            )

        async def training_artifact_import_status(
            self, node: str, import_id: uuid.UUID
        ) -> TrainingArtifactImportStatus:
            if not self.imported:
                raise NodeError(NodeErrorKind.NOT_FOUND, node)
            return self.import_status(import_id)

        def import_status(self, import_id: uuid.UUID) -> TrainingArtifactImportStatus:
            return TrainingArtifactImportStatus(
                import_id=import_id,
                artifact_id=artifact_id,
                manifest_sha256=manifest.canonical_sha256(),
                state="verified",
                transferred_bytes=10,
                verified_manifest=manifest,
            )

        async def grant_training_artifact(
            self, request: TrainingArtifactGrantRequest
        ) -> TrainingArtifactGrantIssued:
            return TrainingArtifactGrantIssued(
                grant_id=uuid.uuid4(), secret="test-secret-" * 4, expires_at=request.expires_at
            )

        async def import_training_artifact(
            self, request: TrainingArtifactImportRequest
        ) -> TrainingArtifactImportStatus:
            self.imported = True
            return self.import_status(request.command_id)

        async def get_engine(self, node: str, engine_id: uuid.UUID) -> EngineStatus:
            assert self.smoke is not None
            assert self.smoke.engine_id == engine_id
            return self.smoke

    node = ArtifactNode()
    runtime = TrainingRuntime(node._settings, node)
    assert await runtime.prepare_adapter(artifact_id) is None
    assert node.verifications == ["coire-edge-a", "coire-edge-b"]
    instance_id = uuid.uuid5(artifact_id, "validation-smoke")
    target = InferenceTarget(
        model_id=prepare.resolved.spec.model.model_id,
        variant_id=prepare.resolved.spec.model.variant_id,
        adapter_id=artifact_id,
        base_manifest_sha256=DIGEST,
        adapter_manifest_sha256=manifest.canonical_sha256(),
    )
    engine_id, hold_id = uuid.uuid4(), uuid.uuid4()
    async with factory.begin() as session:
        copies = list(await session.scalars(select(TrainingArtifactCopyRow)))
        assert len(copies) == 2
        instance = await session.get(ModelInstanceRow, instance_id)
        assert instance is not None
        assert instance.state.value == "requested"
        instance.state = InstanceState.READY
        studio = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert studio is not None
        session.add(
            MemoryReservationRow(
                id=hold_id,
                node_id=studio.id,
                holder_type="model",
                holder_id=str(instance_id),
                bytes=100,
                pinned=False,
                state="held",
            )
        )
        session.add(
            EngineProcessRow(
                id=engine_id,
                instance_id=instance_id,
                model_id=target.model_id,
                variant_id=target.variant_id,
                adapter_id=artifact_id,
                node_id=studio.id,
                port=8000,
                state="ready",
                estimate_bytes=100,
            )
        )
        await session.flush()
        session.add(
            InstanceMemberRow(
                instance_id=instance_id,
                node_id=studio.id,
                rank=0,
                engine_id=engine_id,
                reservation_id=hold_id,
                host="coire-edge-a",
                port=8000,
            )
        )
    now = datetime.now(UTC)
    node.smoke = EngineStatus(
        engine_id=engine_id,
        target=target,
        port=8000,
        pid=123,
        process_create_time=now.timestamp(),
        state=EngineState.READY,
        started_at=now,
        last_health_at=now,
    )
    result = await runtime.prepare_adapter(artifact_id)
    assert result is not None
    if cancel_before_finalize:
        async with factory.begin() as session:
            job = await session.get(TrainingJobRow, JOB, with_for_update=True)
            assert job is not None
            job.state = "cancelling"
        async with factory.begin() as session:
            with pytest.raises(TrainingConflict):
                await finalize_serving_adapter(
                    session, artifact_id, result[1], instance_id=result[0]
                )
        async with factory.begin() as session:
            job = await session.get(TrainingJobRow, JOB)
            adapter = await session.get(TrainingAdapterRow, artifact_id)
            assert job is not None and adapter is not None
            assert job.adapter_id is None and adapter.state == "validating"
        return
    async with factory.begin() as session:
        adapter = await finalize_serving_adapter(
            session, artifact_id, result[1], instance_id=result[0]
        )
        assert (
            adapter.state == "ready" and not adapter.verified and adapter.visibility == "admin_only"
        )
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        assert job.state == "succeeded" and job.adapter_id == artifact_id
        command = await session.scalar(
            select(TrainingCommandRow).where(TrainingCommandRow.operation == "node.adapter.import")
        )
        assert command is not None
        assert "test-secret" not in str(command.payload)


async def test_final_extraction_journals_exact_native_intent_and_polls(
    runtime_db: RuntimeDatabase,
) -> None:
    from coire_api.db import (
        TrainingArtifactCopyRow,
        TrainingCheckpointRow,
        TrainingStorageReservationRow,
    )
    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_api.training.runtime import TrainingRuntime
    from coire_core.models.training_node import (
        TrainingAdapterExtractionStatus,
        TrainingAdapterExtractRequest,
        TrainingArtifactManifest,
    )

    factory, prepare = runtime_db
    checkpoint_id, adapter_id, command_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    manifest = TrainingArtifactManifest.model_validate(
        {
            "artifact_id": checkpoint_id,
            "kind": "checkpoint",
            "files": [
                {"id": name, "name": name + suffix, "bytes": 10, "sha256": DIGEST}
                for name, suffix in (
                    ("adapter", ".safetensors"),
                    ("optimizer", ".safetensors"),
                    ("state", ".json"),
                )
            ],
            "total_bytes": 30,
            "job_id": JOB,
            "attempt_id": ATTEMPT,
            "fence": 1,
            "update": 2,
            "world_size": 1,
            "runtime_sha256": DIGEST,
            "resolved_spec_sha256": payload_digest(prepare.resolved),
            "ranks": [
                {
                    "rank": 0,
                    "update": 2,
                    "adapter_file_id": "adapter",
                    "optimizer_file_id": "optimizer",
                    "state_file_id": "state",
                    "adapter_tensors": [{"key": "a", "shape": [1], "dtype": "float32"}],
                    "optimizer_tensors": [{"key": "m", "shape": [1], "dtype": "float32"}],
                }
            ],
        }
    )
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        assert job is not None and attempt is not None
        job.state, attempt.state = "finalizing", "stopped"
        session.add(
            TrainingCheckpointRow(
                id=checkpoint_id,
                job_id=JOB,
                attempt_id=ATTEMPT,
                fence=1,
                completed_update=2,
                manifest_sha256=manifest.canonical_sha256(),
                manifest=manifest.model_dump(mode="json"),
                total_bytes=30,
                state="committed",
            )
        )
        await session.flush()
        for studio_node in await session.scalars(select(NodeRow)):
            session.add(
                TrainingArtifactCopyRow(
                    artifact_id=checkpoint_id,
                    checkpoint_id=checkpoint_id,
                    node_id=studio_node.id,
                    manifest_sha256=manifest.canonical_sha256(),
                    storage_key=str(checkpoint_id),
                    total_bytes=30,
                    state="verified",
                    verified_at=datetime.now(UTC),
                )
            )
        session.add(
            TrainingCommandRow(
                id=command_id,
                actor_user_id=job.owner_user_id,
                idempotency_key="final",
                operation="training.final.extract",
                subject_id=str(adapter_id),
                job_id=JOB,
                request_sha256=DIGEST,
                payload={"adapter_id": str(adapter_id), "checkpoint_id": str(checkpoint_id)},
                state="dispatching",
            )
        )

    class ExtractionNode(SimulatedNode):
        def __init__(self) -> None:
            super().__init__()
            self.command: TrainingAdapterExtractRequest | None = None
            self.finished = False

        async def extract_adapter(
            self, command: TrainingAdapterExtractRequest
        ) -> TrainingAdapterExtractionStatus:
            self.command = command
            # Native intent must have committed before authenticated side effects.
            async with factory.begin() as session:
                row = await session.get(TrainingCommandRow, command_id)
                assert row is not None and row.payload["node_command"] == command.model_dump(
                    mode="json"
                )
                assert (
                    await session.get(TrainingStorageReservationRow, command.disk_reservation_id)
                    is not None
                )
            return self.status()

        def status(self) -> TrainingAdapterExtractionStatus:
            serving = (
                TrainingArtifactManifest.model_validate(
                    {
                        **manifest.model_dump(mode="json"),
                        "artifact_id": adapter_id,
                        "kind": "adapter",
                        "files": [manifest.files[0].model_dump(mode="json")],
                        "total_bytes": 10,
                        "ranks": [],
                    }
                )
                if self.finished
                else None
            )
            return TrainingAdapterExtractionStatus(
                command_id=command_id,
                adapter_id=adapter_id,
                checkpoint_id=checkpoint_id,
                node=prepare.node,
                state="succeeded" if self.finished else "running",
                manifest=serving,
            )

        async def adapter_extraction_status(
            self, node: str, command_id: uuid.UUID
        ) -> TrainingAdapterExtractionStatus:
            if self.command is None:
                raise NodeError(NodeErrorKind.NOT_FOUND, node)
            return self.status()

    node = ExtractionNode()
    runtime = TrainingRuntime(node._settings, node)
    assert await runtime.extract_final_adapter(checkpoint_id, adapter_id, command_id) is None
    node.finished = True
    extracted = await runtime.extract_final_adapter(checkpoint_id, adapter_id, command_id)
    assert extracted is not None and extracted.artifact_id == adapter_id
    async with factory.begin() as session:
        holds = list(await session.scalars(select(TrainingStorageReservationRow)))
        assert len(holds) == 1
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        job.state = "cancelling"
    with pytest.raises(TrainingConflict):
        await runtime.extract_final_adapter(checkpoint_id, adapter_id, command_id)


@pytest.mark.parametrize("breach", ["memory_breach", "thermal_breach", "insufficient_samples"])
async def test_guard_reads_actual_persisted_metrics_and_fresh_ledger(
    runtime_db: RuntimeDatabase, breach: str
) -> None:
    from coire_api.db import NodeMemoryLedgerRow, TrainingMetricRow
    from coire_api.training.runtime import TrainingRuntime
    from coire_core.models.training import TrainingMetricSample

    factory, prepare = runtime_db
    async with factory.begin() as session:
        participant = await session.scalar(select(TrainingParticipantRow))
        assert participant is not None
        session.add(
            NodeMemoryLedgerRow(
                node_id=participant.node_id,
                budget_bytes=16 * 1024**3,
                sandbox_bytes=0,
                measured_resident_bytes=1,
                health="healthy",
                thermal_state="critical" if breach == "thermal_breach" else "nominal",
                health_sampled_at=datetime.now(UTC),
            )
        )
        if breach != "insufficient_samples":
            sample = TrainingMetricSample(
                job_id=JOB,
                attempt_id=ATTEMPT,
                update=1,
                kind="train",
                loss=1,
                learning_rate=0.01,
                tokens=10,
                tokens_per_second=1,
                updates_per_second=1,
                footprint_bytes=prepare.resolved.resource_envelope.memory_bytes + 1
                if breach == "memory_breach"
                else 1,
                peak_bytes=1,
                recorded_at=datetime.now(UTC),
            )
            session.add(
                TrainingMetricRow(
                    job_id=JOB,
                    attempt_id=ATTEMPT,
                    completed_update=1,
                    kind="train",
                    loss=1,
                    metric=sample.model_dump(mode="json"),
                    rolled_back=False,
                )
            )
            participant.footprint_bytes = sample.footprint_bytes
    node = SimulatedNode()
    runtime = TrainingRuntime(node._settings, node)
    assert await runtime.guard_reason(ATTEMPT) == breach
