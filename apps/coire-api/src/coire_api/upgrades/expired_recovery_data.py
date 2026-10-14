"""One bounded consistent Core data read, returning fingerprints rather than content."""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    RECOVERY_PRESERVATION_TABLES,
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryIntent,
)

tracer = trace.get_tracer("coire.api.recovery_data")


@tracer.start_as_current_span("coire.api.recovery_data_snapshot", record_exception=False)
async def capture_data_snapshot(
    session: AsyncSession, intent: UpgradeExpiredRecoveryIntent
) -> UpgradeExpiredRecoveryDataSnapshot:
    return await _capture_data_snapshot(session, intent, metadata_only=False)


@tracer.start_as_current_span("coire.api.unstarted_recovery_data_snapshot", record_exception=False)
async def capture_unstarted_data_snapshot(
    session: AsyncSession, intent: UpgradeExpiredRecoveryIntent
) -> UpgradeExpiredRecoveryDataSnapshot:
    """Fixed bounded hashes for closing unstarted metadata; no effect authority."""
    return await capture_expired_metadata_snapshot(session, intent)


@tracer.start_as_current_span(
    "coire.api.expired_recovery_metadata_snapshot", record_exception=False
)
async def capture_expired_metadata_snapshot(
    session: AsyncSession, intent: UpgradeExpiredRecoveryIntent
) -> UpgradeExpiredRecoveryDataSnapshot:
    """Expired metadata only; the execution snapshot gate remains separate."""
    if datetime.now(UTC) < intent.deadline_at:
        raise UpgradeConflict("Expired metadata hashing requires an expired intent")
    return await _capture_data_snapshot(session, intent, metadata_only=True)


async def _capture_data_snapshot(
    session: AsyncSession, intent: UpgradeExpiredRecoveryIntent, *, metadata_only: bool
) -> UpgradeExpiredRecoveryDataSnapshot:
    """Fixed identifiers only; same statement snapshot across all selected tables.

    Read at most 20,001 rows per table to detect overflow. PostgreSQL hashes each
    projected row with its built-in SHA-256; private content never leaves the
    database. Core combines only bounded sorted digests/counts. No content is
    logged, returned to a node or retained in journals. Operational qualification
    fields can legitimately change without rewriting user evaluation history.
    """
    remaining = (
        5.0 if metadata_only else min(5.0, (intent.deadline_at - datetime.now(UTC)).total_seconds())
    )
    if remaining <= 0:
        raise UpgradeUnavailable("Recovery data observation budget is exhausted")
    fragments = []
    for table in RECOVERY_PRESERVATION_TABLES:
        projection = "to_jsonb(t)"
        if table == "harness_evaluations":
            projection += " - 'runtime_sha256' - 'harness_image'"
        fragments.append(
            "'" + table + "', (SELECT coalesce(jsonb_agg(value ORDER BY value), '[]'::jsonb) "
            "FROM (SELECT encode(sha256(convert_to(("
            + projection
            + ")::text, 'UTF8')), 'hex') AS value FROM "
            + table
            + " t LIMIT 20001) bounded)"
        )
    statement = text("SELECT jsonb_build_object(" + ",".join(fragments) + ")")
    try:
        async with asyncio.timeout(remaining):
            result = await session.execute(statement)
            document = result.scalar_one()
    except TimeoutError:
        raise UpgradeUnavailable("Recovery data observation exceeded its captured budget") from None
    if not isinstance(document, dict) or set(document) != set(RECOVERY_PRESERVATION_TABLES):
        raise UpgradeUnavailable("Recovery data observation is incomplete")
    counts = {}
    digests = {}
    total = 0
    for table in RECOVERY_PRESERVATION_TABLES:
        rows = document[table]
        if (
            not isinstance(rows, list)
            or len(rows) > 20000
            or any(
                not isinstance(row, str) or re.fullmatch(r"[a-f0-9]{64}", row) is None
                for row in rows
            )
        ):
            raise UpgradeUnavailable("Recovery data observation exceeds its row bound")
        payload = json.dumps(
            rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        total += len(payload)
        if total > 8 * 1024 * 1024:
            raise UpgradeUnavailable("Recovery data observation exceeds its byte bound")
        counts[table] = len(rows)
        digests[table] = hashlib.sha256(payload).hexdigest()
    now = datetime.now(UTC)
    if not metadata_only and not intent.accepted_at <= now < intent.deadline_at:
        raise UpgradeUnavailable("Recovery data observation exceeded captured authority")
    return UpgradeExpiredRecoveryDataSnapshot(
        recovery_id=intent.recovery_id,
        original_job_id=intent.request.original_job_id,
        original_generation=intent.request.original_generation,
        source_sha256=intent.request.expected_source_sha256,
        table_sha256s=digests,
        row_counts=counts,
        observed_at=now,
    )


def verify_data_preserved(
    before: UpgradeExpiredRecoveryDataSnapshot,
    after: UpgradeExpiredRecoveryDataSnapshot,
    intent: UpgradeExpiredRecoveryIntent,
) -> None:
    for snapshot in (before, after):
        if (
            snapshot.recovery_id,
            snapshot.original_job_id,
            snapshot.original_generation,
            snapshot.source_sha256,
        ) != (
            intent.recovery_id,
            intent.request.original_job_id,
            intent.request.original_generation,
            intent.request.expected_source_sha256,
        ):
            raise UpgradeConflict("Recovery data observation belongs to another captured authority")
    if (
        before.table_sha256s != after.table_sha256s
        or before.row_counts != after.row_counts
        or not intent.accepted_at <= before.observed_at <= after.observed_at < intent.deadline_at
    ):
        raise UpgradeUnavailable("Recovery cannot prove preserved original user data")


def verify_unstarted_data_preserved(
    before: UpgradeExpiredRecoveryDataSnapshot,
    after: UpgradeExpiredRecoveryDataSnapshot,
    intent: UpgradeExpiredRecoveryIntent,
) -> None:
    """Compare post-expiry metadata hashes; never satisfy a recovery effect gate."""
    verify_expired_metadata_preserved(before, after, intent)


def verify_expired_metadata_preserved(
    before: UpgradeExpiredRecoveryDataSnapshot,
    after: UpgradeExpiredRecoveryDataSnapshot,
    intent: UpgradeExpiredRecoveryIntent,
) -> None:
    """This comparison is never qualification within an expired execution epoch."""
    verify_data_preserved(before, before, intent)
    if (
        (after.recovery_id, after.original_job_id, after.original_generation, after.source_sha256)
        != (
            before.recovery_id,
            before.original_job_id,
            before.original_generation,
            before.source_sha256,
        )
        or after.table_sha256s != before.table_sha256s
        or after.row_counts != before.row_counts
        or after.observed_at < intent.deadline_at
    ):
        raise UpgradeUnavailable("Expired recovery metadata cannot prove unchanged user data")
