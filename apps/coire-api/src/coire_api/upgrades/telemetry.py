"""Bounded upgrade diagnostics accompany, and never replace, durable audit evidence."""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics, trace
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeExecutorRow, UpgradeMaintenanceRow, UpgradeOperationRow
from coire_api.upgrades.handover_telemetry import collect_handover_health
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    DeploymentOperationHold,
    UpgradeKind,
    UpgradeOperation,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.upgrades")
meter = metrics.get_meter("coire.api.upgrades")
receipt_attempts = meter.create_counter(
    "coire_upgrade_receipt_ingest_attempts_total",
    description="Validated receipt ingestion attempts, including replay",
)
phase_observations = meter.create_counter(
    "coire_upgrade_phase_observations_total",
    description="Persisted upgrade phase evidence observed before transaction commit",
)
failure_observations = meter.create_counter(
    "coire_upgrade_failure_observations_total",
    description="Non-replayed failed receipt observations before transaction commit",
)
operation_duration = meter.create_histogram(
    "coire_upgrade_operation_duration_seconds",
    unit="s",
    description="Captured acceptance-to-terminal receipt duration observed before transaction commit",
)
snapshot_timestamp = meter.create_gauge("coire_upgrade_snapshot_timestamp_seconds", unit="s")
terminal_host_hold = meter.create_gauge("coire_upgrade_terminal_host_hold", unit="")
unclaimed_cancellation_pending = meter.create_gauge(
    "coire_upgrade_unclaimed_cancellation_pending", unit=""
)
enabled = meter.create_gauge("coire_upgrades_enabled", unit="")
active = meter.create_gauge("coire_upgrade_operations", unit="")
phase_age = meter.create_gauge("coire_upgrade_phase_age_seconds", unit="s")
recovery_required = meter.create_gauge("coire_upgrade_recovery_required", unit="")
maintenance_age = meter.create_gauge("coire_upgrade_maintenance_age_seconds", unit="s")
recovery_age = meter.create_gauge("coire_upgrade_recovery_age_seconds", unit="s")
executor_ready = meter.create_gauge("coire_upgrade_executor_ready", unit="")
executor_heartbeat_age = meter.create_gauge(
    "coire_upgrade_executor_heartbeat_age_seconds", unit="s"
)


async def collect_health(session: AsyncSession, settings: Settings) -> None:
    """Read durable obligations; identifiers and evidence never become metric labels."""
    now = datetime.now(UTC)
    from coire_core.upgrade_journal import PrivateUpgradeStore

    def read_host_hold() -> DeploymentOperationHold | None:
        root = Path(settings.upgrade_coordination_dir)
        return (
            PrivateUpgradeStore(root, create=False).read(
                "operation-hold.json", DeploymentOperationHold
            )
            if root.exists()
            else None
        )

    held = await asyncio.to_thread(read_host_hold)
    terminal = (
        await session.get(UpgradeOperationRow, held.job_id)
        if held is not None and not held.released
        else None
    )
    terminal_host_hold.set(
        int(
            terminal is not None
            and terminal.active_slot is None
            and UpgradePhase(terminal.phase) in TERMINAL_UPGRADE_PHASES
        )
    )
    enabled.set(int(settings.upgrades_enabled))
    rows = (
        await session.scalars(
            select(UpgradeOperationRow)
            .where(
                or_(
                    UpgradeOperationRow.active_slot == 1,
                    exists(
                        select(UpgradeMaintenanceRow.target).where(
                            UpgradeMaintenanceRow.job_id == UpgradeOperationRow.id,
                            UpgradeMaintenanceRow.released.is_(False),
                        )
                    ),
                )
            )
            .limit(17)
        )
    ).all()
    if len(rows) > 16:
        raise ValueError("Upgrade telemetry obligation inventory exceeds its bound")
    operations = [UpgradeOperation.model_validate(row.payload) for row in rows]
    unclaimed_cancellation_pending.set(
        sum(
            operation.phase is UpgradePhase.ACCEPTED
            and operation.cancel_requested
            and operation.receipt_sequence == 0
            for operation in operations
        )
    )
    for kind in UpgradeKind:
        selected = [operation for operation in operations if operation.kind is kind]
        labels = {"kind": kind.value}
        active.set(len(selected), labels)
        maintenance_age.set(
            max(
                (max(0, (now - operation.accepted_at).total_seconds()) for operation in selected),
                default=0,
            ),
            labels,
        )
        recovery_age.set(
            max(
                (
                    max(0, (now - operation.updated_at).total_seconds())
                    for operation in selected
                    if operation.phase is UpgradePhase.RECOVERY_REQUIRED
                ),
                default=0,
            ),
            labels,
        )
        phase_age.set(
            max(
                (max(0, (now - operation.updated_at).total_seconds()) for operation in selected),
                default=0,
            ),
            labels,
        )
        recovery_required.set(
            sum(operation.phase is UpgradePhase.RECOVERY_REQUIRED for operation in selected), labels
        )
    owner = await session.get(UpgradeExecutorRow, settings.upgrade_project)
    age = (now - owner.heartbeat_at).total_seconds() if owner is not None else None
    executor_ready.set(
        int(
            owner is not None
            and owner.identity == settings.upgrade_executor_id
            and owner.bootstrap_ready
            and age is not None
            and 0 <= age <= 10
        )
    )
    executor_heartbeat_age.set(max(0, age) if age is not None else 0)
    await collect_handover_health(session, settings, now)
    snapshot_timestamp.set(now.timestamp())


@contextmanager
def receipt_span(receipt: UpgradeReceipt) -> Iterator[None]:
    with tracer.start_as_current_span("coire.api.upgrade_receipt") as span:
        span.set_attribute("coire.job_id", receipt.job_id)
        span.set_attribute("coire.upgrade_generation", receipt.generation)
        span.set_attribute("coire.upgrade_sequence", receipt.sequence)
        span.set_attribute("coire.upgrade_phase", receipt.phase.value)
        yield


def observed(operation: UpgradeOperation, receipt: UpgradeReceipt, *, replay: bool) -> None:
    labels = {
        "kind": operation.kind.value,
        "phase": receipt.phase.value,
        "outcome": receipt.outcome,
    }
    receipt_attempts.add(1, {**labels, "replay": str(replay).lower()})
    if not replay:
        phase_observations.add(1, labels)
        if receipt.outcome == "failed":
            failure_observations.add(1, labels)
        if receipt.phase in TERMINAL_UPGRADE_PHASES and receipt.outcome == "complete":
            operation_duration.record(
                max(0, (receipt.occurred_at - operation.accepted_at).total_seconds()),
                {"kind": operation.kind.value, "phase": receipt.phase.value},
            )
        logger.info(
            "upgrade receipt staged",
            extra={
                "job_id": operation.job_id,
                "user_id": str(operation.user_id),
                "generation": operation.generation,
                "sequence": receipt.sequence,
                "phase": receipt.phase.value,
                "outcome": receipt.outcome,
                "restoration": receipt.restoration,
                "safe_reason": receipt.reason,
            },
        )
