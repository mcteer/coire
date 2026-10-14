"""Transactional upgrade authority, single ownership and idempotent evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    UpgradeHandoverRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UpgradeReceiptRow,
    UpgradeReleaseRow,
)
from coire_api.training.service import training_id
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.releases import changed_cohort
from coire_api.upgrades.telemetry import observed, receipt_span
from coire_core.errors import UpgradeConflict, UpgradeNotFound
from coire_core.models.upgrades import (
    UpgradeAction,
    UpgradeOperation,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeRelease,
    UpgradeStartRequest,
    UpgradeTiming,
    release_pair_compatible,
)


async def operation_lock(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(190034190034)"))


async def get_release(session: AsyncSession, identity: UUID) -> UpgradeRelease:
    row = await session.get(UpgradeReleaseRow, identity)
    if row is None:
        raise UpgradeNotFound()
    return UpgradeRelease.model_validate(row.payload)


async def register_release(
    session: AsyncSession, principal: Principal, release: UpgradeRelease
) -> UpgradeRelease:
    await operation_lock(session)
    owner = await authorize(session, principal)
    if release.prepared_by != owner:
        raise UpgradeConflict("Prepared release actor differs")
    existing = await session.scalar(
        select(UpgradeReleaseRow).where(
            UpgradeReleaseRow.manifest_sha256 == release.manifest_sha256
        )
    )
    if existing:
        return UpgradeRelease.model_validate(existing.payload)
    if await session.scalar(
        select(UpgradeReleaseRow).where(
            UpgradeReleaseRow.prepared_artifact_id == release.prepared_artifact_id
        )
    ):
        raise UpgradeConflict("Prepared artifact identity is already registered")
    session.add(
        UpgradeReleaseRow(
            id=release.release_id,
            manifest_sha256=release.manifest_sha256,
            prepared_artifact_id=release.prepared_artifact_id,
            prepared_by=owner,
            payload=release.model_dump(mode="json"),
        )
    )
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.release.register",
        target_type="upgrade_release",
        target_id=str(release.release_id),
        detail={"manifest_sha256": release.manifest_sha256},
    )
    await session.flush()
    return release


async def accept(
    session: AsyncSession,
    principal: Principal,
    request: UpgradeStartRequest,
    source_id: UUID,
    *,
    node: str | None = None,
    timing: UpgradeTiming | None = None,
    action: UpgradeAction = UpgradeAction.UPGRADE,
    parent_job_id: str | None = None,
) -> tuple[UpgradeOperation, bool]:
    await operation_lock(session)
    owner = await authorize(session, principal)
    existing = await session.scalar(
        select(UpgradeOperationRow).where(
            UpgradeOperationRow.user_id == owner,
            UpgradeOperationRow.idempotency_key == request.idempotency_key,
        )
    )
    request_hash = request.sha256()
    if existing:
        op = UpgradeOperation.model_validate(existing.payload)
        if (
            op.request_sha256 != request_hash
            or op.node != node
            or op.action is not action
            or op.parent_job_id != parent_job_id
        ):
            raise UpgradeConflict("Idempotency key belongs to different upgrade intent")
        return op, False
    if await session.scalar(
        select(UpgradeHandoverRow.id).where(UpgradeHandoverRow.active_slot == 1)
    ):
        raise UpgradeConflict("Executor handover retains activation ownership")
    if await session.scalar(
        select(UpgradeOperationRow.id).where(UpgradeOperationRow.active_slot == 1)
    ):
        raise UpgradeConflict("Another cluster operation retains activation ownership")
    if await session.scalar(
        select(UpgradeMaintenanceRow.target)
        .where(UpgradeMaintenanceRow.released.is_(False))
        .limit(1)
    ):
        raise UpgradeConflict("A prior operation retains unreleased maintenance")
    source, target = (
        await get_release(session, source_id),
        await get_release(session, request.release_id),
    )
    if (
        source.manifest_sha256 != request.expected_source_sha256
        or source.manifest.kind is not target.manifest.kind
        or not release_pair_compatible(source, target, action, parent_job_id)
        or target.status != "prepared"
    ):
        raise UpgradeConflict("Source release or compatibility differs")
    if action is UpgradeAction.ROLLBACK:
        assert parent_job_id is not None
        parent = UpgradeOperation.model_validate(
            (await get_operation(session, parent_job_id)).payload
        )
        if (
            parent.phase is not UpgradePhase.SUCCEEDED
            or parent.node != node
            or parent.kind is not source.manifest.kind
            or (parent.source_release_id, parent.target_release_id)
            != (target.release_id, source.release_id)
            or (parent.source_sha256, parent.target_sha256)
            != (target.manifest_sha256, source.manifest_sha256)
        ):
            raise UpgradeConflict("Rollback does not reverse the successful retained operation")
    generation = int(await session.scalar(text("SELECT nextval('upgrade_generation_seq')")) or 0)
    now = datetime.now(UTC)
    op = UpgradeOperation.model_validate(
        {
            "job_id": training_id(),
            "action": action,
            "kind": target.manifest.kind,
            "node": node,
            "source_release_id": source.release_id,
            "target_release_id": target.release_id,
            "source_sha256": source.manifest_sha256,
            "target_sha256": target.manifest_sha256,
            "user_id": owner,
            "key_id": principal.api_key_id,
            "idempotency_key": request.idempotency_key,
            "request_sha256": request_hash,
            "generation": generation,
            "cohort": changed_cohort(source.manifest, target.manifest),
            "timing": timing or UpgradeTiming(),
            "accepted_at": now,
            "updated_at": now,
            "parent_job_id": parent_job_id,
        }
    )
    session.add(
        UpgradeOperationRow(
            id=op.job_id,
            user_id=owner,
            key_id=principal.api_key_id,
            credential_version=principal.credential_version,
            source_release_id=source.release_id,
            target_release_id=target.release_id,
            idempotency_key=op.idempotency_key,
            request_sha256=request_hash,
            generation=generation,
            active_slot=1,
            phase=op.phase.value,
            payload=op.model_dump(mode="json"),
        )
    )
    await write_principal_audit(
        session,
        principal=principal,
        action=f"upgrades.{action.value}.accept",
        target_type="upgrade",
        target_id=op.job_id,
        detail={
            "source_sha256": op.source_sha256,
            "target_sha256": op.target_sha256,
            "generation": generation,
            "node": node,
            "kind": op.kind.value,
        },
    )
    await session.flush()
    return op, True


async def get_operation(
    session: AsyncSession, job_id: str, *, lock: bool = False
) -> UpgradeOperationRow:
    row = await session.get(
        UpgradeOperationRow, job_id, populate_existing=True, with_for_update=True if lock else None
    )
    if row is None:
        raise UpgradeNotFound()
    return row


async def append_receipt(
    session: AsyncSession, receipt: UpgradeReceipt, principal: Principal
) -> bool:
    with receipt_span(receipt):
        return await _append_receipt(session, receipt, principal)


async def _append_receipt(
    session: AsyncSession, receipt: UpgradeReceipt, principal: Principal
) -> bool:
    row = await get_operation(session, receipt.job_id, lock=True)
    op = UpgradeOperation.model_validate(row.payload)
    if op.generation != receipt.generation:
        raise UpgradeConflict("Receipt owner is stale")
    identity = (receipt.job_id, receipt.generation, receipt.sequence)
    existing = await session.get(UpgradeReceiptRow, identity)
    if existing:
        if existing.receipt_sha256 != receipt.sha256():
            raise UpgradeConflict("Receipt sequence has conflicting evidence")
        observed(op, receipt, replay=True)
        return False
    if receipt.sequence != op.receipt_sequence + 1:
        raise UpgradeConflict("Receipt sequence is not contiguous")
    session.add(
        UpgradeReceiptRow(
            job_id=receipt.job_id,
            generation=receipt.generation,
            sequence=receipt.sequence,
            receipt_sha256=receipt.sha256(),
            payload=receipt.model_dump(mode="json"),
        )
    )
    row.payload = op.model_copy(
        update={"receipt_sequence": receipt.sequence, "updated_at": datetime.now(UTC)}
    ).model_dump(mode="json")
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.stage.receipt",
        target_type="upgrade",
        target_id=op.job_id,
        detail={
            "phase": receipt.phase.value,
            "outcome": receipt.outcome,
            "sequence": receipt.sequence,
            "generation": receipt.generation,
            "source_sha256": op.source_sha256,
            "target_sha256": op.target_sha256,
            "restoration": receipt.restoration,
            "receipt_sha256": receipt.sha256(),
        },
    )
    await session.flush()
    observed(op, receipt, replay=False)
    return True
