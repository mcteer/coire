"""Real scheduler runtime adapters. Core journals metadata; Studios own all artifacts."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, cast, func, select

from coire_api.auth import Principal
from coire_api.db import (
    InstanceMemberRow,
    ModelInstanceRow,
    NodeRow,
    TrainingAdapterRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingCommandRow,
    TrainingMeasurementRow,
    TrainingParticipantRow,
    TrainingStorageReservationRow,
    session_scope,
)
from coire_api.nodes_client import NodeError, NodeErrorKind
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.checkpoints import record_verified_copy, verified_nodes
from coire_api.training.events import current_job
from coire_api.training.service import payload_digest, recheck_training_base
from coire_api.training.telemetry import observed
from coire_api.training_executor import TrainingNodeClient
from coire_core.errors import TrainingConflict, TrainingQuotaExceeded
from coire_core.models.engine import EngineState, EngineStatus
from coire_core.models.instance import InstanceState
from coire_core.models.training import (
    ResolvedTrainingSpec,
    TrainingProfile,
    TrainingReason,
)
from coire_core.models.training_node import (
    TrainingAdapterExtractRequest,
    TrainingArtifactGrantRefresh,
    TrainingArtifactGrantRequest,
    TrainingArtifactImportIntent,
    TrainingArtifactImportRequest,
    TrainingArtifactManifest,
    TrainingArtifactVerifyRequest,
)
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


class TrainingRuntime:
    def __init__(self, settings: Settings, client: TrainingNodeClient) -> None:
        self.settings, self.client = settings, client

    @observed("coire.scheduler.training.runtime.promotions")
    async def reconcile_promotions(self) -> None:
        if not self.settings.training_enabled:
            return
        from coire_api.training.adapters import finalize_serving_adapter
        from coire_api.training_executor import dispatch_adapter_extraction

        async with session_scope() as session:
            commands = list(
                await session.scalars(
                    select(TrainingCommandRow.id)
                    .where(
                        TrainingCommandRow.operation == "node.adapter.extract",
                        TrainingCommandRow.state.in_(["pending", "dispatching"]),
                    )
                    .limit(8)
                )
            )
            adapters = list(
                await session.scalars(
                    select(TrainingAdapterRow.id)
                    .where(
                        TrainingAdapterRow.state.in_(["validating", "replicating"]),
                        TrainingAdapterRow.manifest_sha256.is_not(None),
                        TrainingAdapterRow.metadata_record["automatic"].as_boolean().is_(False),
                    )
                    .limit(8)
                )
            )
        for command_id in commands:
            try:
                async with asyncio.timeout(5):
                    await dispatch_adapter_extraction(command_id, self.client)
            except Exception:
                logger.info("adapter extraction pending", extra={"command_id": str(command_id)})
        for adapter_id in adapters:
            try:
                async with asyncio.timeout(5):
                    smoke = await self.prepare_adapter(adapter_id)
                if smoke is not None:
                    async with session_scope() as session:
                        await finalize_serving_adapter(
                            session, adapter_id, smoke[1], instance_id=smoke[0]
                        )
            except Exception:
                logger.info("adapter preparation pending", extra={"adapter_id": str(adapter_id)})

    @observed("coire.scheduler.training.runtime.extract")
    async def extract_final_adapter(
        self, checkpoint_id: uuid.UUID, adapter_id: uuid.UUID, command_id: uuid.UUID
    ) -> TrainingArtifactManifest | None:
        async with session_scope() as session:
            checkpoint = await session.get(TrainingCheckpointRow, checkpoint_id)
            if checkpoint is None:
                raise TrainingConflict("Final checkpoint is unavailable")
            job = await current_job(session, checkpoint.job_id)
            await authorize_live_training_action(
                session, Principal.model_validate(job.authorization_snapshot)
            )
            job = await current_job(session, checkpoint.job_id, lock=True)
            resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
            await recheck_training_base(session, resolved)
            checkpoint_manifest = TrainingArtifactManifest.model_validate(checkpoint.manifest)
            if (
                checkpoint_manifest.kind != "checkpoint"
                or checkpoint_manifest.artifact_id != checkpoint_id
                or checkpoint_manifest.canonical_sha256() != checkpoint.manifest_sha256
                or checkpoint_manifest.resolved_spec_sha256 != job.resolved_sha256
                or checkpoint_manifest.runtime_sha256 != resolved.runtime_sha256
            ):
                raise TrainingConflict("Final checkpoint immutable identity changed")
            attempt = await session.get(TrainingAttemptRow, checkpoint.attempt_id)
            if (
                job.state != "finalizing"
                or job.fence != checkpoint.fence
                or checkpoint.state != "committed"
                or checkpoint.purged_at is not None
                or checkpoint.completed_update != resolved.spec.optim.updates
                or attempt is None
                or attempt.state != "stopped"
                or await verified_nodes(
                    session, checkpoint.id, checkpoint.manifest_sha256, checkpoint.total_bytes
                )
                != {"coire-edge-a", "coire-edge-b"}
            ):
                raise TrainingConflict("Final extraction authority ended")
            row = await session.get(TrainingCommandRow, command_id, with_for_update=True)
            if row is None or row.operation != "training.final.extract" or row.job_id != job.id:
                raise TrainingConflict("Final extraction intent is missing")
            if row.payload.get("adapter_id") != str(adapter_id) or row.payload.get(
                "checkpoint_id"
            ) != str(checkpoint_id):
                raise TrainingConflict("Final extraction identity changed")
            if row.payload.get("node_command") is not None:
                command = TrainingAdapterExtractRequest.model_validate(row.payload["node_command"])
            else:
                source = await session.scalar(
                    select(NodeRow)
                    .join(TrainingParticipantRow, TrainingParticipantRow.node_id == NodeRow.id)
                    .where(
                        TrainingParticipantRow.attempt_id == attempt.id,
                        TrainingParticipantRow.rank == 0,
                    )
                )
                if source is None:
                    raise TrainingConflict("Extraction participant is missing")
                from coire_api.placement.service import lock_nodes_for_admission

                await lock_nodes_for_admission(session, [source.id])
                maximum = min(checkpoint.total_bytes + 65536, 20 * 1024**3)
                held = await session.scalar(
                    select(func.coalesce(func.sum(TrainingStorageReservationRow.bytes), 0)).where(
                        TrainingStorageReservationRow.node_id == source.id,
                        TrainingStorageReservationRow.state.in_(["held", "releasing"]),
                    )
                )
                if int(held or 0) + maximum * 2 > self.settings.training_artifact_quota_bytes:
                    raise TrainingQuotaExceeded("Final extraction storage quota is held")
                disk_id = uuid.uuid5(command_id, "disk")
                session.add(
                    TrainingStorageReservationRow(
                        id=disk_id,
                        owner_user_id=job.owner_user_id,
                        node_id=source.id,
                        subject_id=str(adapter_id),
                        bytes=maximum * 2,
                        state="held",
                    )
                )
                command = TrainingAdapterExtractRequest.model_validate(
                    {
                        "command_id": command_id,
                        "adapter_id": adapter_id,
                        "checkpoint_id": checkpoint_id,
                        "checkpoint_manifest_sha256": checkpoint.manifest_sha256,
                        "job_id": job.id,
                        "attempt_id": attempt.id,
                        "fence": checkpoint.fence,
                        "node": source.name,
                        "resolved": resolved,
                        "disk_reservation_id": disk_id,
                        "max_bytes": maximum,
                        "deadline": row.created_at + timedelta(seconds=60),
                    }
                )
                row.payload = {**row.payload, "node_command": command.model_dump(mode="json")}
            if (
                command.command_id != command_id
                or command.adapter_id != adapter_id
                or command.checkpoint_id != checkpoint_id
                or command.job_id != job.id
                or command.attempt_id != checkpoint.attempt_id
                or command.fence != checkpoint.fence
                or command.checkpoint_manifest_sha256 != checkpoint.manifest_sha256
                or payload_digest(command.resolved) != job.resolved_sha256
            ):
                raise TrainingConflict("Journaled native extraction identity changed")
            if command.deadline <= datetime.now(UTC):
                raise TrainingConflict("Final extraction deadline expired")
        try:
            status = await self.client.adapter_extraction_status(command.node, command_id)
        except NodeError as error:
            if error.kind is not NodeErrorKind.NOT_FOUND:
                raise
            status = await self.client.extract_adapter(command)
        if (
            status.command_id != command_id
            or status.adapter_id != adapter_id
            or status.checkpoint_id != checkpoint_id
            or status.node != command.node
        ):
            raise TrainingConflict("Extraction status differs from journaled identity")
        if status.state == "failed":
            raise TrainingConflict("Native adapter extraction failed")
        return status.manifest if status.state == "succeeded" else None

    async def _adapter_authority(
        self, adapter_id: uuid.UUID
    ) -> tuple[TrainingArtifactManifest, str, uuid.UUID]:
        async with session_scope() as session:
            adapter = await session.get(TrainingAdapterRow, adapter_id)
            if adapter is None:
                raise TrainingConflict("Adapter is unavailable")
            await authorize_live_training_action(
                session, Principal.model_validate(adapter.metadata_record.get("authority"))
            )
            job = await current_job(session, adapter.source_job_id, lock=True)
            await recheck_training_base(
                session, ResolvedTrainingSpec.model_validate(job.resolved_spec)
            )
            adapter = await session.get(
                TrainingAdapterRow, adapter_id, populate_existing=True, with_for_update=True
            )
            assert adapter is not None
            if adapter.state not in {"validating", "replicating", "ready"} or (
                adapter.metadata_record.get("automatic") is True and job.state != "finalizing"
            ):
                raise TrainingConflict("Adapter preparation authority ended")
            manifest = TrainingArtifactManifest.model_validate(
                adapter.metadata_record.get("manifest")
            )
            if (
                manifest.artifact_id != adapter_id
                or manifest.canonical_sha256() != adapter.manifest_sha256
            ):
                raise TrainingConflict("Adapter manifest changed")
            source = await session.scalar(
                select(NodeRow.name)
                .join(TrainingParticipantRow, TrainingParticipantRow.node_id == NodeRow.id)
                .where(
                    TrainingParticipantRow.attempt_id == manifest.attempt_id,
                    TrainingParticipantRow.rank == 0,
                )
            )
            extraction_id = adapter.metadata_record.get("extraction_command_id")
            if isinstance(extraction_id, str):
                extraction = await session.get(TrainingCommandRow, uuid.UUID(extraction_id))
                if extraction is None or extraction.state != "succeeded":
                    raise TrainingConflict("Adapter extraction proof is unavailable")
                source = TrainingAdapterExtractRequest.model_validate(extraction.payload).node
            if source not in {"coire-edge-a", "coire-edge-b"}:
                raise TrainingConflict("Adapter source participant is missing")
            return manifest, source, job.owner_user_id

    @observed("coire.scheduler.training.runtime.replicate")
    async def mirror_adapter(self, adapter_id: uuid.UUID) -> bool:
        manifest, source, owner = await self._adapter_authority(adapter_id)
        destination = "coire-edge-b" if source == "coire-edge-a" else "coire-edge-a"
        for node in (source, destination):
            if node == destination:
                async with session_scope() as session:
                    copies = await verified_nodes(
                        session, adapter_id, manifest.canonical_sha256(), manifest.total_bytes
                    )
                if destination not in copies:
                    import_id = uuid.uuid5(adapter_id, "mirror:" + destination)
                    try:
                        status = await self.client.training_artifact_import_status(
                            destination, import_id
                        )
                    except NodeError as error:
                        if error.kind is not NodeErrorKind.NOT_FOUND:
                            raise
                        status = None
                    if status is not None and (
                        status.import_id != import_id
                        or status.artifact_id != adapter_id
                        or status.manifest_sha256 != manifest.canonical_sha256()
                    ):
                        raise TrainingConflict("Adapter import identity changed")
                    if status is None or status.state != "verified":
                        # Reauthorize every refresh; only hashes/IDs enter the journal.
                        await self._adapter_authority(adapter_id)
                        grant = await self.client.grant_training_artifact(
                            TrainingArtifactGrantRequest.model_validate(
                                {
                                    "command_id": uuid.uuid4(),
                                    "artifact_id": adapter_id,
                                    "manifest_sha256": manifest.canonical_sha256(),
                                    "source_node": source,
                                    "destination_node": destination,
                                    "attempt_id": manifest.attempt_id,
                                    "fence": manifest.fence,
                                    "file_ids": [f.id for f in manifest.files],
                                    "max_bytes": manifest.total_bytes,
                                    "expires_at": datetime.now(UTC)
                                    + timedelta(seconds=self.settings.training_transfer_grant_s),
                                }
                            )
                        )
                        intent = TrainingArtifactImportIntent.model_validate(
                            {
                                "command_id": import_id,
                                "artifact_id": adapter_id,
                                "manifest_sha256": manifest.canonical_sha256(),
                                "source_node": source,
                                "destination_node": destination,
                                "attempt_id": manifest.attempt_id,
                                "fence": manifest.fence,
                                "grant_id": grant.grant_id,
                            }
                        )
                        if status is None:
                            async with session_scope() as session:
                                job = await current_job(session, manifest.job_id or "", lock=True)
                                adapter = await session.get(TrainingAdapterRow, adapter_id)
                                if adapter is None or (
                                    adapter.metadata_record.get("automatic") is True
                                    and job.state != "finalizing"
                                ):
                                    raise TrainingConflict("Adapter replication was cancelled")
                                prior = await session.get(TrainingCommandRow, import_id)
                                if prior is None:
                                    session.add(
                                        TrainingCommandRow(
                                            id=import_id,
                                            actor_user_id=owner,
                                            idempotency_key=f"adapter-mirror:{adapter_id}",
                                            operation="node.adapter.import",
                                            subject_id=destination,
                                            job_id=job.id,
                                            attempt_id=manifest.attempt_id,
                                            request_sha256=payload_digest(intent),
                                            payload=intent.model_dump(mode="json"),
                                            state="dispatching",
                                        )
                                    )
                                else:
                                    persisted = TrainingArtifactImportIntent.model_validate(
                                        prior.payload
                                    )
                                    if persisted.model_dump(
                                        exclude={"grant_id"}
                                    ) != intent.model_dump(
                                        exclude={"grant_id"}
                                    ) or prior.request_sha256 != payload_digest(persisted):
                                        raise TrainingConflict("Adapter replication intent changed")
                                    intent = persisted
                            # Grant identity is refreshable and not part of artifact identity.
                            status = await self.client.import_training_artifact(
                                TrainingArtifactImportRequest.model_validate(
                                    {
                                        **intent.model_dump(mode="json"),
                                        "grant_id": grant.grant_id,
                                        "grant_secret": grant.secret,
                                    }
                                )
                            )
                        else:
                            status = await self.client.refresh_training_artifact(
                                destination,
                                import_id,
                                TrainingArtifactGrantRefresh.model_validate(
                                    {
                                        "command_id": uuid.uuid4(),
                                        "artifact_id": adapter_id,
                                        "manifest_sha256": manifest.canonical_sha256(),
                                        "attempt_id": manifest.attempt_id,
                                        "fence": manifest.fence,
                                        "grant_id": grant.grant_id,
                                        "grant_secret": grant.secret,
                                    }
                                ),
                            )
                        if status.state != "verified":
                            return False
                    if (
                        status.verified_manifest is None
                        or status.verified_manifest.canonical_sha256()
                        != manifest.canonical_sha256()
                    ):
                        raise TrainingConflict("Peer imported a different adapter manifest")
            proof = await self.client.verify_training_artifact(
                node,
                adapter_id,
                TrainingArtifactVerifyRequest(
                    command_id=uuid.uuid5(adapter_id, "verify:" + node),
                    manifest_sha256=manifest.canonical_sha256(),
                ),
            )
            await self._adapter_authority(adapter_id)
            async with session_scope() as session:
                await record_verified_copy(session, manifest, proof, adapter_id=adapter_id)
                if node == destination:
                    row = await session.get(
                        TrainingCommandRow,
                        uuid.uuid5(adapter_id, "mirror:" + destination),
                        with_for_update=True,
                    )
                    if row is not None:
                        row.receipt, row.state = proof.model_dump(mode="json"), "succeeded"
        return True

    @observed("coire.scheduler.training.runtime.smoke")
    async def prepare_adapter(self, adapter_id: uuid.UUID) -> tuple[uuid.UUID, EngineStatus] | None:
        if not await self.mirror_adapter(adapter_id):
            return None
        await self._adapter_authority(adapter_id)
        from coire_api.instance.service import append_initial_transition

        instance_id = uuid.uuid5(adapter_id, "validation-smoke")
        async with session_scope() as session:
            adapter = await session.get(TrainingAdapterRow, adapter_id, with_for_update=True)
            assert adapter is not None
            instance = await session.get(ModelInstanceRow, instance_id)
            if instance is None:
                # Placement owns admission/holds/commands. This private instance is
                # never a public target until the fenced adapter finalizer succeeds.
                adapter.metadata_record = {
                    **adapter.metadata_record,
                    "smoke_instance_id": str(instance_id),
                }
                instance = ModelInstanceRow(
                    id=instance_id,
                    model_id=adapter.model_id,
                    variant_id=adapter.base_variant_id,
                    adapter_id=adapter.id,
                    policy="single:auto",
                    state=InstanceState.REQUESTED,
                )
                session.add(instance)
                await session.flush()
                await append_initial_transition(session, instance)
            if instance.state is InstanceState.FAILED:
                raise TrainingConflict("Reserved adapter smoke placement failed")
            if instance.state is not InstanceState.READY:
                return None
            member = await session.scalar(
                select(InstanceMemberRow).where(InstanceMemberRow.instance_id == instance_id)
            )
            if member is None or member.engine_id is None:
                raise TrainingConflict("Reserved smoke member is missing")
            node = await session.get(NodeRow, member.node_id)
            assert node is not None
            node_name, engine_id = node.name, member.engine_id
        # Node READY proves bare generation, not merely /health. Finalizer checks
        # target digests, PID/create time, freshness and actual HELD reservation.
        status = await self.client.get_engine(node_name, engine_id)
        return (instance_id, status) if status.state is EngineState.READY else None

    @observed("coire.scheduler.training.runtime.guard")
    async def guard_reason(self, attempt_id: str) -> TrainingReason | None:
        from coire_scheduler.training_guard import guard_reason

        return await guard_reason(attempt_id)

    async def resume_profile(self, job_id: str) -> TrainingProfile | None:
        from coire_scheduler.training_guard import resume_profile

        return await resume_profile(job_id)


class TrainingRuntimeWorker:
    """Own controller/client and baseline poller even while new training is disabled."""

    def __init__(self, settings: Settings) -> None:
        # Registration is local: the controller imports the typed executor.
        from coire_scheduler.training_controller import (
            TrainingController,
            TrainingExecutorTransport,
        )

        class LifecycleController(TrainingController):
            async def tick(controller, job_id: str) -> None:
                if not settings.training_enabled:
                    async with session_scope() as session:
                        job = await current_job(session, job_id, lock=True)
                        active = await session.scalar(
                            select(TrainingAttemptRow.id).where(
                                TrainingAttemptRow.job_id == job_id,
                                TrainingAttemptRow.state.in_(
                                    ["preparing", "running", "stopping", "unknown"]
                                ),
                            )
                        )
                        if active is None and job.state != "finalizing":
                            return
                        if job.state not in {"cancelling", "cancelled", "failed", "succeeded"}:
                            from coire_api.training.events import append_event
                            from coire_core.models.training import TrainingStateEvent

                            job.state, job.safe_reason = "cancelling", "cancelled"
                            job.version += 1
                            job.updated_at = datetime.now(UTC)
                            await append_event(
                                session,
                                job_id,
                                TrainingStateEvent.model_validate(
                                    {"kind": "state", "state": "cancelling", "reason": "cancelled"}
                                ),
                            )
                await super().tick(job_id)

        self.client = TrainingNodeClient(settings, timeout=5.0)
        self.runtime = TrainingRuntime(settings, self.client)
        from coire_api.training.gateway_measurements import gateway_measurement_generate
        from coire_scheduler.training_components import TrainingComponentCoordinator
        from coire_scheduler.training_guard import guard_reason, resume_profile
        from coire_scheduler.training_measurements import (
            GatewayWorkloadDriver,
            MeasurementNodeClient,
            TrainingMeasurementExecutor,
        )

        self.components = TrainingComponentCoordinator(settings, self.client)
        from coire_scheduler.training_retention import TrainingRetentionWorker

        self.retention = TrainingRetentionWorker(self.client)
        self.measurement_client = MeasurementNodeClient(settings, timeout=5.0)
        self.measurements = TrainingMeasurementExecutor(
            settings,
            self.measurement_client,
            GatewayWorkloadDriver(gateway_measurement_generate(settings)),
        )
        self.controller = LifecycleController(
            TrainingExecutorTransport(
                self.client,
                extract_final_adapter=self.runtime.extract_final_adapter,
                prepare_adapter=self.runtime.prepare_adapter,
                guard_reason=guard_reason,
                resume_profile=resume_profile,
            )
        )
        self._stop = asyncio.Event()
        self._metrics: asyncio.Task[None] | None = None
        self._promotions: asyncio.Task[None] | None = None
        self._measurement_recovery: asyncio.Task[None] | None = None

    async def start(self) -> None:
        from coire_scheduler.training_metrics import poll_training_metrics

        self._stop.clear()
        try:
            await self.controller.start()
            await self.components.start()
            await self.retention.start()
            if self.client._settings.training_enabled:
                await self.measurements.start()
            else:
                # The executor's start gate is for new work. Existing persisted
                # dispatches must still be stopped/reconciled after disable.
                self._measurement_recovery = asyncio.create_task(
                    self._poll_measurement_recovery(), name="training-measurement-recovery"
                )
            self._metrics = asyncio.create_task(
                poll_training_metrics(self._stop), name="training-baseline-metrics"
            )
            self._promotions = asyncio.create_task(
                self._poll_promotions(), name="training-adapter-promotions"
            )
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stop.set()
        try:
            await self.controller.stop()
            await self.components.stop()
            await self.retention.stop()
            if self._measurement_recovery is not None:
                self._measurement_recovery.cancel()
                await asyncio.gather(self._measurement_recovery, return_exceptions=True)
                self._measurement_recovery = None
            await self.measurements.stop()
            if self._promotions is not None:
                self._promotions.cancel()
                await asyncio.gather(self._promotions, return_exceptions=True)
                self._promotions = None
            if self._metrics is not None:
                self._metrics.cancel()
                await asyncio.gather(self._metrics, return_exceptions=True)
                self._metrics = None
        finally:
            await self.measurement_client.aclose()
            await self.client.aclose()

    async def _poll_measurement_recovery(self) -> None:
        from coire_api.polling import wait_or_stop

        while not self._stop.is_set():
            try:
                await self._recover_measurements_once()
            except Exception:
                logger.warning(
                    "measurement recovery deferred", extra={"operation": "measurement.recovery"}
                )
            await wait_or_stop(self._stop, 1.0)

    @observed("coire.scheduler.training.measurement.recovery")
    async def _recover_measurements_once(self) -> None:
        async with session_scope() as session:
            identities = list(
                await session.scalars(
                    select(TrainingMeasurementRow.id)
                    .join(
                        TrainingCommandRow,
                        TrainingCommandRow.subject_id == cast(TrainingMeasurementRow.id, String),
                    )
                    .where(
                        TrainingMeasurementRow.state == "running",
                        TrainingCommandRow.operation == "training.measurement",
                        TrainingCommandRow.payload["dispatch"].as_string().is_not(None),
                    )
                    .limit(8)
                )
            )
        results = await asyncio.gather(
            *(self.measurements.advance(identity) for identity in identities),
            return_exceptions=True,
        )
        if any(isinstance(result, Exception) for result in results):
            logger.warning(
                "measurement stop awaiting reconciliation",
                extra={"operation": "measurement.recovery"},
            )

    async def _poll_promotions(self) -> None:
        from coire_api.polling import wait_or_stop

        while not self._stop.is_set():
            try:
                await self.runtime.reconcile_promotions()
            except Exception:
                logger.warning("adapter reconciliation deferred", extra={"operation": "promotions"})
            await wait_or_stop(self._stop, 1.0)
