"""Durable checkpoint extraction and erasure over authenticated Studio commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime

from dbos import DBOS
from sqlalchemy import select

from coire_api.auth import Principal
from coire_api.db import (
    EngineProcessRow,
    InstanceMemberRow,
    ModelInstanceRow,
    NodeRow,
    TrainingAdapterRow,
    TrainingArtifactCopyRow,
    TrainingCheckpointRow,
    TrainingCommandRow,
    TrainingEvaluationTriggerRow,
    TrainingJobRow,
    TrainingStorageReservationRow,
    session_scope,
)
from coire_api.evaluation.checkpoints import finish_checkpoint_cleanup
from coire_api.evaluation.training import reconcile_trigger, require_checkpoint_pause
from coire_api.instance.service import transition
from coire_api.nodes_client import NodeError, NodeErrorKind
from coire_api.training.adapters import finalize_serving_adapter, stage_serving_adapter
from coire_api.training.runtime import TrainingRuntime
from coire_api.training.service import payload_digest
from coire_api.training_executor import TrainingNodeClient
from coire_core.errors import CoireError, TrainingConflict
from coire_core.models.engine import EngineState
from coire_core.models.instance import InstanceState
from coire_core.models.training_node import (
    TrainingAdapterExtractRequest,
    TrainingArtifactDeleteRequest,
    TrainingArtifactManifest,
)
from coire_core.settings import Settings, get_settings


async def prepare(identity: uuid.UUID, runtime: TrainingRuntime, settings: Settings) -> None:
    adapter_id = uuid.uuid5(identity, "checkpoint-adapter")
    command_id = uuid.uuid5(identity, "checkpoint-extraction")
    async with session_scope() as session:
        trigger = await session.get(TrainingEvaluationTriggerRow, identity)
        if trigger is None or trigger.checkpoint_id is None:
            return
        job = await session.get(TrainingJobRow, trigger.job_id, with_for_update=True)
        assert job is not None
        await require_checkpoint_pause(session, job, identity)
        if trigger.deadline_at <= datetime.now(UTC) or not settings.evaluations_enabled:
            return
        adapter = await session.get(TrainingAdapterRow, adapter_id)
        checkpoint_id = trigger.checkpoint_id
        if adapter is None and await session.get(TrainingCommandRow, command_id) is None:
            payload = {"checkpoint_id": str(checkpoint_id), "adapter_id": str(adapter_id)}
            session.add(
                TrainingCommandRow(
                    id=command_id,
                    actor_user_id=job.owner_user_id,
                    idempotency_key=f"evaluation-extraction:{identity}",
                    operation="training.evaluation.extract",
                    subject_id=str(adapter_id),
                    job_id=job.id,
                    request_sha256=hashlib.sha256(
                        json.dumps(payload, sort_keys=True).encode()
                    ).hexdigest(),
                    payload=payload,
                    state="dispatching",
                )
            )
    if adapter is None:
        manifest = await runtime.extract_final_adapter(
            checkpoint_id, adapter_id, command_id, evaluation_trigger_id=identity
        )
        if manifest is None:
            return
        async with session_scope() as session:
            trigger = await session.get(TrainingEvaluationTriggerRow, identity)
            assert trigger is not None
            job = await session.get(TrainingJobRow, trigger.job_id)
            assert job is not None
            await stage_serving_adapter(
                session,
                Principal.model_validate(job.authorization_snapshot),
                checkpoint_id,
                manifest,
                slug=f"eval-{identity.hex}",
                evaluation_trigger_id=identity,
            )
            command = await session.get(TrainingCommandRow, command_id, with_for_update=True)
            assert command is not None
            command.state, command.receipt = (
                "succeeded",
                {"manifest": manifest.model_dump(mode="json")},
            )
    if adapter is None or adapter.state != "ready":
        smoke = await runtime.prepare_adapter(adapter_id)
    else:
        smoke = None
    if smoke is not None:
        async with session_scope() as session:
            await finalize_serving_adapter(session, adapter_id, smoke[1], instance_id=smoke[0])
    async with session_scope() as session:
        adapter = await session.get(TrainingAdapterRow, adapter_id)
        instance = await session.get(ModelInstanceRow, uuid.uuid5(adapter_id, "validation-smoke"))
        if (
            adapter is not None
            and adapter.state == "ready"
            and instance is not None
            and instance.state is InstanceState.READY
        ):
            await transition(
                session, instance.id, InstanceState.DRAINING, reason="checkpoint smoke complete"
            )


async def erase(identity: uuid.UUID, client: TrainingNodeClient) -> bool:
    """Stop owned engines/imports, then prove erasure on both Studios before releasing bytes."""
    adapter_id = uuid.uuid5(identity, "checkpoint-adapter")
    extraction_id = uuid.uuid5(identity, "checkpoint-extraction")
    async with session_scope() as session:
        trigger = await session.get(TrainingEvaluationTriggerRow, identity)
        if trigger is None or trigger.phase != "cleaning_adapter":
            return False
        job = await session.get(TrainingJobRow, trigger.job_id, with_for_update=True)
        assert job is not None
        checkpoint = await session.get(TrainingCheckpointRow, trigger.checkpoint_id)
        if checkpoint is None:
            return False
        checkpoint_manifest = TrainingArtifactManifest.model_validate(checkpoint.manifest)
        adapter = await session.get(TrainingAdapterRow, adapter_id)
        extraction = await session.get(TrainingCommandRow, extraction_id)
        native = (
            TrainingAdapterExtractRequest.model_validate(extraction.payload["node_command"])
            if (extraction is not None and extraction.payload.get("node_command") is not None)
            else None
        )
        instances = (
            await session.scalars(
                select(ModelInstanceRow).where(ModelInstanceRow.adapter_id == adapter_id)
            )
        ).all()
        for instance in instances:
            if instance.state is InstanceState.READY:
                await transition(
                    session,
                    instance.id,
                    InstanceState.DRAINING,
                    reason="evaluation checkpoint cleanup",
                )
            elif instance.state is InstanceState.REQUESTED:
                await transition(
                    session,
                    instance.id,
                    InstanceState.FAILED,
                    reason="evaluation checkpoint cancelled before launch",
                )
        if any(
            item.state not in {InstanceState.STOPPED, InstanceState.FAILED} for item in instances
        ):
            return False
        unsafe = await session.scalar(
            select(EngineProcessRow.id)
            .join(InstanceMemberRow, InstanceMemberRow.engine_id == EngineProcessRow.id)
            .join(ModelInstanceRow, ModelInstanceRow.id == InstanceMemberRow.instance_id)
            .where(
                ModelInstanceRow.adapter_id == adapter_id,
                EngineProcessRow.state != EngineState.STOPPED,
            )
            .limit(1)
        )
        if unsafe is not None:
            return False
        manifest = (
            TrainingArtifactManifest.model_validate(adapter.metadata_record["manifest"])
            if adapter
            else None
        )
        if extraction is not None and native is None:
            extraction.state = "failed"  # No native authority was ever persisted or dispatched.
    if manifest is None and native is not None:
        # Even expired/revoked ownership must reconcile a dispatched extraction receipt.
        # A missing/uncertain receipt retains storage and pins; it is never inferred dead.
        status = await client.adapter_extraction_status(native.node, extraction_id)
        if (
            status.command_id != extraction_id
            or status.adapter_id != adapter_id
            or status.checkpoint_id != native.checkpoint_id
        ):
            raise TrainingConflict("Checkpoint extraction cleanup receipt differs")
        if status.state not in {"succeeded", "failed"}:
            return False
        manifest = status.manifest
        async with session_scope() as session:
            command = await session.get(TrainingCommandRow, extraction_id, with_for_update=True)
            assert command is not None
            command.state, command.receipt = status.state, status.model_dump(mode="json")
    if manifest is not None:
        if (
            manifest.artifact_id != adapter_id
            or manifest.kind != "adapter"
            or manifest.job_id != checkpoint.job_id
            or manifest.attempt_id != checkpoint.attempt_id
            or manifest.fence != checkpoint.fence
            or manifest.update != checkpoint.completed_update
            or manifest.runtime_sha256 != checkpoint_manifest.runtime_sha256
            or manifest.resolved_spec_sha256 != checkpoint_manifest.resolved_spec_sha256
        ):
            raise TrainingConflict("Checkpoint cleanup artifact identity differs")
        for node in ("coire-edge-a", "coire-edge-b"):
            import_id = uuid.uuid5(adapter_id, "mirror:" + node)
            try:
                import_status = await client.training_artifact_import_status(node, import_id)
            except NodeError as error:
                if error.kind is not NodeErrorKind.NOT_FOUND:
                    raise
            else:
                if (
                    import_status.import_id != import_id
                    or import_status.artifact_id != adapter_id
                    or import_status.manifest_sha256 != manifest.canonical_sha256()
                ):
                    raise TrainingConflict("Checkpoint import identity differs before erasure")
                stopped = await client.cancel_training_artifact_import(node, import_id)
                if (
                    stopped.import_id != import_status.import_id
                    or stopped.artifact_id != import_status.artifact_id
                    or stopped.manifest_sha256 != import_status.manifest_sha256
                    or stopped.state not in {"verified", "cancelled"}
                ):
                    raise TrainingConflict("Checkpoint import stop is unproved")
            command_id = uuid.uuid5(identity, "checkpoint-adapter-delete:" + node)
            request = TrainingArtifactDeleteRequest(
                command_id=command_id,
                manifest_sha256=manifest.canonical_sha256(),
                expected_version=1,
                unreferenced=True,
                expected_manifest=manifest,
            )
            async with session_scope() as session:
                trigger = await session.get(TrainingEvaluationTriggerRow, identity)
                assert trigger is not None
                job = await session.get(TrainingJobRow, trigger.job_id, with_for_update=True)
                assert job is not None
                command = await session.get(TrainingCommandRow, command_id)
                if command is None:
                    session.add(
                        TrainingCommandRow(
                            id=command_id,
                            actor_user_id=job.owner_user_id,
                            job_id=job.id,
                            operation="evaluation.adapter.delete",
                            subject_id=node,
                            idempotency_key=f"evaluation-adapter-delete:{identity}:{node}",
                            request_sha256=payload_digest(request),
                            payload=request.model_dump(mode="json"),
                            state="dispatching",
                        )
                    )
                elif command.payload != request.model_dump(mode="json"):
                    raise TrainingConflict("Immutable checkpoint cleanup intent changed")
                elif command.state == "succeeded":
                    continue
            proof = await client.delete_training_artifact(node, adapter_id, request)
            if (
                proof.command_id != command_id
                or proof.artifact_id != adapter_id
                or not proof.purged
            ):
                raise TrainingConflict("Checkpoint adapter erasure is unproved")
            async with session_scope() as session:
                command = await session.get(TrainingCommandRow, command_id, with_for_update=True)
                assert command is not None
                command.state, command.receipt = "succeeded", proof.model_dump(mode="json")
                peer = await session.scalar(select(NodeRow).where(NodeRow.name == node))
                if peer is not None:
                    for copy in (
                        await session.scalars(
                            select(TrainingArtifactCopyRow).where(
                                TrainingArtifactCopyRow.artifact_id == adapter_id,
                                TrainingArtifactCopyRow.node_id == peer.id,
                            )
                        )
                    ).all():
                        copy.state, copy.cleanup_receipt = "purged", proof.model_dump(mode="json")
    async with session_scope() as session:
        adapter = await session.get(TrainingAdapterRow, adapter_id, with_for_update=True)
        if adapter is not None:
            adapter.state, adapter.verified, adapter.visibility = "retired", False, "admin_only"
        for hold in (
            await session.scalars(
                select(TrainingStorageReservationRow)
                .where(
                    TrainingStorageReservationRow.subject_id == str(adapter_id),
                    TrainingStorageReservationRow.state.in_(["held", "releasing"]),
                )
                .with_for_update()
            )
        ).all():
            hold.state = "released"
    return True


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1)
async def checkpoint_tick(identity: str) -> bool:
    key, settings = uuid.UUID(identity), get_settings()
    client = TrainingNodeClient(settings)
    async with client:
        runtime = TrainingRuntime(settings, client)
        async with session_scope() as session:
            trigger = await session.get(TrainingEvaluationTriggerRow, key)
            if trigger is None or trigger.phase == "complete":
                return True
            phase = trigger.phase
        if phase == "preparing_adapter":
            try:
                async with asyncio.timeout(5):
                    await prepare(key, runtime, settings)
            except (CoireError, TimeoutError):
                pass  # Fixed acceptance deadline bounds retry; no asserted failure evidence.
        async with session_scope() as session:
            trigger = await session.get(TrainingEvaluationTriggerRow, key)
            assert trigger is not None
            if trigger.phase not in {
                "pending_pause",
                "resume_pending",
            } or trigger.deadline_at <= datetime.now(UTC):
                await reconcile_trigger(session, key, settings=settings, boundary="checkpoint")
            phase = trigger.phase
        if phase == "cleaning_adapter":
            async with asyncio.timeout(5):
                if not await erase(key, client):
                    return False
            async with session_scope() as session:
                return await finish_checkpoint_cleanup(session, key, settings=settings)
        if phase == "resume_pending":
            async with session_scope() as session:
                return await finish_checkpoint_cleanup(session, key, settings=settings)
    return False


@DBOS.workflow(name="coire.evaluation.training.checkpoint", max_recovery_attempts=100)
async def checkpoint_workflow(identity: str) -> None:
    while not await checkpoint_tick(identity):
        await DBOS.sleep_async(1)
