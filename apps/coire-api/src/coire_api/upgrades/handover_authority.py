"""Live admin acceptance of a fixed executor handover, separate from application activation."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    UpgradeExecutorRow,
    UpgradeHandoverRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
)
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.handover_host_hold import (
    publish_handover_hold,
    reconcile_orphan_handover_hold,
)
from coire_api.upgrades.persistence import get_release, operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import (
    CoreBootstrapHeartbeat,
    CoreBootstrapSeed,
    CoreHandoverPreparation,
    CoreHandoverStartRequest,
    CoreHandoverSummary,
    UpgradeClaimIdle,
    UpgradeComposeService,
    UpgradeReleaseRegister,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import read_prepared_compose, verify_prepared_release

tracer = trace.get_tracer("coire.api.upgrades")


@tracer.start_as_current_span(
    "coire.api.upgrade_handover_accept", record_exception=False, set_status_on_exception=False
)
async def accept_handover(
    session: AsyncSession,
    principal: Principal,
    preparation: CoreHandoverPreparation,
    settings: Settings,
) -> tuple[CoreHandoverPreparation, bool]:
    """Persist initiation only; this receipt cannot start or transfer an executor."""
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    await operation_lock(session)
    owner = await authorize(session, principal)
    if preparation.initiator_user_id != owner:
        raise UpgradeForbidden("Executor handover actor differs from authenticated initiator")
    if preparation.incumbent.source.manifest.project != settings.upgrade_project:
        raise UpgradeConflict("Executor handover project differs")
    previous = await session.scalar(
        select(UpgradeHandoverRow).where(
            UpgradeHandoverRow.user_id == owner,
            UpgradeHandoverRow.idempotency_key == preparation.idempotency_key,
        )
    )
    if previous is not None:
        retained = CoreHandoverPreparation.model_validate(previous.preparation_payload)
        if retained != preparation or previous.preparation_sha256 != preparation.sha256():
            raise UpgradeConflict("Idempotency key belongs to another executor handover")
        return retained, False
    if await session.scalar(
        select(UpgradeHandoverRow.id).where(UpgradeHandoverRow.active_slot == 1)
    ):
        raise UpgradeConflict("Another executor handover retains activation ownership")
    if await session.scalar(
        select(UpgradeOperationRow.id).where(UpgradeOperationRow.active_slot == 1)
    ):
        raise UpgradeConflict("Application upgrade retains activation ownership")
    if await session.scalar(
        select(UpgradeMaintenanceRow.target)
        .where(UpgradeMaintenanceRow.released.is_(False))
        .limit(1)
    ):
        raise UpgradeConflict("Application upgrade retains maintenance ownership")
    now = datetime.now(UTC)
    if (
        not 0 <= (now - preparation.prepared_at).total_seconds() <= 5
        or now >= preparation.deadline_at
    ):
        raise UpgradeUnavailable("Executor handover preparation is not freshly captured")
    executor = await session.get(UpgradeExecutorRow, settings.upgrade_project, with_for_update=True)
    if executor is None or not executor.bootstrap_ready or executor.bootstrap_payload is None:
        raise UpgradeUnavailable("Executor handover has no qualified retained incumbent")
    heartbeat = CoreBootstrapHeartbeat.model_validate(executor.bootstrap_payload)
    if (
        executor.identity != settings.upgrade_executor_id
        or heartbeat.executor_id != executor.identity
        or heartbeat.seed != preparation.incumbent
        or executor.image_id != heartbeat.seed.executor_image_id
        or executor.generation != heartbeat.ownership.generation
        or preparation.generation != executor.generation + 1
        or executor.heartbeat_at != heartbeat.physical.observed_at
        or not 0 <= (now - executor.heartbeat_at).total_seconds() <= 5
    ):
        raise UpgradeConflict("Executor handover incumbent identity or qualification differs")
    for release in (
        preparation.incumbent.source,
        preparation.incumbent.executor_source,
        preparation.candidate_release,
    ):
        if await get_release(session, release.release_id) != release:
            raise UpgradeConflict("Executor handover registered provenance differs")
    row = UpgradeHandoverRow(
        id=uuid4(),
        project=settings.upgrade_project,
        user_id=owner,
        key_id=principal.api_key_id,
        credential_version=principal.credential_version,
        idempotency_key=preparation.idempotency_key,
        preparation_sha256=preparation.sha256(),
        generation=preparation.generation,
        active_slot=1,
        phase="accepted",
        preparation_payload=preparation.model_dump(mode="json"),
    )
    session.add(row)
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.handover.accepted",
        target_type="upgrade_handover",
        target_id=str(row.id),
        context={"generation": preparation.generation, "preparation_sha256": preparation.sha256()},
    )
    await session.flush()
    return preparation, True


def handover_summary(row: UpgradeHandoverRow) -> CoreHandoverSummary:
    preparation = CoreHandoverPreparation.model_validate(row.preparation_payload)
    return CoreHandoverSummary.model_validate(
        {
            "handover_id": row.id,
            "project": row.project,
            "generation": row.generation,
            "phase": row.phase,
            "preparation_sha256": row.preparation_sha256,
            "candidate_release_id": preparation.candidate_release.release_id,
            "incumbent_image_id": preparation.incumbent.executor_image_id,
            "candidate_image_id": preparation.candidate_image_id,
            "prepared_at": preparation.prepared_at,
            "deadline_at": preparation.deadline_at,
        }
    )


async def start_handover(
    session: AsyncSession,
    principal: Principal,
    request: CoreHandoverStartRequest,
    settings: Settings,
) -> tuple[CoreHandoverSummary, bool]:
    """Resolve only sealed registered content and generate runtime identity on the server."""
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    await operation_lock(session)
    owner = await authorize(session, principal)
    previous = await session.scalar(
        select(UpgradeHandoverRow).where(
            UpgradeHandoverRow.user_id == owner,
            UpgradeHandoverRow.idempotency_key == request.idempotency_key,
        )
    )
    if previous is not None:
        retained = CoreHandoverPreparation.model_validate(previous.preparation_payload)
        if (
            retained.candidate_release.release_id != request.release_id
            or retained.incumbent.sha256() != request.expected_incumbent_seed_sha256
        ):
            raise UpgradeConflict("Idempotency key belongs to another executor handover")
        return handover_summary(previous), False
    await reconcile_orphan_handover_hold(session, principal, settings)
    executor = await session.get(UpgradeExecutorRow, settings.upgrade_project, with_for_update=True)
    if executor is None or executor.bootstrap_payload is None:
        raise UpgradeUnavailable("Executor handover has no retained bootstrap")
    heartbeat = CoreBootstrapHeartbeat.model_validate(executor.bootstrap_payload)
    if heartbeat.seed.sha256() != request.expected_incumbent_seed_sha256:
        raise UpgradeConflict("Expected incumbent executor differs")
    candidate = await get_release(session, request.release_id)
    verified = await asyncio.to_thread(
        verify_prepared_release,
        Path(settings.upgrade_prepared_dir),
        UpgradeReleaseRegister(
            prepared_artifact_id=candidate.prepared_artifact_id,
            expected_sha256=candidate.manifest_sha256,
        ),
    )
    if verified != candidate:
        raise UpgradeConflict("Executor candidate sealed content differs")
    document = await asyncio.to_thread(
        read_prepared_compose, Path(settings.upgrade_prepared_dir), candidate
    )
    services = document.get("services")
    raw = services.get("coire-upgrade-executor") if isinstance(services, dict) else None
    try:
        role = UpgradeComposeService.model_validate(raw)
    except ValueError:
        raise UpgradeConflict("Prepared executor role is absent or unsupported") from None
    now = datetime.now(UTC)
    try:
        preparation = CoreHandoverPreparation(
            incumbent=heartbeat.seed,
            candidate_release=candidate,
            candidate_runtime_id=f"executor-{uuid4().hex}",
            candidate_image_id=role.image,
            generation=executor.generation + 1,
            initiator_user_id=owner,
            idempotency_key=request.idempotency_key,
            prepared_at=now,
            deadline_at=now + timedelta(seconds=120),
        )
    except ValueError:
        raise UpgradeConflict("Prepared executor handover compatibility differs") from None
    await accept_handover(session, principal, preparation, settings)
    await asyncio.to_thread(publish_handover_hold, settings, preparation)
    row = await session.scalar(
        select(UpgradeHandoverRow).where(
            UpgradeHandoverRow.user_id == owner,
            UpgradeHandoverRow.idempotency_key == request.idempotency_key,
        )
    )
    assert row is not None
    return handover_summary(row), True


async def claim_handover(
    session: AsyncSession,
    principal: Principal,
    seed: CoreBootstrapSeed,
    settings: Settings,
) -> CoreHandoverPreparation | UpgradeClaimIdle:
    """Authorize only fixed passive preparation; writer transfer remains a separate boundary."""
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if (
        principal.kind is not PrincipalKind.UPGRADE_EXECUTOR
        or principal.subject != settings.upgrade_executor_id
        or principal.scopes != frozenset({"upgrades:execute"})
        or seed.source.manifest.project != settings.upgrade_project
    ):
        raise UpgradeForbidden("Executor handover claim is outside restricted project authority")
    await operation_lock(session)
    row = await session.scalar(
        select(UpgradeHandoverRow)
        .where(
            UpgradeHandoverRow.active_slot == 1,
            UpgradeHandoverRow.project == settings.upgrade_project,
        )
        .with_for_update()
    )
    if row is None:
        return UpgradeClaimIdle()
    preparation = CoreHandoverPreparation.model_validate(row.preparation_payload)
    if (
        row.preparation_sha256 != preparation.sha256()
        or preparation.incumbent != seed
        or row.generation != preparation.generation
        or row.phase not in {"accepted", "passive"}
    ):
        raise UpgradeConflict("Executor handover passive claim differs from accepted authority")
    now = datetime.now(UTC)
    if not preparation.prepared_at <= now < preparation.deadline_at:
        raise UpgradeUnavailable("Executor handover captured preparation deadline elapsed")
    executor = await session.get(UpgradeExecutorRow, settings.upgrade_project)
    if executor is None or not executor.bootstrap_ready or executor.bootstrap_payload is None:
        raise UpgradeUnavailable("Executor handover incumbent qualification is absent")
    heartbeat = CoreBootstrapHeartbeat.model_validate(executor.bootstrap_payload)
    if (
        heartbeat.seed != seed
        or executor.identity != principal.subject
        or not 0 <= (now - executor.heartbeat_at).total_seconds() <= 5
    ):
        raise UpgradeConflict("Executor handover incumbent qualification changed")
    actor = Principal(
        kind=PrincipalKind.API_KEY if row.key_id is not None else PrincipalKind.ADMIN,
        user_id=row.user_id,
        role=UserRole.ADMIN,
        scopes=frozenset({"admin"}),
        api_key_id=row.key_id,
        credential_version=row.credential_version,
    )
    await authorize(session, actor)
    if row.phase == "accepted":
        row.phase = "passive"
        await write_principal_audit(
            session,
            principal=principal,
            action="upgrades.handover.passive_authorized",
            target_type="upgrade_handover",
            target_id=str(row.id),
            context={"generation": row.generation, "preparation_sha256": preparation.sha256()},
        )
        await session.flush()
    return preparation


@tracer.start_as_current_span(
    "coire.api.upgrade_handover_recovery_claim",
    record_exception=False,
    set_status_on_exception=False,
)
async def recovery_claim_handover(
    session: AsyncSession, principal: Principal, seed: CoreBootstrapSeed, settings: Settings
) -> CoreHandoverPreparation | UpgradeClaimIdle:
    """Read original no-transfer metadata for Source cleanup after revocation/expiry.

    This grants no passive creation, start, transfer or renewed preparation epoch.
    The executor must prohibit candidate effects before acknowledging unchanged.
    """
    from coire_api.upgrades.core_targets import verify_current_writer
    from coire_core.models.upgrades import DeploymentHandoverHold
    from coire_core.upgrade_journal import PrivateUpgradeStore

    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if (
        principal.kind is not PrincipalKind.UPGRADE_EXECUTOR
        or principal.subject != settings.upgrade_executor_id
        or principal.scopes != frozenset({"upgrades:execute"})
        or seed.source.manifest.project != settings.upgrade_project
    ):
        raise UpgradeForbidden("Recovery metadata is outside fixed executor scope")
    await operation_lock(session)
    row = await session.scalar(
        select(UpgradeHandoverRow)
        .where(
            UpgradeHandoverRow.active_slot == 1,
            UpgradeHandoverRow.project == settings.upgrade_project,
        )
        .with_for_update()
    )
    if row is None:
        return UpgradeClaimIdle()
    preparation = CoreHandoverPreparation.model_validate(row.preparation_payload)
    if (
        row.preparation_sha256 != preparation.sha256()
        or row.generation != preparation.generation
        or row.user_id != preparation.initiator_user_id
        or row.idempotency_key != preparation.idempotency_key
        or row.phase not in {"accepted", "passive"}
        or preparation.incumbent != seed
        or row.transfer_payload is not None
        or row.commit_payload is not None
    ):
        raise UpgradeConflict("Recovery metadata differs from pending no-transfer authority")
    executor = await session.get(UpgradeExecutorRow, settings.upgrade_project)
    if executor is None or not executor.bootstrap_ready or executor.bootstrap_payload is None:
        raise UpgradeUnavailable("Recovery metadata has no fresh qualified Source")
    heartbeat = CoreBootstrapHeartbeat.model_validate(executor.bootstrap_payload)
    now = datetime.now(UTC)
    if (
        heartbeat.seed != seed
        or executor.identity != principal.subject
        or executor.generation != heartbeat.ownership.generation
        or executor.image_id != seed.executor_image_id
        or executor.source_release_id != seed.source.release_id
        or executor.heartbeat_at != heartbeat.physical.observed_at
        or heartbeat.ownership.generation < preparation.generation - 1
        or not 0 <= (now - executor.heartbeat_at).total_seconds() <= 5
    ):
        raise UpgradeConflict("Recovery metadata Source qualification changed")
    await asyncio.to_thread(verify_current_writer, settings, heartbeat)
    hold = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir)).read(
        "handover-hold.json", DeploymentHandoverHold
    )
    if (
        hold is None
        or hold.released
        or (
            hold.project,
            hold.preparation_sha256,
            hold.generation,
            hold.incumbent_runtime_id,
            hold.incumbent_container_id,
            hold.candidate_runtime_id,
        )
        != (
            settings.upgrade_project,
            preparation.sha256(),
            preparation.generation,
            seed.executor_runtime_id,
            seed.executor_container_id,
            preparation.candidate_runtime_id,
        )
    ):
        raise UpgradeConflict("Recovery metadata has no exact retained admission hold")
    return preparation
