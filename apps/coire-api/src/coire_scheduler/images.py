"""DBOS recovery of a fenced image transfer without rerunning generation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dbos import DBOS
from opentelemetry import metrics, trace
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    ImageExecutionLeaseRow,
    ImageJobEventRow,
    ImageJobRow,
    ImageOutputRow,
    ImageTransferRow,
    NodeRow,
    session_scope,
)
from coire_api.images.expiry import expire_queued_image_job
from coire_api.images.job_capacity import release_pending_image_job_capacity
from coire_api.images.jobs import _policy
from coire_api.images.maintenance import purge_cancelled_transfer_staging
from coire_api.images.observation import reconcile_image_observation
from coire_api.images.quota import _QUOTA_LOCK, release_storage_hold
from coire_api.images.storage import (
    ImagePublicationRevoked,
    fail_revoked_image_batch,
    publish_image_batch,
)
from coire_api.images.transfer import mint_transfer_grant, require_bound_image_spec
from coire_api.nodes_client import NodeClient, NodeError, NodeErrorKind
from coire_core.errors import ImageConflict, ImageForbidden
from coire_core.models.image_worker import (
    ImageJobBinding,
    ImageTransferGrantRequest,
    ImageTransferReceipt,
    NodeImageCancelRequest,
    NodeImageJob,
    NodeImageTransferRequest,
)
from coire_core.models.images import ImageJobEvent, ImageJobState
from coire_core.settings import Settings, get_settings
from coire_scheduler.image_dispatch import (
    _access_current,
    prepare_image_dispatch,
    request_revoked_image_cancel,
    request_thermal_image_cancel,
)

tracer = trace.get_tracer("coire.scheduler.images")
logger = logging.getLogger(__name__)
transfers_total = metrics.get_meter("coire.scheduler.images").create_counter(
    "coire_image_transfer_recovery_total", unit="1", description="Fenced image transfer recovery"
)
cancellations_total = metrics.get_meter("coire.scheduler.images").create_counter(
    "coire_image_cancellation_recovery_total",
    unit="1",
    description="Fenced image cancellation recovery",
)
observations_total = metrics.get_meter("coire.scheduler.images").create_counter(
    "coire_image_observation_total", unit="1", description="Fenced image journal observations"
)
publications_total = metrics.get_meter("coire.scheduler.images").create_counter(
    "coire_image_publication_total", unit="1", description="Whole-batch image publication"
)
queue_expirations_total = metrics.get_meter("coire.scheduler.images").create_counter(
    "coire_image_queue_expiration_total", unit="1", description="Overdue image queue outcomes"
)
dispatches_total = metrics.get_meter("coire.scheduler.images").create_counter(
    "coire_image_dispatch_total", unit="1", description="Fenced image placement outcomes"
)


def _binding(row: ImageJobRow) -> ImageJobBinding:
    return ImageJobBinding(job_id=row.id, attempt=row.attempt, fence=row.fence)


def _same_attempt(row: ImageJobRow, status: NodeImageJob, node: str) -> bool:
    return (
        row.id == status.job_id
        and row.attempt == status.attempt
        and row.fence == status.fence
        and row.instance_id == status.instance_id
        and status.node == node
    )


async def drive_image_transfer(job_id: str) -> None:
    """Reconcile journal -> grants -> core receipts -> Studio cleanup acknowledgment."""
    async with session_scope() as session:
        row = await session.get(ImageJobRow, job_id)
        if row is None or row.state != "transferring":
            return
        if row.cleanup_state == "cleaned" and row.receipt_state == "complete":
            return
        if row.selected_node_id is None or row.instance_id is None or row.fence < 1:
            raise ImageConflict("image placement is incomplete")
        selected = await session.get(NodeRow, row.selected_node_id)
        if selected is None:
            raise ImageConflict("image node is unavailable")
        node = selected.name
        binding = _binding(row)
        resolved = require_bound_image_spec(row)
        instance_id = row.instance_id

    settings = get_settings()
    with tracer.start_as_current_span("coire.scheduler.image.transfer"):
        async with NodeClient(settings) as client:
            status = await client.image_job_status(node, binding)
            if (
                status.job_id != job_id
                or status.attempt != binding.attempt
                or status.fence != binding.fence
                or status.node != node
                or status.instance_id != instance_id
            ):
                raise ImageConflict("node image attempt differs from core")
            if status.state == "transferring":
                if {item.index for item in status.outputs} != set(range(resolved.spec.n)):
                    raise ImageConflict("image output manifest is incomplete")
                async with session_scope() as session:
                    locked = await session.get(ImageJobRow, job_id, with_for_update=True)
                    if (
                        locked is None
                        or locked.state != "transferring"
                        or locked.cancel_requested_at is not None
                        or not _same_attempt(locked, status, node)
                    ):
                        raise ImageConflict("image attempt changed before transfer")
                    grants = tuple(
                        [
                            await mint_transfer_grant(
                                session,
                                ImageTransferGrantRequest(
                                    job_id=job_id,
                                    attempt=binding.attempt,
                                    fence=binding.fence,
                                    node=node,
                                    index=output.index,
                                    expected_bytes=output.byte_count,
                                    expected_sha256=output.sha256,
                                ),
                            )
                            for output in sorted(status.outputs, key=lambda item: item.index)
                        ]
                    )
                status = await client.transfer_image_job(
                    node,
                    NodeImageTransferRequest(
                        job_id=job_id,
                        attempt=binding.attempt,
                        fence=binding.fence,
                        node=node,
                        grants=grants,
                    ),
                )
            if status.state != "succeeded" or not status.scratch_cleaned:
                raise ImageConflict("node image cleanup is incomplete")
    if len(status.receipts) != resolved.spec.n:
        raise ImageConflict("node image receipts are incomplete")
    async with session_scope() as session:
        locked = await session.get(ImageJobRow, job_id, with_for_update=True)
        if (
            locked is None
            or locked.state != "transferring"
            or locked.cancel_requested_at is not None
            or not _same_attempt(locked, status, node)
        ):
            raise ImageConflict("image attempt changed after cleanup")
        receipts = {receipt.index: receipt for receipt in status.receipts}
        if set(receipts) != set(range(resolved.spec.n)):
            raise ImageConflict("image receipt indexes are incomplete")
        now = datetime.now(UTC)
        for index in sorted(receipts):
            transfer = await session.get(
                ImageTransferRow, (job_id, binding.attempt, index), with_for_update=True
            )
            receipt = receipts[index]
            if transfer is None or transfer.receipt is None:
                raise ImageConflict("core image receipt is missing")
            stored = ImageTransferReceipt.model_validate(transfer.receipt)
            if stored != receipt:
                raise ImageConflict("core and node image receipts differ")
            transfer.node_cleanup_ack_at = now
        locked.receipt_state = "complete"
        locked.cleanup_state = "cleaned"
        locked.updated_at = now
    transfers_total.add(1, {"outcome": "cleaned"})


async def finalize_cancelled_image_job(
    session: AsyncSession,
    status: NodeImageJob,
    selected_node_id: uuid.UUID,
    settings: Settings,
) -> bool:
    """Commit terminal cancellation only after both node and core scratch are absent."""
    if status.state != "cancelled" or not status.scratch_cleaned:
        raise ImageConflict("node cancellation cleanup is incomplete")
    await session.execute(_QUOTA_LOCK)
    row = await session.get(
        ImageJobRow, status.job_id, populate_existing=True, with_for_update=True
    )
    if row is None or row.state == "cancelled":
        return False
    if (
        row.state != "cancelling"
        or row.cancel_requested_at is None
        or row.selected_node_id != selected_node_id
        or not _same_attempt(row, status, status.node)
    ):
        raise ImageConflict("image cancellation attempt changed")
    if (
        await session.scalar(
            select(ImageOutputRow.id)
            .where(ImageOutputRow.job_id == row.id, ImageOutputRow.state == "published")
            .limit(1)
        )
        is not None
    ):
        raise ImageConflict("image publication already won")
    leases = (
        await session.scalars(
            select(ImageExecutionLeaseRow)
            .where(
                ImageExecutionLeaseRow.job_id == row.id,
                ImageExecutionLeaseRow.released_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    if len(leases) != 1:
        raise ImageConflict("image execution lease is unavailable")
    lease = leases[0]
    if lease.mode != "image" or lease.node_id != selected_node_id or lease.fence != row.fence:
        raise ImageConflict("image execution lease differs from attempt")
    transfers = (
        await session.scalars(
            select(ImageTransferRow)
            .where(ImageTransferRow.job_id == row.id, ImageTransferRow.attempt == row.attempt)
            .with_for_update()
        )
    ).all()
    snapshot, _, _ = _policy(row)
    held = row.authorization_snapshot.get("output_hold_bytes")
    if not isinstance(held, int) or isinstance(held, bool) or held <= 0:
        raise ImageConflict("image capacity hold unavailable")
    await asyncio.to_thread(
        purge_cancelled_transfer_staging,
        Path(settings.image_blob_root),
        row.id,
        row.attempt,
    )
    if snapshot.resolved is None:
        await release_pending_image_job_capacity(
            session, row.owner_user_id, snapshot.effective_spec.n, held
        )
    else:
        await release_storage_hold(session, row.owner_user_id, held)
    latest = await session.scalar(
        select(func.max(ImageJobEventRow.sequence)).where(ImageJobEventRow.job_id == row.id)
    )
    if latest is None or latest < 1:
        raise ImageConflict("image event history unavailable")
    now = datetime.now(UTC)
    for transfer in transfers:
        transfer.state = "cancelled"
        transfer.lease_expires_at = now
    lease.released_at = now
    lease.release_evidence = {
        "state": "cancelled",
        "node": status.node,
        "scratch_cleaned": True,
        "observed_at": status.updated_at.isoformat(),
    }
    row.state = ImageJobState.CANCELLED
    row.cleanup_state = "cleaned"
    row.receipt_state = "none"
    row.updated_at = now
    row.finished_at = now
    row.version += 1
    event = ImageJobEvent(
        job_id=row.id,
        sequence=latest + 1,
        at=now,
        type="cancelled",
        state=ImageJobState.CANCELLED,
    )
    session.add(
        ImageJobEventRow(
            job_id=row.id,
            sequence=event.sequence,
            event_type=event.type,
            payload=event.model_dump(mode="json"),
            created_at=now,
        )
    )
    return True


async def drive_image_cancel(job_id: str) -> None:
    """Replay the same fenced stop request until the node proves cleanup."""
    async with session_scope() as session:
        row = await session.get(ImageJobRow, job_id)
        if row is None or row.state != "cancelling":
            return
        if (
            row.selected_node_id is None
            or row.instance_id is None
            or row.fence < 1
            or row.cancel_requested_at is None
        ):
            raise ImageConflict("image cancellation placement is uncertain")
        selected = await session.get(NodeRow, row.selected_node_id)
        if selected is None:
            raise ImageConflict("image cancellation node is unavailable")
        node = selected.name
        selected_node_id = row.selected_node_id
        request = NodeImageCancelRequest(
            job_id=job_id,
            attempt=row.attempt,
            fence=row.fence,
            reason="user",
            requested_at=row.cancel_requested_at,
        )
    settings = get_settings()
    with tracer.start_as_current_span("coire.scheduler.image.cancel"):
        async with NodeClient(settings) as client:
            status = await client.cancel_image_job(node, request)
    if (
        status.job_id != request.job_id
        or status.attempt != request.attempt
        or status.fence != request.fence
        or status.node != node
    ):
        raise ImageConflict("node cancellation identity differs from core")
    async with session_scope() as session:
        if await finalize_cancelled_image_job(session, status, selected_node_id, settings):
            cancellations_total.add(1, {"outcome": "cancelled"})


async def observe_image_job(job_id: str) -> bool:
    """Poll an existing node journal; never issue a start command on recovery."""
    async with session_scope() as session:
        row = await session.get(ImageJobRow, job_id)
        if row is None or row.state not in {"reserving", "running"}:
            return False
        if row.cancel_requested_at is not None:
            return False
        _, _, explicit = _policy(row)
        if not await _access_current(session, row, explicit=explicit):
            with tracer.start_as_current_span("coire.scheduler.image.revocation"):
                if await request_revoked_image_cancel(session, job_id):
                    observations_total.add(1, {"outcome": "revoked"})
                    logger.info(
                        "image authorization revoked; cancellation requested",
                        extra={"job_id": job_id},
                    )
            return False
        with tracer.start_as_current_span("coire.scheduler.image.thermal_check"):
            if await request_thermal_image_cancel(session, job_id):
                observations_total.add(1, {"outcome": "thermal_alarm"})
                logger.warning(
                    "image node thermal alarm; cancellation requested",
                    extra={"job_id": job_id, "node_id": str(row.selected_node_id)},
                )
                return False
        if row.selected_node_id is None or row.instance_id is None or row.fence < 1:
            raise ImageConflict("image placement is incomplete")
        selected = await session.get(NodeRow, row.selected_node_id)
        if selected is None:
            raise ImageConflict("image node is unavailable")
        node = selected.name
        selected_node_id = row.selected_node_id
        binding = _binding(row)
    with tracer.start_as_current_span("coire.scheduler.image.observe"):
        async with NodeClient(get_settings()) as client:
            try:
                observed = await client.image_job_status(node, binding)
            except NodeError as exc:
                if exc.kind is not NodeErrorKind.NOT_FOUND:
                    raise
                observations_total.add(1, {"outcome": "journal_missing"})
                raise ImageConflict("node image journal is unavailable") from exc
        async with session_scope() as session:
            more = await reconcile_image_observation(
                session, job_id, selected_node_id, observed, expected_node=node
            )
    observations_total.add(1, {"outcome": "active" if more else "transitioned"})
    return more


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def image_observe_step(job_id: str) -> bool:
    try:
        return await observe_image_job(job_id)
    except Exception:
        observations_total.add(1, {"outcome": "failed"})
        raise


@DBOS.workflow(name="coire.image.observe", max_recovery_attempts=100)
async def image_observe_workflow(job_id: str) -> None:
    while await image_observe_step(job_id):  # noqa: ASYNC110 - DBOS polls until transfer
        await asyncio.sleep(0.5)


async def drive_image_publication(job_id: str) -> None:
    with tracer.start_as_current_span("coire.scheduler.image.publish"):
        settings = get_settings()
        try:
            async with session_scope() as session:
                published = await publish_image_batch(session, job_id, settings)
            if published:
                publications_total.add(1, {"outcome": "succeeded"})
        except (ImageForbidden, ImagePublicationRevoked) as exc:
            code = (
                "authorization_revoked" if isinstance(exc, ImageForbidden) else "model_unavailable"
            )
            async with session_scope() as session:
                denied = await fail_revoked_image_batch(session, job_id, settings, safe_code=code)
            if denied:
                publications_total.add(1, {"outcome": "denied"})


async def drive_image_queue_expiry(job_id: str) -> None:
    with tracer.start_as_current_span("coire.scheduler.image.queue_expire"):
        async with session_scope() as session:
            expired = await expire_queued_image_job(session, job_id)
    if expired:
        queue_expirations_total.add(1, {"outcome": "expired"})


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def image_queue_expiry_step(job_id: str) -> None:
    try:
        await drive_image_queue_expiry(job_id)
    except Exception:
        queue_expirations_total.add(1, {"outcome": "failed"})
        raise


@DBOS.workflow(name="coire.image.queue_expire", max_recovery_attempts=100)
async def image_queue_expiry_workflow(job_id: str) -> None:
    await image_queue_expiry_step(job_id)


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def image_publish_step(job_id: str) -> None:
    try:
        await drive_image_publication(job_id)
    except Exception:
        publications_total.add(1, {"outcome": "failed"})
        raise


@DBOS.workflow(name="coire.image.publish", max_recovery_attempts=100)
async def image_publish_workflow(job_id: str) -> None:
    await image_publish_step(job_id)


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def image_cancel_step(job_id: str) -> None:
    try:
        await drive_image_cancel(job_id)
    except Exception:
        cancellations_total.add(1, {"outcome": "failed"})
        raise


@DBOS.workflow(name="coire.image.cancel", max_recovery_attempts=100)
async def image_cancel_workflow(job_id: str) -> None:
    await image_cancel_step(job_id)


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def image_transfer_step(job_id: str) -> None:
    try:
        await drive_image_transfer(job_id)
    except Exception:
        transfers_total.add(1, {"outcome": "failed"})
        raise


@DBOS.workflow(name="coire.image.transfer", max_recovery_attempts=100)
async def image_transfer_workflow(job_id: str) -> None:
    await image_transfer_step(job_id)


async def drive_image_dispatch(job_id: str) -> bool:
    """Place a queued job, then send one idempotent worker load and start.

    False means the job is still queued and should be tried again. A completed
    attempt is never submitted a second time.
    """
    settings = get_settings()
    async with session_scope() as session:
        prepared = await prepare_image_dispatch(session, job_id, settings)
    if prepared is None:
        async with session_scope() as session:
            row = await session.get(ImageJobRow, job_id)
        return not (
            row is not None
            and row.state == "queued"
            and row.cancel_requested_at is None
            and row.deadline_at > datetime.now(UTC)
        )
    with tracer.start_as_current_span("coire.scheduler.image.dispatch"):
        async with NodeClient(settings) as client:
            try:
                loaded = await client.load_image_worker(prepared.node, prepared.load)
            except NodeError as exc:
                if exc.kind is NodeErrorKind.NOT_FOUND:
                    dispatches_total.add(1, {"outcome": "worker_missing"})
                    raise ImageConflict("image worker state is unavailable") from exc
                raise
            if loaded.state == "failed" or loaded.instance_id != prepared.load.instance_id:
                dispatches_total.add(1, {"outcome": "worker_uncertain"})
                raise ImageConflict("image worker state differs from dispatch")
            try:
                started = await client.start_image_job(prepared.node, prepared.start)
            except NodeError as exc:
                if exc.kind is NodeErrorKind.NOT_FOUND:
                    dispatches_total.add(1, {"outcome": "journal_missing"})
                    raise ImageConflict("node image job journal is unavailable") from exc
                if exc.kind is NodeErrorKind.CONFLICT:
                    dispatches_total.add(1, {"outcome": "conflict"})
                    return True
                raise
        if (
            started.job_id != prepared.start.job_id
            or started.attempt != prepared.start.attempt
            or started.fence != prepared.start.fence
            or started.node != prepared.node
            or started.instance_id != prepared.start.instance_id
        ):
            raise ImageConflict("node image attempt differs from core")
        dispatches_total.add(1, {"outcome": "started"})
        return True


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1.0)
async def image_dispatch_step(job_id: str) -> bool:
    try:
        return await drive_image_dispatch(job_id)
    except Exception:
        dispatches_total.add(1, {"outcome": "failed"})
        raise


@DBOS.workflow(name="coire.image.dispatch", max_recovery_attempts=100)
async def image_dispatch_workflow(job_id: str) -> None:
    while not await image_dispatch_step(job_id):  # noqa: ASYNC110 - DBOS waits for a Studio
        await asyncio.sleep(1)
