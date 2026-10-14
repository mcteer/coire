"""Bounded handover obligations use captured deadlines, independent of diagnostics export."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from opentelemetry import metrics
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeHandoverRow
from coire_core.models.upgrades import (
    BootstrapHandover,
    BootstrapStabilityObservation,
    CoreHandoverCommitIntent,
    CoreHandoverPreparation,
    CoreHandoverRecoveryIntent,
    CoreHandoverTransferIntent,
    DeploymentHandoverHold,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore

meter = metrics.get_meter("coire.api.upgrades")
pending = meter.create_gauge("coire_upgrade_handover_pending", unit="")
deadline_overdue = meter.create_gauge("coire_upgrade_handover_deadline_overdue_seconds", unit="s")
phase_age = meter.create_gauge("coire_upgrade_handover_phase_age_seconds", unit="s")
BOUNDARIES = ("passive", "transfer", "stability", "commit", "restoration")


def captured_deadline(
    row: UpgradeHandoverRow,
    settings: Settings,
) -> tuple[str, datetime]:
    prep = CoreHandoverPreparation.model_validate(row.preparation_payload)
    if (
        row.project != settings.upgrade_project
        or row.preparation_sha256 != prep.sha256()
        or row.generation != prep.generation
    ):
        raise ValueError("Handover telemetry accepted preparation differs")
    boundary, deadline = "passive", prep.deadline_at
    if row.transfer_payload is not None:
        transfer = CoreHandoverTransferIntent.model_validate(row.transfer_payload)
        if (
            transfer.preparation_sha256 != prep.sha256()
            or transfer.generation != prep.generation
            or transfer.handover_id != row.id
        ):
            raise ValueError("Handover telemetry transfer differs")
        boundary, deadline = "transfer", transfer.deadline_at
    if row.commit_payload is not None:
        decision = CoreHandoverCommitIntent.model_validate(row.commit_payload)
        if (
            decision.preparation_sha256 != prep.sha256()
            or decision.generation != prep.generation
            or decision.handover_id != row.id
        ):
            raise ValueError("Handover telemetry commit differs")
        boundary, deadline = "commit", decision.decided_at + timedelta(seconds=30)
    root = Path(settings.upgrade_state_dir) / "bootstrap"
    if root.exists():
        bootstrap = PrivateUpgradeStore(root)
        current = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
        if (
            bootstrap.read("handover-preparation.json", CoreHandoverPreparation) == prep
            and current is not None
            and current.generation == prep.generation
        ):
            if row.commit_payload is None and current.phase in {"claimed", "stable"}:
                boundary = "stability" if current.phase == "claimed" else "commit"
                if current.phase == "claimed":
                    # The captured 120 seconds are a minimum stability period.
                    # Final observation must occur within the existing maximum
                    # ten-second physical-probe gap; neither bound is renewed.
                    proof = bootstrap.read(
                        "stability-observation.json", BootstrapStabilityObservation
                    )
                    baseline = current.stability_started_at
                    if baseline is None:
                        raise ValueError(
                            "Claimed handover telemetry has no captured stability start"
                        )
                    if proof is not None:
                        if (
                            proof.generation,
                            proof.incumbent_container_id,
                            proof.candidate_container_id,
                        ) != (
                            current.generation,
                            current.incumbent_container_id,
                            current.candidate_container_id,
                        ):
                            raise ValueError("Handover telemetry probe belongs to another epoch")
                        baseline = proof.observed_at
                    deadline = min(current.deadline_at, baseline) + timedelta(seconds=10)
                else:
                    deadline = current.updated_at + timedelta(seconds=30)
            journal_root = root / "handover" / str(prep.generation)
            if current.phase in {"restoring", "restored"} and journal_root.exists():
                recovery = PrivateUpgradeStore(journal_root).read(
                    "recovery-intent.json", CoreHandoverRecoveryIntent
                )
                if recovery is not None:
                    if (
                        recovery.preparation_sha256 != prep.sha256()
                        or recovery.generation != prep.generation
                        or recovery.incumbent_container_id != prep.incumbent.executor_container_id
                    ):
                        raise ValueError("Handover telemetry restoration differs")
                    boundary, deadline = "restoration", recovery.deadline_at
    return boundary, deadline


def retained_hold(settings: Settings) -> DeploymentHandoverHold | None:
    root = Path(settings.upgrade_coordination_dir)
    return (
        PrivateUpgradeStore(root).read("handover-hold.json", DeploymentHandoverHold)
        if root.exists()
        else None
    )


async def collect_handover_health(
    session: AsyncSession,
    settings: Settings,
    now: datetime,
) -> None:
    hold = await asyncio.to_thread(retained_hold, settings)
    predicate = UpgradeHandoverRow.active_slot == 1
    if hold is not None and not hold.released:
        if hold.project != settings.upgrade_project:
            raise ValueError("Handover telemetry hold belongs to another project")
        predicate = or_(predicate, UpgradeHandoverRow.preparation_sha256 == hold.preparation_sha256)
    rows = (
        await session.scalars(
            select(UpgradeHandoverRow)
            .where(
                UpgradeHandoverRow.project == settings.upgrade_project,
                predicate,
            )
            .limit(2)
        )
    ).all()
    if len(rows) > 1:
        raise ValueError("Handover telemetry has conflicting retained obligations")
    values = dict.fromkeys(BOUNDARIES, 0.0)
    age = 0.0
    if rows:
        row = rows[0]
        boundary, deadline = await asyncio.to_thread(captured_deadline, row, settings)
        values[boundary] = max(0.0, (now - deadline).total_seconds())
        age = max(0.0, (now - row.created_at).total_seconds())
    pending.set(len(rows))
    phase_age.set(age)
    for boundary, overdue in values.items():
        deadline_overdue.set(overdue, {"boundary": boundary})
