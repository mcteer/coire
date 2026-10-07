"""Counted core disk holds acquired before multipart parsing or private source writes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import anyio
from opentelemetry import trace
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import TrainingDatasetRevisionRow, TrainingStorageReservationRow
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.storage import DatasetStore
from coire_core.errors import (
    TrainingConflict,
    TrainingQuotaExceeded,
    TrainingUnavailable,
    TrainingUploadTooLarge,
)
from coire_core.settings import Settings

tracer = trace.get_tracer("coire.api.training.datasets")
COUNTED_STATES = ("held", "retained", "releasing")


def upload_reservation_bytes(declared_bytes: int, settings: Settings) -> int:
    if (
        type(declared_bytes) is not int
        or not 1 <= declared_bytes <= settings.training_dataset_upload_max_bytes + 64 * 1024
    ):
        raise TrainingUploadTooLarge()
    # Count request spool + private source + a conservative bounded row-index/
    # digest manifest. A tiny JSONL corpus can have more index bytes than source.
    rows = min(settings.training_dataset_max_rows, declared_bytes // 2 + 1)
    return 2 * declared_bytes + rows * 128


async def reserve_upload(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    *,
    declared_bytes: int,
    subject_id: uuid.UUID,
) -> TrainingStorageReservationRow:
    if not settings.training_enabled:
        raise TrainingUnavailable("Training datasets are disabled")
    owner = await authorize_live_training_action(session, principal)
    needed = upload_reservation_bytes(declared_bytes, settings)
    with tracer.start_as_current_span("coire.api.training.dataset.reserve"):
        await session.execute(
            select(
                func.pg_advisory_xact_lock(func.hashtextextended("coire.training.dataset.disk", 0))
            )
        )
        counted = await session.scalar(
            select(func.coalesce(func.sum(TrainingStorageReservationRow.bytes), 0)).where(
                TrainingStorageReservationRow.node_id.is_(None),
                TrainingStorageReservationRow.state.in_(COUNTED_STATES),
            )
        )
        if int(counted or 0) + needed > settings.training_dataset_quota_bytes:
            raise TrainingQuotaExceeded("Aggregate dataset storage quota is unavailable")
        row = TrainingStorageReservationRow(
            id=uuid.uuid4(),
            owner_user_id=owner,
            node_id=None,
            subject_id=str(subject_id),
            bytes=needed,
            state="held",
        )
        session.add(row)
        await session.flush()
        return row


async def finish_upload_hold(
    session: AsyncSession,
    reservation_id: uuid.UUID,
    *,
    retained_bytes: int,
    private_staging_cleanup_proven: bool,
) -> None:
    """Only internal storage/route code calls this after actual file cleanup.

    An uncertain source/spool/stage remains counted. Never mark a failed request
    free solely from its HTTP outcome or an elapsed timeout.
    """
    row = await session.get(TrainingStorageReservationRow, reservation_id, with_for_update=True)
    if (
        row is None
        or type(retained_bytes) is not int
        or type(private_staging_cleanup_proven) is not bool
        or not 0 <= retained_bytes <= row.bytes
    ):
        raise TrainingConflict("Dataset storage hold does not match its completed bytes")
    if row.state == "retained":
        if retained_bytes != row.bytes:
            raise TrainingConflict("Published source bytes require separate verified deletion")
        return
    if row.state == "released":
        if retained_bytes:
            raise TrainingConflict("Released storage hold cannot acquire retained bytes")
        return
    if not private_staging_cleanup_proven:
        row.state = "releasing"
        return
    if retained_bytes:
        row.bytes = retained_bytes
        row.state = "retained"
    else:
        row.state = "released"
        row.released_at = datetime.now(UTC)
    await session.flush()


async def reconcile_upload_holds(session: AsyncSession, settings: Settings) -> None:
    """Sweep expired private staging, preserving any committed source publication.

    Request processing is bounded by analysis_timeout_s. Never sweep a hold
    younger than that bound even when a shorter retention setting is selected.
    Cleanup exceptions abort the transaction and keep its original disk charge.
    """
    cutoff = datetime.now(UTC) - timedelta(
        seconds=max(settings.training_staging_retention_s, settings.training_analysis_timeout_s)
    )
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended("coire.training.dataset.disk", 0)))
    )
    holds = list(
        await session.scalars(
            select(TrainingStorageReservationRow)
            .where(
                TrainingStorageReservationRow.node_id.is_(None),
                TrainingStorageReservationRow.state.in_(("held", "releasing")),
                TrainingStorageReservationRow.created_at < cutoff,
            )
            .order_by(TrainingStorageReservationRow.created_at)
            .limit(8)
            .with_for_update()
        )
    )
    if not holds:
        return
    store = await anyio.to_thread.run_sync(DatasetStore, settings)
    for hold in holds:
        identity = uuid.UUID(hold.subject_id)
        dataset = await session.get(TrainingDatasetRevisionRow, identity, with_for_update=True)
        await store.discard(hold.id)
        retained = 0
        if dataset is not None and dataset.state not in {"failed", "purged"}:
            retained = await store.retained_size(identity)
        else:
            await store.purge_source(identity)
        await finish_upload_hold(
            session, hold.id, retained_bytes=retained, private_staging_cleanup_proven=True
        )
