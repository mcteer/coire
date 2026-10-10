"""Typed authenticated control-fabric transport for persisted training commands."""

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from coire_api.db import TrainingAttemptRow, TrainingCommandRow, session_scope
from coire_api.nodes_client import NodeClient, NodeError, NodeErrorKind
from coire_api.training.service import payload_digest
from coire_core.errors import TrainingConflict
from coire_core.models import training_node
from coire_core.models.training_node import (
    CheckpointAcknowledgementDocument,
    CheckpointCommitAcknowledgement,
    NodeTrainingEventPage,
    NodeTrainingStatus,
    TrainingAdapterExtractionStatus,
    TrainingAdapterExtractRequest,
    TrainingArtifactDeleteRequest,
    TrainingArtifactDeletionReceipt,
    TrainingArtifactGrantIssued,
    TrainingArtifactGrantRefresh,
    TrainingArtifactGrantRequest,
    TrainingArtifactImportRequest,
    TrainingArtifactImportStatus,
    TrainingArtifactVerificationReceipt,
    TrainingArtifactVerifyRequest,
    TrainingAttemptCleanupReceipt,
    TrainingAttemptCleanupRequest,
    TrainingCommand,
    TrainingInputsRequest,
    TrainingLeaseRenewal,
    TrainingPauseRequest,
    TrainingPrepared,
    TrainingPrepareRequest,
    TrainingRankCollection,
    TrainingRankComponentManifest,
    TrainingRankGrantRequest,
    TrainingRankImportRequest,
    TrainingRankImportStatus,
    TrainingRankVerificationReceipt,
    TrainingStartReceipt,
    TrainingStartRequest,
    TrainingStopReceipt,
    TrainingStopRequest,
    parse_checkpoint_acknowledgement,
)
from coire_scheduler.training import observe_training, reduce_command_receipt
from coire_scheduler.training_recovery import record_stop_proof


async def stop_with_rejected_prepare(
    client: "TrainingNodeClient", stop: TrainingStopRequest
) -> TrainingStopReceipt:
    async with asyncio.timeout(5):
        try:
            return await client.stop_training(stop)
        except NodeError as error:
            if error.kind is not NodeErrorKind.CONFLICT:
                raise
        async with session_scope() as session:
            row = await session.scalar(
                select(TrainingCommandRow).where(
                    TrainingCommandRow.operation == "node.training.prepare",
                    TrainingCommandRow.attempt_id == stop.attempt_id,
                    TrainingCommandRow.subject_id == stop.node,
                )
            )
            if row is None:
                raise TrainingConflict("Stop has no immutable preparation to fence")
            prepare = TrainingPrepareRequest.model_validate(row.payload)
            if (
                prepare.command_id != row.id
                or payload_digest(prepare) != row.request_sha256
                or prepare.lease_expires_at > datetime.now(UTC)
                or any(
                    getattr(prepare, field) != getattr(stop, field)
                    for field in (
                        "job_id",
                        "attempt_id",
                        "fence",
                        "node",
                        "rank",
                        "world_size",
                        "request_sha256",
                    )
                )
            ):
                raise TrainingConflict("Stop rebind requires its exact expired preparation")
        # Expired immutable prepare can only acquire a fenced no-start rejection,
        # never a new trainer/input delivery. HTTP failure is still not death proof.
        try:
            await client.prepare_training(prepare)
        except NodeError as error:
            if error.kind is not NodeErrorKind.CONFLICT:
                raise
        return await client.stop_training(stop)


async def dispatch_training_command(command_id: uuid.UUID, client: "TrainingNodeClient") -> None:
    """Persist dispatch uncertainty before I/O; retries reuse the exact command UUID."""
    async with session_scope() as session:
        from datetime import UTC, datetime

        from coire_api.auth import Principal
        from coire_api.training.authorization import authorize_live_training_action
        from coire_api.training.events import current_job

        row = await session.get(TrainingCommandRow, command_id, populate_existing=True)
        if row is None or row.state not in {"pending", "dispatching"}:
            return
        # This command has distinct checkpoint/promotion authority, not the trainer lease.
        extraction = row.operation == "node.adapter.extract"
        if row.operation == "node.training.prepare" and row.attempt_id is not None:
            from coire_scheduler.training_components import bind_collective_prepares

            await bind_collective_prepares(session, row.attempt_id, client._settings)
    if extraction:
        await dispatch_adapter_extraction(command_id, client)
        return
    async with session_scope() as session:
        row = await session.get(TrainingCommandRow, command_id, populate_existing=True)
        if row is None or row.state not in {"pending", "dispatching"}:
            return
        if row.job_id is None:
            raise TrainingConflict("Runtime command has no job binding")
        job = await current_job(session, row.job_id)
        if row.operation not in {"node.training.stop", "node.training.pause"}:
            await authorize_live_training_action(
                session, Principal.model_validate(job.authorization_snapshot)
            )
        job = await current_job(session, row.job_id, lock=True)
        row = await session.get(
            TrainingCommandRow, command_id, populate_existing=True, with_for_update=True
        )
        assert row is not None
        operation, payload = row.operation, row.payload
        if not client._settings.training_enabled and operation in {
            "node.training.prepare",
            "node.training.start",
            "node.training.lease",
        }:
            raise TrainingConflict("Training execution is disabled")
        if operation not in {
            "node.training.prepare",
            "node.training.start",
            "node.training.stop",
            "node.training.pause",
            "node.training.lease",
            "node.training.checkpoint-commit",
        }:
            raise TrainingConflict("Unsupported runtime command")
        types: dict[str, type[TrainingCommand]] = {
            "node.training.prepare": TrainingPrepareRequest,
            "node.training.start": TrainingStartRequest,
            "node.training.stop": TrainingStopRequest,
            "node.training.pause": TrainingPauseRequest,
            "node.training.lease": TrainingLeaseRenewal,
            "node.training.checkpoint-commit": CheckpointCommitAcknowledgement,
        }
        request = (
            parse_checkpoint_acknowledgement(payload)
            if operation == "node.training.checkpoint-commit"
            else types[operation].model_validate(payload)
        )
        if (
            request.command_id != command_id
            or request.node != row.subject_id
            or payload_digest(request) != row.request_sha256
        ):
            raise TrainingConflict("Persisted runtime command identity changed")
        authority_expires_at = request.lease_expires_at
        if (
            isinstance(request, TrainingPrepareRequest)
            and request.resolved.spec.schema_version == 3
        ):
            attempt = await session.get(
                TrainingAttemptRow, request.attempt_id, populate_existing=True
            )
            if (
                attempt is None
                or attempt.job_id != job.id
                or attempt.fence != request.fence
                or attempt.state != "preparing"
            ):
                raise TrainingConflict("Preparation has no current attempt authority")
            authority_expires_at = attempt.lease_expires_at
        if operation not in {"node.training.stop", "node.training.pause"} and (
            request.fence != job.fence
            or job.state not in {"reserving", "running", "pausing"}
            or authority_expires_at <= datetime.now(UTC)
        ):
            raise TrainingConflict("Runtime execution authority expired or was cancelled")
        row.state = "dispatching"
    # A lost acknowledgement must remain dispatching, with all holds counted.
    if operation == "node.training.prepare":
        command = TrainingPrepareRequest.model_validate(payload)
        receipt = await client.prepare_training(command)
        if not receipt.ready and receipt.reason in {None, "analysis_pending"}:
            from coire_api.training.input_grants import mint_attempt_inputs

            async with session_scope() as session:
                inputs = await mint_attempt_inputs(session, command, client._settings)
            # The hash-only grant transaction commits before native delivery. A retry
            # issues fresh authority and the supervisor refreshes its pending download.
            await client.deliver_training_inputs(inputs)
            receipt = await client.prepare_training(command)
        if not receipt.ready and receipt.reason in {None, "analysis_pending"}:
            return  # Never journal a transient receipt as immutable preparation success.
        async with session_scope() as session:
            from coire_api.training.input_grants import attempt_sources

            await attempt_sources(session, command)
            await reduce_command_receipt(session, command_id, receipt)
    elif operation == "node.training.start":
        start = TrainingStartRequest.model_validate(payload)
        started = await client.start_training(start)
        async with session_scope() as session:
            await reduce_command_receipt(session, command_id, started)
    elif operation == "node.training.stop":
        stop = TrainingStopRequest.model_validate(payload)
        stopped = await stop_with_rejected_prepare(client, stop)
        async with session_scope() as session:
            await record_stop_proof(session, stop.attempt_id, stopped)
            row = await session.get(TrainingCommandRow, command_id, with_for_update=True)
            assert row is not None
            row.receipt, row.state = stopped.model_dump(mode="json"), "succeeded"
    elif operation == "node.training.checkpoint-commit":
        acknowledgement = parse_checkpoint_acknowledgement(payload)
        status = await client.acknowledge_checkpoint(acknowledgement)
        async with session_scope() as session:
            row = await session.get(TrainingCommandRow, command_id, with_for_update=True)
            assert row is not None
            row.receipt, row.state = status.model_dump(mode="json"), "succeeded"
    else:
        if operation == "node.training.pause":
            status = await client.pause_training(TrainingPauseRequest.model_validate(payload))
        else:
            status = await client.renew_training(TrainingLeaseRenewal.model_validate(payload))
        async with session_scope() as session:
            await observe_training(session, status)
            row = await session.get(TrainingCommandRow, command_id, with_for_update=True)
            assert row is not None
            row.receipt, row.state = status.model_dump(mode="json"), "succeeded"


class TrainingNodeClient(NodeClient):
    """API credential boundary; scheduler persists intent and never holds node secrets."""

    async def cleanup_training_attempt(
        self, request: TrainingAttemptCleanupRequest
    ) -> TrainingAttemptCleanupReceipt:
        _, body = await self._call(
            "POST",
            request.node,
            f"/node/training/attempts/{request.attempt_id}/cleanup",
            json=request.model_dump(mode="json"),
            request_timeout_s=5.0,
        )
        receipt = TrainingAttemptCleanupReceipt.model_validate(body)
        if (
            receipt.command_id != request.command_id
            or receipt.job_id != request.job_id
            or receipt.attempt_id != request.attempt_id
            or receipt.fence != request.fence
            or receipt.node != request.node
        ):
            raise TrainingConflict("Cleanup response differs from its authenticated scope")
        return receipt

    async def delete_training_artifact(
        self, node: str, artifact_id: uuid.UUID, request: TrainingArtifactDeleteRequest
    ) -> TrainingArtifactDeletionReceipt:
        _, body = await self._call(
            "DELETE",
            node,
            f"/node/training/artifacts/{artifact_id}",
            json=request.model_dump(mode="json"),
            request_timeout_s=5.0,
        )
        receipt = TrainingArtifactDeletionReceipt.model_validate(body)
        if receipt.command_id != request.command_id or receipt.artifact_id != artifact_id:
            raise TrainingConflict("Cleanup receipt differs from the authenticated request")
        return receipt

    async def rank_component(
        self, node: str, artifact_id: uuid.UUID, rank: int
    ) -> TrainingRankComponentManifest:
        _, body = await self._call(
            "GET",
            node,
            f"/node/training/components/{artifact_id}/ranks/{rank}",
            expect=(200,),
        )
        component = TrainingRankComponentManifest.model_validate(body)
        if component.artifact_id != artifact_id or component.rank != rank:
            raise TrainingConflict("Rank metadata differs from authenticated request")
        return component

    async def verify_rank_component(
        self, node: str, component: TrainingRankComponentManifest, command_id: uuid.UUID
    ) -> TrainingRankVerificationReceipt:
        request = TrainingArtifactVerifyRequest(
            command_id=command_id, manifest_sha256=component.canonical_sha256()
        )
        _, body = await self._call(
            "POST",
            node,
            f"/node/training/components/{component.artifact_id}/ranks/{component.rank}/verify",
            json=request.model_dump(mode="json"),
            expect=(200,),
        )
        receipt = TrainingRankVerificationReceipt.model_validate(body)
        if (
            receipt.node != node
            or receipt.command_id != command_id
            or receipt.component != component
        ):
            raise TrainingConflict("Rank verification differs from authenticated request")
        return receipt

    async def grant_rank_component(
        self, request: TrainingRankGrantRequest
    ) -> TrainingArtifactGrantIssued:
        _, body = await self._call(
            "POST",
            request.source_node,
            "/node/training/components/grants",
            json=request.model_dump(mode="json"),
            expect=(200,),
        )
        return TrainingArtifactGrantIssued.model_validate(body)

    async def import_rank_component(
        self, request: TrainingRankImportRequest
    ) -> TrainingRankImportStatus:
        _, body = await self._call(
            "POST",
            request.destination_node,
            "/node/training/components/imports",
            json=request.model_dump(mode="json"),
            expect=(202,),
        )
        status = TrainingRankImportStatus.model_validate(body)
        if status.import_id != request.command_id or status.component != request.component:
            raise TrainingConflict("Rank import differs from authenticated request")
        return status

    async def rank_import_status(self, node: str, import_id: uuid.UUID) -> TrainingRankImportStatus:
        _, body = await self._call(
            "GET", node, f"/node/training/components/imports/{import_id}", expect=(200,)
        )
        status = TrainingRankImportStatus.model_validate(body)
        if status.import_id != import_id:
            raise TrainingConflict("Rank import status identifies another command")
        return status

    async def refresh_rank_import(
        self, node: str, import_id: uuid.UUID, request: TrainingArtifactGrantRefresh
    ) -> TrainingRankImportStatus:
        _, body = await self._call(
            "POST",
            node,
            f"/node/training/components/imports/{import_id}/grant",
            json=request.model_dump(mode="json"),
            expect=(202,),
        )
        status = TrainingRankImportStatus.model_validate(body)
        if (
            status.import_id != import_id
            or status.component.artifact_id != request.artifact_id
            or status.component.canonical_sha256() != request.manifest_sha256
            or status.component.attempt_id != request.attempt_id
            or status.component.fence != request.fence
        ):
            raise TrainingConflict("Rank refresh response expands component scope")
        return status

    async def cancel_rank_import(self, node: str, import_id: uuid.UUID) -> TrainingRankImportStatus:
        _, body = await self._call(
            "POST",
            node,
            f"/node/training/components/imports/{import_id}/cancel",
            expect=(200,),
        )
        status = TrainingRankImportStatus.model_validate(body)
        if status.import_id != import_id:
            raise TrainingConflict("Rank import cancellation identifies another command")
        return status

    async def collect_training_ranks(self, command: TrainingRankCollection) -> NodeTrainingStatus:
        _, body = await self._call(
            "POST",
            command.node,
            f"/node/training/attempts/{command.attempt_id}/rank-collection",
            json=command.model_dump(mode="json"),
            expect=(200,),
        )
        status = NodeTrainingStatus.model_validate(body)
        if (
            status.node != command.node
            or status.attempt_id != command.attempt_id
            or status.fence != command.fence
            or status.job_id != command.job_id
        ):
            raise TrainingConflict("Collection status identifies another participant")
        return status

    async def acknowledge_checkpoint(
        self, command: CheckpointAcknowledgementDocument
    ) -> NodeTrainingStatus:
        _, body = await self._call(
            "POST",
            command.node,
            f"/node/training/attempts/{command.attempt_id}/checkpoint-commit",
            json=command.model_dump(mode="json"),
            expect=(200,),
        )
        status = NodeTrainingStatus.model_validate(body)
        if (
            status.attempt_id != command.attempt_id
            or status.job_id != command.job_id
            or status.node != command.node
            or status.fence != command.fence
        ):
            raise TrainingConflict("Checkpoint acknowledgement status identity differs")
        return status

    async def verify_training_artifact(
        self, node: str, artifact_id: uuid.UUID, request: TrainingArtifactVerifyRequest
    ) -> TrainingArtifactVerificationReceipt:
        _, body = await self._call(
            "POST",
            node,
            f"/node/training/artifacts/{artifact_id}/verify",
            json=request.model_dump(mode="json"),
        )
        receipt = TrainingArtifactVerificationReceipt.model_validate(body)
        if receipt.node != node or receipt.command_id != request.command_id:
            raise TrainingConflict("Artifact proof differs from the authenticated node request")
        return receipt

    async def grant_training_artifact(
        self, request: TrainingArtifactGrantRequest
    ) -> TrainingArtifactGrantIssued:
        _, body = await self._call(
            "POST",
            request.source_node,
            "/node/training/artifacts/grants",
            json=request.model_dump(mode="json"),
        )
        return TrainingArtifactGrantIssued.model_validate(body)

    async def import_training_artifact(
        self, request: TrainingArtifactImportRequest
    ) -> TrainingArtifactImportStatus:
        _, body = await self._call(
            "POST",
            request.destination_node,
            "/node/training/artifacts/imports",
            json=request.model_dump(mode="json"),
        )
        return TrainingArtifactImportStatus.model_validate(body)

    async def training_artifact_import_status(
        self, node: str, import_id: uuid.UUID
    ) -> TrainingArtifactImportStatus:
        _, body = await self._call("GET", node, f"/node/training/artifacts/imports/{import_id}")
        return TrainingArtifactImportStatus.model_validate(body)

    async def cancel_training_artifact_import(
        self, node: str, import_id: uuid.UUID
    ) -> TrainingArtifactImportStatus:
        _, body = await self._call(
            "POST", node, f"/node/training/artifacts/imports/{import_id}/cancel"
        )
        status = TrainingArtifactImportStatus.model_validate(body)
        if status.import_id != import_id:
            raise TrainingConflict("Import cancellation response identifies another command")
        return status

    async def refresh_training_artifact(
        self, node: str, import_id: uuid.UUID, request: TrainingArtifactGrantRefresh
    ) -> TrainingArtifactImportStatus:
        _, body = await self._call(
            "POST",
            node,
            f"/node/training/artifacts/imports/{import_id}/grant",
            json=request.model_dump(mode="json"),
        )
        return TrainingArtifactImportStatus.model_validate(body)

    async def extract_adapter(
        self, command: TrainingAdapterExtractRequest
    ) -> TrainingAdapterExtractionStatus:
        _, body = await self._call(
            "POST",
            command.node,
            "/node/training/adapters/extractions",
            json=command.model_dump(mode="json"),
            request_timeout_s=5.0,
        )
        return TrainingAdapterExtractionStatus.model_validate(body)

    async def adapter_extraction_status(
        self, node: str, command_id: uuid.UUID
    ) -> TrainingAdapterExtractionStatus:
        _, body = await self._call("GET", node, f"/node/training/adapters/extractions/{command_id}")
        return TrainingAdapterExtractionStatus.model_validate(body)

    async def deliver_training_inputs(self, command: TrainingInputsRequest) -> TrainingPrepared:
        _, body = await self._call(
            "POST",
            command.node,
            f"/node/training/attempts/{command.attempt_id}/inputs",
            json=command.model_dump(mode="json"),
            expect=(202,),
            request_timeout_s=5.0,
        )
        result = TrainingPrepared.model_validate(body)
        if (
            result.node != command.node
            or result.attempt_id != command.attempt_id
            or result.fence != command.fence
        ):
            raise TrainingConflict("Input receipt belongs to a different participant")
        return result

    async def prepare_training(self, command: TrainingPrepareRequest) -> TrainingPrepared:
        # V3 also validates the registered initial adapter before launch. Keep
        # that bounded preparation separate from the five-second stop lane.
        _, body = await self._call(
            "POST",
            command.node,
            f"/node/training/attempts/{command.attempt_id}/prepare",
            json=command.model_dump(mode="json"),
            request_timeout_s=30.0 if command.resolved.spec.schema_version == 3 else None,
        )
        return TrainingPrepared.model_validate(body)

    async def start_training(self, command: TrainingStartRequest) -> TrainingStartReceipt:
        _, body = await self._call(
            "POST",
            command.node,
            f"/node/training/attempts/{command.attempt_id}/start",
            json=command.model_dump(mode="json"),
        )
        return TrainingStartReceipt.model_validate(body)

    async def stop_training(self, command: TrainingStopRequest) -> TrainingStopReceipt:
        _, body = await self._call(
            "POST",
            command.node,
            f"/node/training/attempts/{command.attempt_id}/stop",
            json=command.model_dump(mode="json"),
            request_timeout_s=5.0,
        )
        return TrainingStopReceipt.model_validate(body)

    async def pause_training(self, command: TrainingPauseRequest) -> NodeTrainingStatus:
        _, body = await self._call(
            "POST",
            command.node,
            f"/node/training/attempts/{command.attempt_id}/pause",
            json=command.model_dump(mode="json"),
            request_timeout_s=5.0,
        )
        return NodeTrainingStatus.model_validate(body)

    async def renew_training(self, command: TrainingLeaseRenewal) -> NodeTrainingStatus:
        _, body = await self._call(
            "POST",
            command.node,
            f"/node/training/attempts/{command.attempt_id}/lease",
            json=command.model_dump(mode="json"),
            request_timeout_s=5.0,
        )
        return NodeTrainingStatus.model_validate(body)

    async def training_status(self, node: str, attempt_id: str) -> NodeTrainingStatus:
        _, body = await self._call("GET", node, f"/node/training/attempts/{attempt_id}")
        return NodeTrainingStatus.model_validate(body)

    async def training_events(
        self, node: str, attempt_id: str, after: int
    ) -> NodeTrainingEventPage:
        if after < 0:
            raise ValueError("invalid node event cursor")
        _, body = await self._call(
            "GET", node, f"/node/training/attempts/{attempt_id}/events?after={after}"
        )
        return NodeTrainingEventPage.model_validate(body)


async def dispatch_adapter_extraction(command_id: uuid.UUID, client: TrainingNodeClient) -> None:
    from datetime import UTC, datetime

    from coire_api.auth import Principal
    from coire_api.db import TrainingAdapterRow
    from coire_api.training.adapters import stage_serving_adapter
    from coire_api.training.authorization import authorize_live_training_action

    request_type = getattr(training_node, "TrainingAdapterExtractRequest", None)
    if request_type is None:
        raise TrainingConflict("Adapter extraction contract is unavailable")
    async with session_scope() as session:
        row = await session.get(TrainingCommandRow, command_id, populate_existing=True)
        if (
            row is None
            or row.operation != "node.adapter.extract"
            or row.state not in {"pending", "dispatching"}
        ):
            return
        command = request_type.model_validate(row.payload)
        candidate = await session.get(TrainingAdapterRow, command.adapter_id)
        if candidate is None:
            raise TrainingConflict("Extraction target was not reserved")
        principal = Principal.model_validate(candidate.metadata_record.get("authority"))
        await authorize_live_training_action(session, principal)
        candidate = await session.get(
            TrainingAdapterRow, command.adapter_id, populate_existing=True, with_for_update=True
        )
        if (
            candidate is None
            or candidate.state != "validating"
            or command.deadline <= datetime.now(UTC)
        ):
            raise TrainingConflict("Extraction target is no longer authorized")
        row = await session.get(
            TrainingCommandRow, command_id, populate_existing=True, with_for_update=True
        )
        assert row is not None
        if (
            command.command_id != row.id
            or command.node != row.subject_id
            or payload_digest(command) != row.request_sha256
        ):
            raise TrainingConflict("Extraction request identity changed")
        prior_dispatch = row.state == "dispatching"
        row.state = "dispatching"
    status = (
        await client.adapter_extraction_status(command.node, command_id)
        if prior_dispatch
        else await client.extract_adapter(command)
    )
    # Status was validated against the shared strict result before inspecting fields.
    status_type = getattr(training_node, "TrainingAdapterExtractionStatus", None)
    if status_type is None:
        raise TrainingConflict("Adapter extraction contract is unavailable")
    observed = status_type.model_validate(status.model_dump(mode="json"))
    if (
        observed.command_id != command_id
        or observed.adapter_id != command.adapter_id
        or observed.checkpoint_id != command.checkpoint_id
        or observed.node != command.node
    ):
        raise TrainingConflict("Extraction status differs from the reserved command")
    if observed.state in {"queued", "running"}:
        return
    async with session_scope() as session:
        if observed.state == "succeeded":
            await stage_serving_adapter(
                session, principal, command.checkpoint_id, observed.manifest, slug=candidate.slug
            )
        else:
            await authorize_live_training_action(session, principal)
            candidate = await session.get(
                TrainingAdapterRow, command.adapter_id, populate_existing=True, with_for_update=True
            )
            assert candidate is not None
            candidate.state = "failed"
            candidate.version += 1
        row = await session.get(
            TrainingCommandRow, command_id, populate_existing=True, with_for_update=True
        )
        assert row is not None
        row.receipt, row.state = (
            observed.model_dump(mode="json"),
            "succeeded" if observed.state == "succeeded" else "failed",
        )


async def observe_training_attempt(attempt_id: str, client: TrainingNodeClient) -> None:
    """Drain persisted native mailbox events before reducing actual liveness."""
    from sqlalchemy import func, select

    from coire_api.db import NodeRow, TrainingParticipantRow
    from coire_scheduler.training import ingest_training_event

    async with session_scope() as session:
        participants = list(
            (
                await session.execute(
                    select(NodeRow.name)
                    .join(TrainingParticipantRow, TrainingParticipantRow.node_id == NodeRow.id)
                    .where(TrainingParticipantRow.attempt_id == attempt_id)
                )
            ).scalars()
        )
    for node in participants:
        async with session_scope() as session:
            cursor = int(
                await session.scalar(
                    select(
                        func.coalesce(
                            func.max(TrainingCommandRow.payload["sequence"].as_integer()), 0
                        )
                    ).where(
                        TrainingCommandRow.operation == "node.training.event",
                        TrainingCommandRow.attempt_id == attempt_id,
                        TrainingCommandRow.subject_id == node,
                    )
                )
                or 0
            )
        try:
            page = await client.training_events(node, attempt_id, cursor)
            if page.next_sequence != (page.items[-1].sequence if page.items else cursor):
                raise TrainingConflict("Node mailbox cursor differs from the recorded page")
            for event in page.items:
                async with session_scope() as session:
                    await ingest_training_event(session, node, event)
        finally:
            status = await client.training_status(node, attempt_id)
            async with session_scope() as session:
                await observe_training(session, status)


async def mirror_checkpoint(checkpoint_id: uuid.UUID, client: TrainingNodeClient) -> bool:
    """One bounded mirroring tick; no tensor bytes or transfer secrets are persisted on core."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from coire_api.auth import Principal
    from coire_api.db import (
        NodeRow,
        TrainingCheckpointRow,
        TrainingParticipantRow,
    )
    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_api.training.authorization import authorize_live_training_action
    from coire_api.training.checkpoints import record_verified_copy
    from coire_api.training.events import current_attempt, current_job
    from coire_core.models.training_node import (
        TrainingArtifactImportIntent,
        TrainingArtifactManifest,
    )
    from coire_scheduler.training import enqueue_checkpoint_acknowledgements, enqueue_controls

    async with session_scope() as session:
        checkpoint = await session.get(TrainingCheckpointRow, checkpoint_id)
        if checkpoint is None:
            raise TrainingConflict("Checkpoint was not staged")
        job = await current_job(session, checkpoint.job_id)
        await authorize_live_training_action(
            session, Principal.model_validate(job.authorization_snapshot)
        )
        job = await current_job(session, checkpoint.job_id, lock=True)
        await current_attempt(session, job, checkpoint.attempt_id, checkpoint.fence)
        if checkpoint.state == "committed":
            await enqueue_checkpoint_acknowledgements(session, checkpoint_id)
            return True
        if checkpoint.state not in {"staging", "replicating"}:
            raise TrainingConflict("Checkpoint cannot be mirrored")
        deadline = checkpoint.created_at + timedelta(seconds=60)
        if datetime.now(UTC) >= deadline:
            job.state, job.safe_reason = "recovering", "replication_failed"
            await enqueue_controls(session, checkpoint.attempt_id, "stop")
            return False
        manifest = TrainingArtifactManifest.model_validate(checkpoint.manifest)
        if manifest.world_size == 2:
            from coire_scheduler.training_components import validate_rank_bundle

            if not await validate_rank_bundle(session, manifest):
                return False
        source = await session.scalar(
            select(NodeRow)
            .join(TrainingParticipantRow, TrainingParticipantRow.node_id == NodeRow.id)
            .where(
                TrainingParticipantRow.attempt_id == checkpoint.attempt_id,
                TrainingParticipantRow.rank == 0,
            )
        )
        if source is None:
            raise TrainingConflict("Checkpoint source participant is unavailable")
        source_name = source.name
        if source_name not in {"coire-edge-a", "coire-edge-b"}:
            raise TrainingConflict("Checkpoint source is not a declared Studio")
        destination = "coire-edge-b" if source_name == "coire-edge-a" else "coire-edge-a"
        import_id = uuid.uuid5(checkpoint.id, "mirror:" + destination)
        prior = await session.get(TrainingCommandRow, import_id)
        if prior is not None:
            TrainingArtifactImportIntent.model_validate(prior.payload)
        checkpoint.state = "replicating"
        actor = job.owner_user_id
    proof = await client.verify_training_artifact(
        source_name,
        checkpoint_id,
        TrainingArtifactVerifyRequest(
            command_id=uuid.uuid5(checkpoint_id, "source-verify"),
            manifest_sha256=manifest.canonical_sha256(),
        ),
    )
    async with session_scope() as session:
        await record_verified_copy(session, manifest, proof, checkpoint_id=checkpoint_id)
    try:
        status = await client.training_artifact_import_status(destination, import_id)
    except NodeError as error:
        if error.kind is not NodeErrorKind.NOT_FOUND:
            raise
        status = None
    if status is not None and (
        status.artifact_id != checkpoint_id or status.manifest_sha256 != manifest.canonical_sha256()
    ):
        raise TrainingConflict("Peer import differs from immutable checkpoint")
    if status is None or status.state not in {"verified", "cancelled"}:
        grant = await client.grant_training_artifact(
            TrainingArtifactGrantRequest.model_validate(
                {
                    "command_id": uuid.uuid4(),
                    "artifact_id": checkpoint_id,
                    "manifest_sha256": manifest.canonical_sha256(),
                    "source_node": source_name,
                    "destination_node": destination,
                    "attempt_id": manifest.attempt_id,
                    "fence": manifest.fence,
                    "file_ids": [file.id for file in manifest.files],
                    "max_bytes": manifest.total_bytes,
                    "expires_at": min(deadline, datetime.now(UTC) + timedelta(seconds=60)),
                }
            )
        )
        if status is None:
            intent = TrainingArtifactImportIntent.model_validate(
                {
                    "command_id": import_id,
                    "artifact_id": checkpoint_id,
                    "manifest_sha256": manifest.canonical_sha256(),
                    "source_node": source_name,
                    "destination_node": destination,
                    "attempt_id": manifest.attempt_id,
                    "fence": manifest.fence,
                    "grant_id": grant.grant_id,
                }
            )
            async with session_scope() as session:
                job = await current_job(session, manifest.job_id or "", lock=True)
                await current_attempt(session, job, manifest.attempt_id or "", manifest.fence or 0)
                row = await session.get(TrainingCommandRow, import_id)
                if row is None:
                    session.add(
                        TrainingCommandRow(
                            id=import_id,
                            actor_user_id=actor,
                            idempotency_key=f"checkpoint-mirror:{checkpoint_id}:{destination}",
                            operation="node.artifact.import",
                            subject_id=destination,
                            job_id=job.id,
                            attempt_id=manifest.attempt_id,
                            request_sha256=payload_digest(intent),
                            payload=intent.model_dump(mode="json"),
                            state="dispatching",
                        )
                    )
            status = await client.import_training_artifact(
                TrainingArtifactImportRequest.model_validate(
                    {**intent.model_dump(mode="json"), "grant_secret": grant.secret}
                )
            )
        else:
            status = await client.refresh_training_artifact(
                destination,
                import_id,
                TrainingArtifactGrantRefresh.model_validate(
                    {
                        "command_id": uuid.uuid4(),
                        "artifact_id": checkpoint_id,
                        "manifest_sha256": manifest.canonical_sha256(),
                        "attempt_id": manifest.attempt_id,
                        "fence": manifest.fence,
                        "grant_id": grant.grant_id,
                        "grant_secret": grant.secret,
                    }
                ),
            )
    if status is None or status.state != "verified":
        return False
    if (
        status.verified_manifest is None
        or status.verified_manifest.canonical_sha256() != manifest.canonical_sha256()
    ):
        raise TrainingConflict("Peer verified a different complete checkpoint manifest")
    peer_proof = await client.verify_training_artifact(
        destination,
        checkpoint_id,
        TrainingArtifactVerifyRequest(
            command_id=uuid.uuid5(checkpoint_id, "peer-verify"),
            manifest_sha256=manifest.canonical_sha256(),
        ),
    )
    async with session_scope() as session:
        await record_verified_copy(session, manifest, peer_proof, checkpoint_id=checkpoint_id)
        await enqueue_checkpoint_acknowledgements(session, checkpoint_id)
        row = await session.get(TrainingCommandRow, import_id, with_for_update=True)
        if row is not None:
            row.receipt, row.state = status.model_dump(mode="json"), "succeeded"
    return True
