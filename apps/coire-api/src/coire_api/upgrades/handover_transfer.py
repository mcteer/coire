"""Atomically revalidate the initiating actor and persist one fixed writer-transfer intent."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import UpgradeExecutorRow, UpgradeHandoverRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.persistence import operation_lock
from coire_core.deploy import idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapHeartbeat,
    CoreBootstrapSeed,
    CoreHandoverCandidateStart,
    CoreHandoverCreation,
    CoreHandoverPreparation,
    CoreHandoverTransferIntent,
    CoreHandoverTransferRequest,
    DeploymentHandoverHold,
    DeploymentOwnership,
    ExecutorPassiveReadiness,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore

tracer = trace.get_tracer("coire.api.upgrades")


def verify_private_transfer(
    settings: Settings,
    preparation: CoreHandoverPreparation,
    request: CoreHandoverTransferRequest,
) -> tuple[CoreBootstrapSeed, ExecutorPassiveReadiness, DeploymentOwnership]:
    root = Path(settings.upgrade_state_dir) / "bootstrap"
    coordination_root = Path(settings.upgrade_coordination_dir)
    if not root.exists() or not coordination_root.exists():
        raise UpgradeUnavailable("Executor transfer retained authority is absent")
    store, coordination = PrivateUpgradeStore(root), PrivateUpgradeStore(coordination_root)
    if store.read("handover-preparation.json", CoreHandoverPreparation) != preparation:
        raise UpgradeConflict("Executor transfer preparation differs from retained intent")
    journal_root = root / "handover" / str(preparation.generation)
    if not journal_root.exists():
        raise UpgradeUnavailable("Executor transfer candidate journal is absent")
    journal = PrivateUpgradeStore(journal_root)
    if journal.read("preparation.json", CoreHandoverPreparation) != preparation:
        raise UpgradeConflict("Executor transfer candidate preparation differs")
    seed = journal.read("candidate-seed.json", CoreBootstrapSeed)
    creation = journal.read("creation.json", CoreHandoverCreation)
    started = journal.read("candidate-start.json", CoreHandoverCandidateStart)
    proof = journal.read("passive-proof.json", ExecutorPassiveReadiness)
    handover = store.read("bootstrap-handover.json", BootstrapHandover)
    hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
    if (
        seed is None
        or creation is None
        or started is None
        or proof is None
        or handover is None
        or hold is None
    ):
        raise UpgradeUnavailable("Executor transfer candidate protocol evidence is incomplete")
    if (
        seed.source != preparation.incumbent.source
        or seed.executor_source != preparation.candidate_release
        or seed.source_container_ids != preparation.incumbent.source_container_ids
        or seed.executor_runtime_id != preparation.candidate_runtime_id
        or seed.executor_image_id != preparation.candidate_image_id
        or seed.generation != preparation.generation
        or creation.preparation_sha256 != preparation.sha256()
        or creation.candidate_name != preparation.candidate_name
        or creation.candidate_image_id != seed.executor_image_id
        or creation.candidate_container_id != seed.executor_container_id
        or creation.generation != seed.generation
        or creation.outcome != "observed"
        or creation.observed_at != seed.prepared_at
        or started.preparation_sha256 != preparation.sha256()
        or started.candidate_seed_sha256 != seed.sha256()
        or started.candidate_container_id != seed.executor_container_id
        or started.generation != seed.generation
        or started.outcome != "observed"
        or started.deadline_at != preparation.deadline_at
        or started.started_at is None
        or not preparation.prepared_at <= started.started_at < preparation.deadline_at
        or proof.sha256() != request.passive_proof_sha256
        or proof.preparation_sha256 != preparation.sha256()
        or proof.candidate_seed_sha256 != seed.sha256()
        or proof.candidate_runtime_id != seed.executor_runtime_id
        or proof.candidate_container_id != seed.executor_container_id
        or proof.candidate_image_id != seed.executor_image_id
        or proof.generation != seed.generation
        or proof.incumbent_runtime_id != preparation.incumbent.executor_runtime_id
        or proof.incumbent_container_id != preparation.incumbent.executor_container_id
        or handover.phase != "verified"
        or handover.generation != seed.generation
        or handover.incumbent_id != preparation.incumbent.executor_runtime_id
        or handover.incumbent_container_id != preparation.incumbent.executor_container_id
        or handover.incumbent_image_id != preparation.incumbent.executor_image_id
        or handover.candidate_id != seed.executor_runtime_id
        or handover.candidate_container_id != seed.executor_container_id
        or handover.candidate_image_id != seed.executor_image_id
        or handover.deadline_at != preparation.deadline_at
        or hold.released
        or hold.project != settings.upgrade_project
        or hold.preparation_sha256 != preparation.sha256()
        or hold.generation != seed.generation
        or hold.incumbent_runtime_id != preparation.incumbent.executor_runtime_id
        or hold.incumbent_container_id != preparation.incumbent.executor_container_id
        or hold.candidate_runtime_id != seed.executor_runtime_id
    ):
        raise UpgradeConflict("Executor transfer authority differs from exact retained candidate")
    now = datetime.now(UTC)
    if (
        not preparation.prepared_at <= now < preparation.deadline_at
        or not 0 <= (now - proof.observed_at).total_seconds() <= 5
    ):
        raise UpgradeUnavailable(
            "Executor transfer original deadline or protocol freshness elapsed"
        )
    owner = idle_executor_owner(
        coordination, settings.upgrade_project, settings.upgrade_executor_id
    )
    if owner is None or (
        owner.executor_runtime_id,
        owner.executor_container_id,
        owner.source_sha256,
        owner.generation + 1,
    ) != (
        preparation.incumbent.executor_runtime_id,
        preparation.incumbent.executor_container_id,
        preparation.incumbent.source.manifest_sha256,
        preparation.generation,
    ):
        raise UpgradeConflict("Executor transfer has no exact retained incumbent writer")
    return seed, proof, owner


@tracer.start_as_current_span(
    "coire.api.upgrade_handover_transfer_consume",
    record_exception=False,
    set_status_on_exception=False,
)
async def consume_handover_transfer(
    session: AsyncSession,
    principal: Principal,
    request: CoreHandoverTransferRequest,
    settings: Settings,
) -> CoreHandoverTransferIntent:
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if (
        principal.kind is not PrincipalKind.UPGRADE_EXECUTOR
        or principal.subject != settings.upgrade_executor_id
        or principal.scopes != frozenset({"upgrades:execute"})
    ):
        raise UpgradeForbidden("Executor transfer is outside restricted service authority")
    await operation_lock(session)
    row = await session.scalar(
        select(UpgradeHandoverRow)
        .where(
            UpgradeHandoverRow.preparation_sha256 == request.preparation_sha256,
            UpgradeHandoverRow.project == settings.upgrade_project,
        )
        .with_for_update()
    )
    if row is None:
        raise UpgradeConflict("Executor transfer has no accepted handover")
    preparation = CoreHandoverPreparation.model_validate(row.preparation_payload)
    if (
        row.preparation_sha256 != preparation.sha256()
        or row.generation != preparation.generation
        or row.user_id != preparation.initiator_user_id
        or row.idempotency_key != preparation.idempotency_key
        or row.project != preparation.incumbent.source.manifest.project
    ):
        raise UpgradeConflict("Executor transfer accepted authority changed")
    if row.transfer_payload is not None:
        intent = CoreHandoverTransferIntent.model_validate(row.transfer_payload)
        if (
            intent.handover_id != row.id
            or intent.preparation_sha256 != preparation.sha256()
            or intent.passive_proof_sha256 != request.passive_proof_sha256
            or intent.generation != row.generation
        ):
            raise UpgradeConflict("Executor transfer replay belongs to another consumed intent")
        return intent
    if row.phase != "passive" or row.active_slot != 1:
        raise UpgradeConflict("Executor handover is not at its transfer boundary")
    seed, proof, owner = await asyncio.to_thread(
        verify_private_transfer, settings, preparation, request
    )
    retained = await session.get(UpgradeExecutorRow, settings.upgrade_project, with_for_update=True)
    if retained is None or not retained.bootstrap_ready or retained.bootstrap_payload is None:
        raise UpgradeUnavailable("Executor transfer incumbent qualification is absent")
    heartbeat = CoreBootstrapHeartbeat.model_validate(retained.bootstrap_payload)
    if (
        heartbeat.seed != preparation.incumbent
        or heartbeat.ownership != owner
        or retained.identity != principal.subject
        or retained.image_id != preparation.incumbent.executor_image_id
        or retained.generation != owner.generation
        or retained.heartbeat_at != heartbeat.physical.observed_at
        or not 0 <= (datetime.now(UTC) - retained.heartbeat_at).total_seconds() <= 5
    ):
        raise UpgradeConflict("Executor transfer fresh incumbent qualification changed")
    actor = Principal(
        kind=PrincipalKind.API_KEY if row.key_id is not None else PrincipalKind.ADMIN,
        user_id=row.user_id,
        role=UserRole.ADMIN,
        scopes=frozenset({"admin"}),
        api_key_id=row.key_id,
        credential_version=row.credential_version,
    )
    await authorize(session, actor)
    if await asyncio.to_thread(verify_private_transfer, settings, preparation, request) != (
        seed,
        proof,
        owner,
    ):
        raise UpgradeConflict("Executor transfer evidence changed during actor revalidation")
    consumed_at = datetime.now(UTC)
    if consumed_at >= preparation.deadline_at:
        raise UpgradeUnavailable("Executor transfer original deadline elapsed before consumption")
    intent = CoreHandoverTransferIntent(
        handover_id=row.id,
        intent_id=uuid4(),
        preparation_sha256=preparation.sha256(),
        candidate_seed_sha256=seed.sha256(),
        passive_proof_sha256=proof.sha256(),
        generation=row.generation,
        consumed_at=consumed_at,
        deadline_at=min(consumed_at + timedelta(seconds=30), preparation.deadline_at),
    )
    row.transfer_payload, row.phase = intent.model_dump(mode="json"), "transfer_consumed"
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.handover.transfer_consumed",
        target_type="upgrade_handover",
        target_id=str(row.id),
        context={"generation": row.generation, "intent_id": str(intent.intent_id)},
    )
    await session.flush()
    return intent
