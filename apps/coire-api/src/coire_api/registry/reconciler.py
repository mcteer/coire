"""The acquisition and engine reconciler.

ADR-0005: one linear job per acquisition, whose stage is a cursor. Every pass reads the
unfinished jobs and advances each by issuing the node verb for its current stage. Because
every node verb is idempotent on the job id, a control plane that restarts mid-acquisition
simply re-issues the current stage and re-attaches — there is no orphaned work and no second
download. Feature 002 replaces this driver with a DBOS workflow over the *same* node verbs.

Three passes run each tick:

  * **downloads** — advance acquisitions;
  * **engines** — mirror what nodes report, so a died engine stops being reported as ready;
  * **retirement** — drive unload-then-delete until every node confirms.

Nothing here raises out of the loop. A node being down is the normal case this exists to
survive, and a reconciler that dies on the first `NodeError` would take the platform's ability
to recover with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coire_api.audit import write_audit
from coire_api.db import (
    DownloadJobRow,
    EngineProcessRow,
    MemoryReservationRow,
    ModelCopyRow,
    ModelInstanceRow,
    ModelRow,
    NodeRow,
    create_engine,
)
from coire_api.instance import service as instance_service
from coire_api.nodes_client import NodeClient, NodeError, NodeErrorKind
from coire_api.registry import service
from coire_core.models.audit import AuditAction, AuditOutcome
from coire_core.models.engine import (
    LIVE_ENGINE_STATES,
    TERMINAL_ENGINE_STATES,
    EngineState,
    ReconcileExpectation,
    ReconcileRequest,
)
from coire_core.models.instance import InstanceState
from coire_core.models.jobs import ChecksumManifest, DownloadStage, JobStage, JobStatus
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.registry import CopyRole, ModelState
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.registry")

_meter = otel_metrics.get_meter("coire.registry")
_download_bytes = _meter.create_counter(
    "coire_download_bytes_total", unit="By", description="Bytes acquired, by stage."
)
_model_state = _meter.create_gauge(
    "coire_model_state", description="Models in each registry state."
)
_engine_state_gauge = _meter.create_gauge(
    "coire_engine_state", description="Engines in each state."
)

GRANT_TTL = timedelta(hours=24)
ACTOR = "reconciler"


class RegistryReconciler:
    """Drives acquisitions, mirrors engines, and completes retirements."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._node_reconcile: set[str] = set()
        self.node_statuses: dict[str, object] = {}
        """Last status per node, shared with the admin views so they need no extra round trip."""

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="registry-reconciler")

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def request_reconcile(self, node: str) -> None:
        """Ask for an engine reconcile against one node on the next pass.

        Called when a node registers or comes back from unreachable — the two moments its real
        process state may differ from what the registry believes (spec FR-015).
        """
        self._node_reconcile.add(node)
        self._wake.set()

    async def _run(self) -> None:
        engine = create_engine(self._settings)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            while not self._stopping.is_set():
                # Consume the wakeup that led to this pass before doing any I/O. A node may
                # register while the pass is running; clearing afterward would erase that new
                # request and defer engine reconciliation until an unrelated later wakeup.
                self._wake.clear()
                try:
                    await self._pass(maker)
                except Exception:
                    logger.exception("reconciler pass failed; retrying next interval")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self._settings.registry_reconcile_interval_s
                    )
        finally:
            await engine.dispose()

    async def _pass(self, maker: async_sessionmaker[AsyncSession]) -> None:
        async with maker() as session, NodeClient(self._settings) as client:
            await self._refresh_statuses(session, client)
            await self._advance_downloads(session, client)
            await self._sync_engines(session, client)
            await self._drive_retirement(session, client)
            await self._reconcile_nodes(session, client)
            await self._publish_metrics(session)
            await session.commit()

    # -- node status -------------------------------------------------------
    async def _refresh_statuses(self, session: AsyncSession, client: NodeClient) -> None:
        for row in (await session.execute(select(NodeRow))).scalars().all():
            try:
                self.node_statuses[row.name] = await client.health(row.name)
                # Reconcile every healthy node on this pass. Registration is intentionally
                # sent before the restarted listener binds, so its event-driven reconcile may
                # race readiness; periodic reconciliation guarantees eventual drift/orphan
                # detection without relying on another state transition.
                self._node_reconcile.add(row.name)
            except NodeError:
                self.node_statuses.pop(row.name, None)

    # -- downloads ---------------------------------------------------------
    async def _advance_downloads(self, session: AsyncSession, client: NodeClient) -> None:
        jobs = (
            (
                await session.execute(
                    select(DownloadJobRow).where(
                        DownloadJobRow.stage.notin_([DownloadStage.DONE, DownloadStage.FAILED])
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            model = await session.get(ModelRow, job.model_id)
            if model is None or model.state is ModelState.RETIRED:
                continue
            with tracer.start_as_current_span(f"registry.reconcile.{job.stage.value}"):
                try:
                    await self._advance_one(session, client, job, model)
                except NodeError as exc:
                    await self._handle_node_error(session, job, model, exc)
                except Exception as exc:
                    logger.exception("job %s failed unexpectedly", job.id)
                    await self._fail(session, job, model, f"{type(exc).__name__}: {exc}")

    async def _advance_one(
        self,
        session: AsyncSession,
        client: NodeClient,
        job: DownloadJobRow,
        model: ModelRow,
    ) -> None:
        origin, replica = await self._job_nodes(session, job)

        if job.stage is DownloadStage.PULL:
            status = await self._pull(client, job, model, origin.name)
            await self._mirror(job, status)
            if status.stage is JobStage.DONE:
                await self._record_copy(session, job, model, origin, status, role=CopyRole.ORIGIN)
                job.manifest = status.manifest.model_dump(mode="json") if status.manifest else None
                model.manifest_sha256 = status.manifest_sha256
                await self._set_stage(session, job, model, DownloadStage.VERIFY_ORIGIN)
            elif status.stage is JobStage.FAILED:
                await self._fail(session, job, model, status.error or "the pull failed", status)
            return

        if job.stage is DownloadStage.VERIFY_ORIGIN:
            # Pass-through: the pull already verified against Hugging Face's own digests.
            await self._set_stage(session, job, model, DownloadStage.EXPORT)
            return

        if job.stage is DownloadStage.EXPORT:
            if not job.transfer_grant:
                job.transfer_grant = secrets.token_urlsafe(32)
            await client.grant_export(
                origin.name, model.slug, job.transfer_grant, datetime.now(UTC) + GRANT_TTL
            )
            await self._set_stage(session, job, model, DownloadStage.IMPORT)
            return

        if job.stage is DownloadStage.IMPORT:
            if not job.manifest or not job.transfer_grant:
                await self._fail(session, job, model, "the origin manifest or grant is missing")
                return
            manifest = ChecksumManifest.model_validate(job.manifest)
            status = await client.start_import(
                replica.name,
                job_id=job.id,
                slug=model.slug,
                source_node=origin.name,
                grant=job.transfer_grant,
                manifest=manifest,
            )
            if not status.is_terminal:
                status = await client.get_job(replica.name, job.id)
            await self._mirror(job, status)
            if status.stage is JobStage.DONE:
                await self._record_copy(session, job, model, replica, status, role=CopyRole.REPLICA)
                await self._set_stage(session, job, model, DownloadStage.VERIFY_REPLICA)
            elif status.stage is JobStage.FAILED:
                await self._record_mismatch(session, job, model, replica, status)
                await self._fail(session, job, model, status.error or "replication failed", status)
            return

        if job.stage is DownloadStage.VERIFY_REPLICA:
            # Pass-through: the import verified file by file as it wrote.
            with contextlib.suppress(NodeError):
                await client.revoke_export(origin.name, model.slug)
            job.transfer_grant = None
            job.stage = DownloadStage.DONE
            job.finished_at = datetime.now(UTC)
            job.updated_at = datetime.now(UTC)
            state = await service.recompute_state(session, model)
            if state is ModelState.READY:
                await write_audit(
                    session,
                    actor=ACTOR,
                    action=AuditAction.MODEL_READY,
                    target_type="model",
                    target_id=str(model.id),
                    detail={"repo_id": model.repo_id},
                )
                logger.info("model %s is ready on both Studios", model.slug)

    async def _pull(
        self, client: NodeClient, job: DownloadJobRow, model: ModelRow, origin: str
    ) -> JobStatus:
        """Issue or re-attach to the pull. Idempotent on the job id (ADR-0005)."""
        status = await client.start_pull(
            origin,
            job_id=job.id,
            repo_id=model.repo_id,
            slug=model.slug,
            expected_total_bytes=model.total_bytes,
        )
        if not status.is_terminal:
            status = await client.get_job(origin, job.id)
        return status

    async def _job_nodes(
        self, session: AsyncSession, job: DownloadJobRow
    ) -> tuple[NodeRow, NodeRow]:
        origin = await session.get(NodeRow, job.origin_node_id)
        replica = await session.get(NodeRow, job.replica_node_id)
        if origin is None or replica is None:
            raise RuntimeError("a job references a node that no longer exists")
        return origin, replica

    async def _mirror(self, job: DownloadJobRow, status: JobStatus) -> None:
        if status.bytes_done > job.bytes_done:
            _download_bytes.add(status.bytes_done - job.bytes_done, {"stage": job.stage.value})
        job.bytes_done = status.bytes_done
        job.bytes_total = status.bytes_total or job.bytes_total
        job.files_done = status.files_done
        job.files_total = status.files_total or job.files_total
        job.updated_at = datetime.now(UTC)

    async def _set_stage(
        self,
        session: AsyncSession,
        job: DownloadJobRow,
        model: ModelRow,
        stage: DownloadStage,
    ) -> None:
        job.stage = stage
        job.updated_at = datetime.now(UTC)
        if stage in (DownloadStage.EXPORT, DownloadStage.IMPORT):
            await service.transition(
                session, model, ModelState.REPLICATING, "one verified copy; replicating"
            )

    async def _record_copy(
        self,
        session: AsyncSession,
        job: DownloadJobRow,
        model: ModelRow,
        node: NodeRow,
        status: JobStatus,
        *,
        role: CopyRole,
    ) -> None:
        existing = (
            await session.execute(
                select(ModelCopyRow).where(
                    ModelCopyRow.model_id == model.id, ModelCopyRow.node_id == node.id
                )
            )
        ).scalar_one_or_none()
        copy = existing or ModelCopyRow(model_id=model.id, node_id=node.id, role=role)
        copy.path = f"{self._settings.node_store_dir}/{model.slug}"
        copy.bytes = status.manifest.total_bytes if status.manifest else 0
        copy.manifest_sha256 = status.manifest_sha256
        copy.verified = True
        copy.verified_at = datetime.now(UTC)
        copy.mismatched_paths = []
        copy.role = role
        if existing is None:
            session.add(copy)
        logger.info("verified %s copy of %s on %s", role.value, model.slug, node.name)

    async def _record_mismatch(
        self,
        session: AsyncSession,
        job: DownloadJobRow,
        model: ModelRow,
        node: NodeRow,
        status: JobStatus,
    ) -> None:
        if not status.mismatched_paths:
            return
        existing = (
            await session.execute(
                select(ModelCopyRow).where(
                    ModelCopyRow.model_id == model.id, ModelCopyRow.node_id == node.id
                )
            )
        ).scalar_one_or_none()
        copy = existing or ModelCopyRow(model_id=model.id, node_id=node.id, role=CopyRole.REPLICA)
        copy.path = f"{self._settings.node_store_dir}/{model.slug}"
        copy.verified = False
        copy.mismatched_paths = status.mismatched_paths
        if existing is None:
            session.add(copy)
        logger.error(
            "checksum mismatch replicating %s to %s: %s",
            model.slug,
            node.name,
            status.mismatched_paths,
        )

    async def _handle_node_error(
        self,
        session: AsyncSession,
        job: DownloadJobRow,
        model: ModelRow,
        exc: NodeError,
    ) -> None:
        """A node problem. Retryable ones hold the stage; refusals fail the job."""
        if exc.retryable:
            reason = f"waiting for {exc.node}: {exc.kind.value}"
            await service.transition(
                session,
                model,
                ModelState.REPLICATING
                if job.stage in (DownloadStage.EXPORT, DownloadStage.IMPORT)
                else model.state,
                reason,
            )
            model.state_reason = reason
            job.updated_at = datetime.now(UTC)
            logger.info("job %s holding at %s: %s", job.id, job.stage.value, exc)
            return
        if exc.kind is NodeErrorKind.CONFLICT:
            logger.info("job %s: node busy (%s); holding", job.id, exc.detail)
            return
        await self._fail(session, job, model, f"{exc.kind.value}: {exc.detail or exc}")

    async def _fail(
        self,
        session: AsyncSession,
        job: DownloadJobRow,
        model: ModelRow,
        reason: str,
        status: JobStatus | None = None,
    ) -> None:
        job.stage = DownloadStage.FAILED
        job.failure_reason = reason
        job.finished_at = datetime.now(UTC)
        job.updated_at = datetime.now(UTC)
        await service.transition(session, model, ModelState.FAILED, reason)
        await write_audit(
            session,
            actor=ACTOR,
            action=AuditAction.MODEL_FAILED,
            target_type="model",
            target_id=str(model.id),
            outcome=AuditOutcome.ERROR,
            detail={
                "reason": reason,
                "stage": job.stage.value,
                "mismatched_paths": list(status.mismatched_paths) if status else [],
            },
        )
        logger.error("acquisition of %s failed: %s", model.slug, reason)

    # -- engines -----------------------------------------------------------
    async def _sync_engines(self, session: AsyncSession, client: NodeClient) -> None:
        rows = (
            (
                await session.execute(
                    select(EngineProcessRow).where(
                        EngineProcessRow.state.notin_(list(TERMINAL_ENGINE_STATES))
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return
        nodes = {n.id: n for n in (await session.execute(select(NodeRow))).scalars().all()}
        for row in rows:
            node = nodes.get(row.node_id)
            if node is None:
                continue
            try:
                status = await client.get_engine(node.name, row.id)
            except NodeError as exc:
                if exc.kind is NodeErrorKind.NOT_FOUND:
                    row.state = EngineState.FAILED
                    row.state_reason = "the node no longer knows this engine"
                    row.stopped_at = datetime.now(UTC)
                    await self._fail_instance_for_engine(session, row, row.state_reason)
                    logger.warning("engine %s is unknown to %s", row.id, node.name)
                continue
            row.state = status.state
            row.state_reason = status.state_reason
            row.pid = status.pid
            row.process_create_time = status.process_create_time
            row.resident_bytes = status.resident_bytes
            row.resident_delta_bytes = status.resident_delta_bytes
            row.cpu_percent = status.cpu_percent
            row.chat_template_sha256 = status.chat_template_sha256
            row.load_seconds = status.load_seconds
            row.last_health_at = status.last_health_at
            if status.state in TERMINAL_ENGINE_STATES and row.stopped_at is None:
                row.stopped_at = status.stopped_at or datetime.now(UTC)
                await self._fail_instance_for_engine(
                    session, row, status.state_reason or "engine exited while instance was active"
                )

    async def _reconcile_nodes(self, session: AsyncSession, client: NodeClient) -> None:
        """Ask named nodes what they are really running (spec FR-015)."""
        pending, self._node_reconcile = self._node_reconcile, set()
        for name in sorted(pending):
            node = (
                await session.execute(select(NodeRow).where(NodeRow.name == name))
            ).scalar_one_or_none()
            if node is None:
                continue
            rows = (
                (
                    await session.execute(
                        select(EngineProcessRow).where(
                            EngineProcessRow.node_id == node.id,
                            EngineProcessRow.state.notin_(list(TERMINAL_ENGINE_STATES)),
                        )
                    )
                )
                .scalars()
                .all()
            )
            expected = [
                ReconcileExpectation(
                    engine_id=r.id,
                    slug="",
                    port=r.port,
                    pid=r.pid,
                    process_create_time=r.process_create_time,
                )
                for r in rows
            ]
            try:
                result = await client.reconcile(node.name, ReconcileRequest(expected=expected))
            except NodeError as exc:
                logger.warning("could not reconcile %s: %s", node.name, exc)
                self._node_reconcile.add(name)
                continue

            by_id = {r.id: r for r in rows}
            for adopted in result.adopted:
                row = by_id.get(adopted.engine_id) if adopted.engine_id else None
                if row is not None:
                    row.state = adopted.state
                    row.pid = adopted.pid
                    row.process_create_time = adopted.process_create_time
                    row.state_reason = adopted.state_reason

            for dead_id in result.dead:
                row = by_id.get(dead_id)
                if row is None:
                    continue
                row.state = EngineState.FAILED
                row.state_reason = "process gone during agent restart"
                row.stopped_at = datetime.now(UTC)
                await self._fail_instance_for_engine(
                    session, row, "process gone during agent restart"
                )
                await write_audit(
                    session,
                    actor=ACTOR,
                    action=AuditAction.ENGINE_RECONCILE,
                    target_type="engine",
                    target_id=str(dead_id),
                    outcome=AuditOutcome.ERROR,
                    detail={"node": node.name, "reason": "process gone during agent restart"},
                )

            for orphan in result.orphans:
                await self._record_orphan(session, node, orphan)

            if result.dead or result.orphans:
                logger.warning(
                    "reconciled %s: %d adopted, %d dead, %d orphan",
                    node.name,
                    len(result.adopted),
                    len(result.dead),
                    len(result.orphans),
                )

    async def _fail_instance_for_engine(
        self, session: AsyncSession, engine: EngineProcessRow, reason: str
    ) -> None:
        if engine.instance_id is None:
            return
        instance = await session.get(ModelInstanceRow, engine.instance_id)
        if instance is None or instance.state in {
            InstanceState.STOPPED,
            InstanceState.FAILED,
            InstanceState.DRAINING,
        }:
            return
        await instance_service.transition(
            session,
            instance.id,
            InstanceState.FAILED,
            reason=reason,
            failure_code="engine_failed",
        )
        reservation = await session.scalar(
            select(MemoryReservationRow).where(
                MemoryReservationRow.node_id == engine.node_id,
                MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                MemoryReservationRow.holder_id == str(instance.id),
                MemoryReservationRow.state.in_(
                    [
                        MemoryReservationState.PENDING,
                        MemoryReservationState.HELD,
                        MemoryReservationState.RELEASING,
                    ]
                ),
            )
        )
        if reservation is not None:
            reservation.state = MemoryReservationState.RELEASED
            reservation.released_at = datetime.now(UTC)

    async def _record_orphan(self, session: AsyncSession, node: NodeRow, orphan: object) -> None:
        pid = getattr(orphan, "pid", None)
        orphan_id = getattr(orphan, "engine_id", None)
        # A launch can commit while the node reconciliation request is in flight. In that
        # case the node truthfully labelled the process orphaned against the older expected
        # set, but its stable engine id is now authoritative control-plane state. Recheck by
        # identity after the network round trip before attempting an orphan insert.
        if orphan_id is not None and await session.get(EngineProcessRow, orphan_id) is not None:
            return
        existing = (
            await session.execute(
                select(EngineProcessRow).where(
                    EngineProcessRow.node_id == node.id,
                    EngineProcessRow.pid == pid,
                    EngineProcessRow.state == EngineState.ORPHAN,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        slug = getattr(orphan, "slug", None)
        model = None
        if slug:
            model = (
                await session.execute(select(ModelRow).where(ModelRow.slug == slug))
            ).scalar_one_or_none()
        row = EngineProcessRow(
            # Preserve the node-assigned identity so a later admin DELETE addresses the same
            # process in both control-plane and node state.
            id=orphan_id or uuid.uuid4(),
            model_id=model.id if model else None,
            node_id=node.id,
            port=getattr(orphan, "port", 0),
            pid=pid,
            process_create_time=getattr(orphan, "process_create_time", None),
            state=EngineState.ORPHAN,
            state_reason="running but not started by this control plane",
            resident_bytes=getattr(orphan, "resident_bytes", None),
        )
        session.add(row)
        await write_audit(
            session,
            actor=ACTOR,
            action=AuditAction.ENGINE_RECONCILE,
            target_type="engine",
            target_id=str(row.id),
            outcome=AuditOutcome.ERROR,
            detail={"node": node.name, "pid": pid, "slug": slug, "reason": "orphan"},
        )
        logger.warning("orphan engine on %s: pid %s serving %s", node.name, pid, slug)

    # -- retirement --------------------------------------------------------
    async def _drive_retirement(self, session: AsyncSession, client: NodeClient) -> None:
        """Stop engines, then delete copies, until every node confirms (US5 scenario 3)."""
        models = (
            (await session.execute(select(ModelRow).where(ModelRow.state == ModelState.RETIRED)))
            .scalars()
            .all()
        )
        if not models:
            return
        nodes = {n.id: n for n in (await session.execute(select(NodeRow))).scalars().all()}

        for model in models:
            engines = (
                (
                    await session.execute(
                        select(EngineProcessRow).where(
                            EngineProcessRow.model_id == model.id,
                            EngineProcessRow.state.in_(list(LIVE_ENGINE_STATES)),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for engine in engines:
                node = nodes.get(engine.node_id)
                if node is None:
                    continue
                with contextlib.suppress(NodeError):
                    await client.stop_engine(node.name, engine.id)
                    engine.state = EngineState.STOPPING

            if engines:
                continue  # delete the files once nothing is serving them

            copies = (
                (
                    await session.execute(
                        select(ModelCopyRow).where(ModelCopyRow.model_id == model.id)
                    )
                )
                .scalars()
                .all()
            )
            for copy in copies:
                node = nodes.get(copy.node_id)
                if node is None:
                    await session.delete(copy)
                    continue
                try:
                    await client.delete_model(node.name, model.slug)
                except NodeError as exc:
                    logger.info("retirement of %s waiting on %s: %s", model.slug, node.name, exc)
                    continue
                await session.delete(copy)

    # -- metrics -----------------------------------------------------------
    async def _publish_metrics(self, session: AsyncSession) -> None:
        for state in ModelState:
            count = len(
                (await session.execute(select(ModelRow).where(ModelRow.state == state)))
                .scalars()
                .all()
            )
            _model_state.set(count, {"state": state.value})
        for engine_state in EngineState:
            count = len(
                (
                    await session.execute(
                        select(EngineProcessRow).where(EngineProcessRow.state == engine_state)
                    )
                )
                .scalars()
                .all()
            )
            _engine_state_gauge.set(count, {"state": engine_state.value})
