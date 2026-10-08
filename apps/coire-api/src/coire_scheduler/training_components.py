"""Durable two-rank collection; core journals descriptors, never tensor bytes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    NodeRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingCommandRow,
    TrainingJobRow,
    TrainingParticipantRow,
    session_scope,
)
from coire_api.nodes_client import NodeError, NodeErrorKind
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.events import current_attempt, current_job
from coire_api.training.service import payload_digest
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingConflict, TrainingForbidden
from coire_core.models.training import parse_resolved_training_spec
from coire_core.models.training_node import (
    NodeTrainingEvent,
    TrainingArtifactGrantRefresh,
    TrainingArtifactManifest,
    TrainingCollectiveBinding,
    TrainingPrepareRequest,
    TrainingRankCollection,
    TrainingRankComponentManifest,
    TrainingRankGrantRequest,
    TrainingRankImportRequest,
    TrainingRankImportStatus,
    TrainingRankVerificationReceipt,
)
from coire_core.settings import Settings

if TYPE_CHECKING:
    from coire_api.training_executor import TrainingNodeClient

logger = logging.getLogger(__name__)
COMPONENT = "node.training.component"
IMPORT = "node.training.component-import"
VERIFY = "node.training.component-verify"
COLLECTION = "node.training.rank-collection"


def component_id(component: TrainingRankComponentManifest) -> uuid.UUID:
    return uuid.uuid5(component.artifact_id, f"component:{component.rank}")


async def component_authority(
    session: AsyncSession, component: TrainingRankComponentManifest
) -> TrainingJobRow:
    job = await current_job(session, component.job_id)
    await authorize_live_training_action(
        session, Principal.model_validate(job.authorization_snapshot)
    )
    job = await current_job(session, component.job_id, lock=True)
    attempt = await current_attempt(session, job, component.attempt_id, component.fence)
    resolved = parse_resolved_training_spec(job.resolved_spec)
    if (
        attempt.world_size != 2
        or component.world_size != 2
        or component.runtime_sha256 != attempt.runtime_sha256
        or component.runtime_sha256 != resolved.runtime_sha256
        or component.resolved_spec_sha256 != job.resolved_sha256
        or payload_digest(resolved) != job.resolved_sha256
        or component.update > resolved.spec.optim.updates
        or component.update < job.completed_update
    ):
        raise TrainingConflict("Rank component differs from current evaluated execution")
    participants = list(
        await session.scalars(
            select(TrainingParticipantRow)
            .join(NodeRow, NodeRow.id == TrainingParticipantRow.node_id)
            .where(TrainingParticipantRow.attempt_id == attempt.id)
            .order_by(TrainingParticipantRow.rank)
        )
    )
    if len(participants) != 2 or {p.rank for p in participants} != {0, 1}:
        raise TrainingConflict("Rank component participant barrier is incomplete")
    for p in participants:
        node = await session.get(NodeRow, p.node_id)
        if (
            node is None
            or node.name != ("coire-edge-a" if p.rank == 0 else "coire-edge-b")
            or p.stopped_at is not None
        ):
            raise TrainingConflict("Rank component participant identity or liveness changed")
    return job


@observed("coire.scheduler.training.component.stage")
async def record_rank_component(
    session: AsyncSession, node_name: str, event: NodeTrainingEvent
) -> None:
    if event.payload.kind != "checkpoint_rank_staged":
        raise TrainingConflict("Expected an authenticated rank-component event")
    component = event.payload.component
    await component_authority(session, component)
    if (
        component.job_id != event.job_id
        or component.attempt_id != event.attempt_id
        or component.fence != event.fence
        or component.update != event.update
        or node_name != ("coire-edge-a" if component.rank == 0 else "coire-edge-b")
    ):
        raise TrainingConflict("Authenticated rank event scope differs")
    row = await session.get(TrainingCommandRow, component_id(component))
    if row is not None:
        if (
            row.payload != component.model_dump(mode="json")
            or row.subject_id != node_name
            or row.request_sha256 != component.canonical_sha256()
        ):
            raise TrainingConflict("Rank component is immutable")
        return
    prior_update = await session.scalar(
        select(func.max(TrainingCommandRow.payload["update"].as_integer())).where(
            TrainingCommandRow.operation == COMPONENT,
            TrainingCommandRow.attempt_id == component.attempt_id,
            TrainingCommandRow.subject_id == node_name,
        )
    )
    if prior_update is not None and component.update <= prior_update:
        raise TrainingConflict("Rank checkpoint update must advance monotonically")
    other = await session.scalar(
        select(TrainingCommandRow).where(
            TrainingCommandRow.operation == COMPONENT,
            TrainingCommandRow.attempt_id == component.attempt_id,
            TrainingCommandRow.payload["update"].as_integer() == component.update,
        )
    )
    if other is not None:
        peer = TrainingRankComponentManifest.model_validate(other.payload)
        if peer.artifact_id != component.artifact_id or peer.rank == component.rank:
            raise TrainingConflict("Ranks disagree on common checkpoint identity")
    job = await current_job(session, event.job_id, lock=True)
    session.add(
        TrainingCommandRow(
            id=component_id(component),
            actor_user_id=job.owner_user_id,
            idempotency_key=f"rank-component:{component.artifact_id}:{component.rank}",
            operation=COMPONENT,
            subject_id=node_name,
            job_id=job.id,
            attempt_id=component.attempt_id,
            request_sha256=component.canonical_sha256(),
            payload=component.model_dump(mode="json"),
            state="succeeded",
        )
    )
    await session.flush()


def _read_hostfile(path: str) -> bytes:
    # This is the scheduler's deployment-mounted generated file, not a remote
    # Studio path read, guessed topology, or a hostfile generation operation.
    with Path(path).open("rb") as stream:
        payload = stream.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise TrainingConflict("Generated JACCL hostfile exceeds its bound")
    return payload


async def bind_collective_prepares(
    session: AsyncSession, attempt_id: str, settings: Settings
) -> None:
    """Freeze both not-yet-dispatched prepare commands under one job lock."""
    attempt = await session.get(TrainingAttemptRow, attempt_id)
    if attempt is None or attempt.world_size != 2:
        return
    job = await current_job(session, attempt.job_id)
    await authorize_live_training_action(
        session, Principal.model_validate(job.authorization_snapshot)
    )
    job = await current_job(session, attempt.job_id, lock=True)
    if (
        not settings.training_enabled
        or job.state != "reserving"
        or job.fence != attempt.fence
        or attempt.state != "preparing"
        or attempt.lease_expires_at <= datetime.now(UTC)
    ):
        raise TrainingConflict("Collective preparation has no execution authority")
    from coire_api.sharding import link_projection
    from coire_scheduler.sharding import validate_hostfile

    if not (await link_projection(session, settings)).tp_eligible:
        raise TrainingConflict("Current measured JACCL link evidence is unavailable")
    identity = uuid.uuid5(uuid.NAMESPACE_URL, f"coire:training-collective:{attempt_id}")
    frozen = await session.get(TrainingCommandRow, identity)
    if frozen is None:
        nodes = list(
            await session.scalars(
                select(NodeRow).where(NodeRow.name.in_(["coire-edge-a", "coire-edge-b"]))
            )
        )
        try:
            payload = await anyio.to_thread.run_sync(
                _read_hostfile, settings.sharding_jaccl_hostfile
            )
            digest = validate_hostfile(payload, {node.name: node.data_host or "" for node in nodes})
            if json.loads(payload).get("backend") != "jaccl":
                raise ValueError("JACCL required")
        except (OSError, ValueError):
            raise TrainingConflict("Declared generated JACCL hostfile is unavailable") from None
        # Matches the native training launcher's existing declared coordinator.
        binding = TrainingCollectiveBinding(
            hostfile_sha256=digest, coordinator_port=32323, runtime_sha256=attempt.runtime_sha256
        )
        session.add(
            TrainingCommandRow(
                id=identity,
                actor_user_id=job.owner_user_id,
                idempotency_key=f"training-collective:{attempt_id}",
                operation="training.collective",
                subject_id=attempt_id,
                job_id=job.id,
                attempt_id=attempt_id,
                request_sha256=payload_digest(binding),
                payload=binding.model_dump(mode="json"),
                state="succeeded",
            )
        )
    else:
        binding = TrainingCollectiveBinding.model_validate(frozen.payload)
        if (
            frozen.request_sha256 != payload_digest(binding)
            or binding.runtime_sha256 != attempt.runtime_sha256
        ):
            raise TrainingConflict("Frozen collective identity changed")
    commands = list(
        await session.scalars(
            select(TrainingCommandRow)
            .where(
                TrainingCommandRow.attempt_id == attempt_id,
                TrainingCommandRow.operation == "node.training.prepare",
            )
            .with_for_update()
        )
    )
    if len(commands) != 2:
        raise TrainingConflict("Both prepare intents must exist before collective dispatch")
    for command in commands:
        prepared = TrainingPrepareRequest.model_validate(command.payload)
        if prepared.collective is not None:
            if prepared.collective != binding:
                raise TrainingConflict("Prepare collective differs from pinned identity")
        else:
            if command.state != "pending" or command.receipt is not None:
                raise TrainingConflict("An uncertain preparation cannot change its collective")
            prepared = prepared.model_copy(update={"collective": binding})
            command.payload, command.request_sha256 = (
                prepared.model_dump(mode="json"),
                payload_digest(prepared),
            )
    await session.flush()


async def validate_rank_bundle(session: AsyncSession, manifest: TrainingArtifactManifest) -> bool:
    """Both full-bundle events must agree with both accepted immutable components."""
    if manifest.world_size != 2:
        return True
    rows = list(
        await session.scalars(
            select(TrainingCommandRow).where(
                TrainingCommandRow.operation == COMPONENT,
                TrainingCommandRow.attempt_id == manifest.attempt_id,
                TrainingCommandRow.payload["artifact_id"].as_string() == str(manifest.artifact_id),
            )
        )
    )
    components = [TrainingRankComponentManifest.model_validate(row.payload) for row in rows]
    if len(components) != 2 or {c.rank for c in components} != {0, 1}:
        raise TrainingConflict("Complete bundle lacks both accepted rank components")
    for component in components:
        if any(
            getattr(component, k) != getattr(manifest, k)
            for k in (
                "job_id",
                "attempt_id",
                "fence",
                "update",
                "world_size",
                "runtime_sha256",
                "resolved_spec_sha256",
            )
        ):
            raise TrainingConflict("Complete bundle lineage differs from accepted components")
    if (
        sorted(manifest.ranks, key=lambda r: r.rank)
        != [c.state for c in sorted(components, key=lambda c: c.rank)]
        or sorted(manifest.files, key=lambda f: f.id)
        != sorted([f for c in components for f in c.files], key=lambda f: f.id)
        or manifest.total_bytes != sum(c.total_bytes for c in components)
    ):
        raise TrainingConflict(
            "Complete bundle files or rank states differ from accepted components"
        )
    events = list(
        await session.scalars(
            select(TrainingCommandRow).where(
                TrainingCommandRow.operation == "node.training.event",
                TrainingCommandRow.attempt_id == manifest.attempt_id,
                TrainingCommandRow.payload["payload"]["kind"].as_string() == "checkpoint_staged",
                TrainingCommandRow.payload["payload"]["manifest"]["artifact_id"].as_string()
                == str(manifest.artifact_id),
            )
        )
    )
    nodes = set()
    for row in events:
        event = NodeTrainingEvent.model_validate(row.payload)
        assert event.payload.kind == "checkpoint_staged"
        if event.payload.manifest.canonical_sha256() != manifest.canonical_sha256():
            raise TrainingConflict("Ranks emitted different complete bundles")
        nodes.add(row.subject_id)
    return nodes == {"coire-edge-a", "coire-edge-b"}


class TrainingComponentCoordinator:
    """Idempotent metadata/transfer/collection ticks with a separate cancellation lane."""

    def __init__(self, settings: Settings, client: TrainingNodeClient) -> None:
        self.settings, self.client = settings, client
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="training-rank-components")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        from coire_api.polling import wait_or_stop

        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.warning(
                    "rank collection scan deferred", extra={"operation": "component.scan"}
                )
            await wait_or_stop(self._stop, 1.0)

    @observed("coire.scheduler.training.component.scan")
    async def run_once(self) -> None:
        async with session_scope() as session:
            rows = list(
                await session.scalars(
                    select(TrainingCommandRow)
                    .join(
                        TrainingAttemptRow, TrainingAttemptRow.id == TrainingCommandRow.attempt_id
                    )
                    .outerjoin(
                        TrainingCheckpointRow,
                        cast(TrainingCheckpointRow.id, String)
                        == TrainingCommandRow.payload["artifact_id"].as_string(),
                    )
                    .where(
                        TrainingCommandRow.operation == COMPONENT,
                        TrainingAttemptRow.state.in_(["running", "stopping", "unknown"]),
                        (TrainingCheckpointRow.id.is_(None))
                        | (TrainingCheckpointRow.state != "committed"),
                    )
                    .order_by(TrainingCommandRow.created_at)
                    .limit(32)
                )
            )
        groups = {(row.attempt_id or "", str(row.payload["artifact_id"])) for row in rows}
        async with session_scope() as session:
            teardown = list(
                await session.scalars(
                    select(TrainingCommandRow.attempt_id)
                    .join(
                        TrainingAttemptRow, TrainingAttemptRow.id == TrainingCommandRow.attempt_id
                    )
                    .join(TrainingJobRow, TrainingJobRow.id == TrainingAttemptRow.job_id)
                    .where(
                        TrainingCommandRow.operation == IMPORT,
                        TrainingCommandRow.state == "dispatching",
                        (
                            TrainingJobRow.state.in_(
                                ["cancelling", "recovering", "cancelled", "failed"]
                            )
                        )
                        | (TrainingAttemptRow.fence != TrainingJobRow.fence)
                        | (TrainingAttemptRow.state == "stopped"),
                    )
                    .distinct()
                )
            )
        await asyncio.gather(
            *(self.cancel_imports(attempt_id) for attempt_id in teardown if attempt_id is not None)
        )
        await asyncio.gather(
            *(self._isolated_tick(attempt, uuid.UUID(artifact)) for attempt, artifact in groups)
        )

    async def _isolated_tick(self, attempt_id: str, artifact_id: uuid.UUID) -> None:
        try:
            await self.tick(attempt_id, artifact_id)
        except (TrainingConflict, TrainingForbidden):
            await self.fail_attempt(attempt_id)
            await self.cancel_imports(attempt_id)
        except Exception:
            logger.info(
                "rank collection awaiting reconciliation",
                extra={"attempt_id": attempt_id, "artifact_id": str(artifact_id)},
            )

    async def cancel_imports(self, attempt_id: str) -> None:
        async with session_scope() as session:
            rows = list(
                await session.scalars(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.attempt_id == attempt_id,
                        TrainingCommandRow.operation == IMPORT,
                        TrainingCommandRow.state == "dispatching",
                    )
                )
            )

        async def cancel(row: TrainingCommandRow) -> None:
            try:
                status = await self.client.cancel_rank_import(row.subject_id, row.id)
                component = TrainingRankComponentManifest.model_validate(row.payload["component"])
                if status.component != component or status.import_id != row.id:
                    raise TrainingConflict("Cancelled import identity changed")
                if status.state not in {"cancelled", "verified"}:
                    return
                async with session_scope() as session:
                    current = await session.get(TrainingCommandRow, row.id, with_for_update=True)
                    if current is not None and current.state == "dispatching":
                        current.state, current.receipt = "cancelled", status.model_dump(mode="json")
            except Exception:
                logger.info(
                    "rank import teardown awaiting proof",
                    extra={"attempt_id": attempt_id, "command_id": str(row.id)},
                )

        await asyncio.gather(*(cancel(row) for row in rows))

    async def fail_attempt(self, attempt_id: str) -> None:
        from coire_scheduler.training import enqueue_controls

        async with session_scope() as session:
            attempt = await session.get(TrainingAttemptRow, attempt_id)
            if attempt is None:
                return
            job = await current_job(session, attempt.job_id, lock=True)
            if job.fence != attempt.fence or attempt.state == "stopped":
                return
            if job.state not in {
                "cancelling",
                "pausing",
                "recovering",
                "cancelled",
                "failed",
                "succeeded",
            }:
                job.state, job.safe_reason = "recovering", "rank_failed"
                job.version += 1
                job.updated_at = datetime.now(UTC)
                from coire_api.training.events import append_event
                from coire_core.models.training import TrainingStateEvent

                await append_event(
                    session,
                    job.id,
                    TrainingStateEvent.model_validate(
                        {"kind": "state", "state": "recovering", "reason": "rank_failed"}
                    ),
                )
            await enqueue_controls(session, attempt_id, "stop")

    async def _authority(self, component: TrainingRankComponentManifest) -> None:
        async with session_scope() as session:
            await component_authority(session, component)
            if not self.settings.training_enabled:
                raise TrainingConflict("Training is disabled")
            staged = await session.get(TrainingCommandRow, component_id(component))
            if staged is None or staged.payload != component.model_dump(mode="json"):
                raise TrainingConflict("Accepted component identity changed")

    async def _intent(
        self,
        component: TrainingRankComponentManifest,
        identity: uuid.UUID,
        operation: str,
        node: str,
        payload: dict[str, object],
    ) -> TrainingCommandRow:
        async with session_scope() as session:
            job = await component_authority(session, component)
            row = await session.get(TrainingCommandRow, identity, with_for_update=True)
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
            if row is None:
                row = TrainingCommandRow(
                    id=identity,
                    actor_user_id=job.owner_user_id,
                    idempotency_key=f"{operation}:{identity}",
                    operation=operation,
                    subject_id=node,
                    job_id=job.id,
                    attempt_id=component.attempt_id,
                    request_sha256=digest,
                    payload=payload,
                    state="dispatching",
                )
                session.add(row)
                await session.flush()
            elif row.payload != payload or row.request_sha256 != digest or row.subject_id != node:
                raise TrainingConflict("Rank command intent changed")
            return row

    async def _receipt(
        self,
        component: TrainingRankComponentManifest,
        identity: uuid.UUID,
        payload: dict[str, object],
    ) -> None:
        async with session_scope() as session:
            await component_authority(session, component)
            row = await session.get(TrainingCommandRow, identity, with_for_update=True)
            if row is None:
                raise TrainingConflict("Rank receipt lacks persisted intent")
            if row.receipt is not None and row.receipt != payload:
                raise TrainingConflict("Rank receipt changed")
            row.receipt, row.state = payload, "succeeded"

    async def _verify(self, node: str, component: TrainingRankComponentManifest) -> None:
        identity = uuid.uuid5(component_id(component), "verify:" + node)
        row = await self._intent(
            component, identity, VERIFY, node, component.model_dump(mode="json")
        )
        if row.state == "succeeded":
            proof = TrainingRankVerificationReceipt.model_validate(row.receipt)
            if proof.node != node or proof.component != component or proof.command_id != identity:
                raise TrainingConflict("Persisted rank verification differs")
            return
        await self._authority(component)
        proof = await self.client.verify_rank_component(node, component, identity)
        if proof.component != component or proof.node != node or proof.command_id != identity:
            raise TrainingConflict("Authenticated rank verification differs")
        await self._receipt(component, identity, proof.model_dump(mode="json"))

    async def _transfer(self, component: TrainingRankComponentManifest) -> bool:
        source = "coire-edge-a" if component.rank == 0 else "coire-edge-b"
        destination = "coire-edge-b" if component.rank == 0 else "coire-edge-a"
        await self._authority(component)
        actual = await self.client.rank_component(source, component.artifact_id, component.rank)
        if actual != component:
            raise TrainingConflict("Authenticated local component metadata changed")
        await self._verify(source, component)
        identity = uuid.uuid5(component_id(component), "import:" + destination)
        payload: dict[str, object] = {
            "component": component.model_dump(mode="json"),
            "source_node": source,
            "destination_node": destination,
        }
        row = await self._intent(component, identity, IMPORT, destination, payload)
        if row.state == "succeeded":
            imported = TrainingRankImportStatus.model_validate(row.receipt)
            if (
                imported.import_id != identity
                or imported.component != component
                or imported.state != "verified"
                or imported.receipt is None
                or imported.receipt.node != destination
                or imported.receipt.command_id != identity
            ):
                raise TrainingConflict("Persisted peer import verification differs")
            await self._verify(destination, component)
            return True
        await self._authority(component)
        try:
            status = await self.client.rank_import_status(destination, identity)
        except NodeError as error:
            if error.kind is not NodeErrorKind.NOT_FOUND:
                raise
            status = None
        if status is not None:
            if status.component != component or status.import_id != identity:
                raise TrainingConflict("Rank import status changed component scope")
            if status.state == "cancelled":
                raise TrainingConflict("Rank import was cancelled")
            if status.state in {"staging", "transferring"}:
                return False  # Do not refresh an actively owned stream.
        if status is None or status.state == "failed":
            await self._authority(component)
            grant = await self.client.grant_rank_component(
                TrainingRankGrantRequest.model_validate(
                    {
                        "command_id": uuid.uuid4(),
                        "artifact_id": component.artifact_id,
                        "manifest_sha256": component.canonical_sha256(),
                        "source_node": source,
                        "destination_node": destination,
                        "attempt_id": component.attempt_id,
                        "fence": component.fence,
                        "file_ids": [f.id for f in component.files],
                        "max_bytes": component.total_bytes,
                        "expires_at": datetime.now(UTC)
                        + timedelta(seconds=min(60, self.settings.training_transfer_grant_s)),
                        "component": component,
                    }
                )
            )
            await self._authority(component)
            common = {
                "artifact_id": component.artifact_id,
                "manifest_sha256": component.canonical_sha256(),
                "attempt_id": component.attempt_id,
                "fence": component.fence,
                "grant_id": grant.grant_id,
                "grant_secret": grant.secret,
            }
            if status is None:
                status = await self.client.import_rank_component(
                    TrainingRankImportRequest.model_validate(
                        {
                            **common,
                            "command_id": identity,
                            "source_node": source,
                            "destination_node": destination,
                            "component": component,
                        }
                    )
                )
            else:
                status = await self.client.refresh_rank_import(
                    destination,
                    identity,
                    TrainingArtifactGrantRefresh.model_validate(
                        {**common, "command_id": uuid.uuid4()}
                    ),
                )
        if status.component != component or status.import_id != identity:
            raise TrainingConflict("Rank import response differs from current scope")
        if status.state != "verified":
            return False
        if (
            status.receipt is None
            or status.receipt.node != destination
            or status.receipt.component != component
            or status.receipt.command_id != identity
        ):
            raise TrainingConflict("Peer rank import has no exact authenticated receipt")
        await self._verify(destination, component)
        await self._receipt(component, identity, status.model_dump(mode="json"))
        return True

    @observed("coire.scheduler.training.component.tick")
    async def tick(self, attempt_id: str, artifact_id: uuid.UUID) -> None:
        async with session_scope() as session:
            rows = list(
                await session.scalars(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.operation == COMPONENT,
                        TrainingCommandRow.attempt_id == attempt_id,
                        TrainingCommandRow.payload["artifact_id"].as_string() == str(artifact_id),
                    )
                )
            )
            if not rows:
                return
            checkpoint = await session.get(TrainingCheckpointRow, artifact_id)
            if checkpoint is not None and checkpoint.state == "committed":
                return
            if checkpoint is not None:
                await validate_rank_bundle(
                    session, TrainingArtifactManifest.model_validate(checkpoint.manifest)
                )
            if min(row.created_at for row in rows) + timedelta(seconds=60) <= datetime.now(UTC):
                raise TrainingConflict("Rank collection deadline expired")
            components = sorted(
                [TrainingRankComponentManifest.model_validate(row.payload) for row in rows],
                key=lambda c: c.rank,
            )
            if len(components) != 2:
                return
            await component_authority(session, components[0])
            await component_authority(session, components[1])
        if not all(await asyncio.gather(*(self._transfer(c) for c in components))):
            return
        for rank, node in enumerate(("coire-edge-a", "coire-edge-b")):
            component = components[rank]
            async with session_scope() as session:
                await component_authority(session, component)
                participant = await session.scalar(
                    select(TrainingParticipantRow).where(
                        TrainingParticipantRow.attempt_id == attempt_id,
                        TrainingParticipantRow.rank == rank,
                    )
                )
                attempt = await session.get(TrainingAttemptRow, attempt_id)
                assert participant is not None and attempt is not None
                identity = uuid.uuid5(artifact_id, "collection:" + node)
                prior = await session.get(TrainingCommandRow, identity)
                collection = (
                    TrainingRankCollection.model_validate(prior.payload)
                    if prior
                    else TrainingRankCollection.model_validate(
                        {
                            "command_id": identity,
                            "job_id": component.job_id,
                            "attempt_id": attempt_id,
                            "fence": component.fence,
                            "request_sha256": participant.request_sha256,
                            "node": node,
                            "rank": rank,
                            "world_size": 2,
                            "lease_expires_at": attempt.lease_expires_at,
                            "artifact_id": artifact_id,
                            "components": components,
                        }
                    )
                )
                if (
                    collection.components != components
                    or collection.artifact_id != artifact_id
                    or collection.command_id != identity
                    or collection.node != node
                    or collection.rank != rank
                    or collection.job_id != component.job_id
                    or collection.attempt_id != attempt_id
                    or collection.fence != component.fence
                    or collection.request_sha256 != participant.request_sha256
                ):
                    raise TrainingConflict(
                        "Persisted collection differs from accepted participants and components"
                    )
            row = await self._intent(
                component, identity, COLLECTION, node, collection.model_dump(mode="json")
            )
            if row.state == "succeeded":
                continue
            await self._authority(component)
            status = await self.client.collect_training_ranks(collection)
            if status.liveness != "running" or status.update != component.update:
                raise TrainingConflict(
                    "Rank collection was not accepted by the owned evaluated worker"
                )
            await self._receipt(component, identity, status.model_dump(mode="json"))
