"""Always-on, fixed-label feedback/export health independent of diagnostics."""

from datetime import UTC, datetime

from opentelemetry import metrics
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import PreferenceExportRow
from coire_api.feedback.retention import record_purge_age

meter = metrics.get_meter("coire.scheduler.feedback")
export_states = meter.create_gauge("coire_feedback_exports", unit="")
export_overdue = meter.create_gauge("coire_feedback_export_overdue", unit="")
export_cleanup_pending = meter.create_gauge("coire_feedback_export_cleanup_pending", unit="")
snapshot_timestamp = meter.create_gauge("coire_feedback_snapshot_timestamp", unit="s")
STATES = ("queued", "staging", "publishing", "succeeded", "failed", "cancelled")


async def record_feedback_metrics(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    rows = await session.execute(
        select(PreferenceExportRow.state, func.count()).group_by(PreferenceExportRow.state)
    )
    counts = dict(rows.tuples().all())
    overdue = await session.scalar(
        select(func.count())
        .select_from(PreferenceExportRow)
        .where(
            or_(
                and_(
                    PreferenceExportRow.state == "queued",
                    PreferenceExportRow.queue_deadline_at < now,
                ),
                and_(
                    PreferenceExportRow.state.in_(["staging", "publishing"]),
                    PreferenceExportRow.execution_deadline_at < now,
                ),
            )
        )
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(PreferenceExportRow)
        .where(PreferenceExportRow.cleanup_pending.is_(True))
    )
    await record_purge_age(session)
    for state in STATES:
        export_states.set(int(counts.get(state, 0)), {"state": state})
    export_overdue.set(int(overdue or 0))
    export_cleanup_pending.set(int(pending or 0))
    snapshot_timestamp.set(now.timestamp())
