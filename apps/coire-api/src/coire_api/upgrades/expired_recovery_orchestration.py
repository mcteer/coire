"""Bounded trusted observer orchestration; never accept caller-supplied snapshots."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coire_api.auth import Principal
from coire_api.db import UpgradeExpiredRecoveryRow, UpgradeOperationRow
from coire_api.training.service import training_id
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.expired_recovery import accept_expired_recovery
from coire_api.upgrades.persistence import get_release, operation_lock
from coire_core.errors import UpgradeConflict, UpgradeNotFound, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryObservation,
    UpgradeExpiredRecoveryRequest,
)
from coire_core.models.upgrades import (
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeOperation,
    UpgradePhase,
    UpgradeRelease,
)
from coire_core.upgrade_recovery import capture_expired_native_recovery

tracer = trace.get_tracer("coire.api.expired_recovery")
Observer = Callable[
    [UpgradeExpiredRecoveryRequest, UpgradeRelease], Awaitable[UpgradeExpiredRecoveryObservation]
]


async def load_context(
    session: AsyncSession, principal: Principal, request: UpgradeExpiredRecoveryRequest
) -> tuple[
    UUID,
    UpgradeExpiredRecoveryIntent | None,
    UpgradeOperation | None,
    UpgradeExecutionPlan | None,
    UpgradeRelease | None,
]:
    await operation_lock(session)
    from coire_api.upgrades.recovery_support_scope import require_original

    require_original(
        request.original_job_id, request.original_generation, request.expected_source_sha256
    )
    owner = await authorize(session, principal)
    existing = await session.scalar(
        select(UpgradeExpiredRecoveryRow).where(
            UpgradeExpiredRecoveryRow.user_id == owner,
            UpgradeExpiredRecoveryRow.idempotency_key == request.idempotency_key,
        )
    )
    if existing:
        intent = UpgradeExpiredRecoveryIntent.model_validate(existing.intent_payload)
        if existing.request_sha256 != request.sha256() or existing.intent_sha256 != intent.sha256():
            raise UpgradeConflict("Recovery idempotency or retained authority differs")
        return owner, intent, None, None, None
    original = await session.get(UpgradeOperationRow, request.original_job_id)
    if original is None or original.execution_plan is None:
        raise UpgradeNotFound()
    operation = UpgradeOperation.model_validate(original.payload)
    plan = UpgradeExecutionPlan.model_validate(original.execution_plan)
    if (
        original.active_slot != 1
        or original.phase != UpgradePhase.RECOVERY_REQUIRED.value
        or operation.kind is not UpgradeKind.STUDIO
        or operation.phase is not UpgradePhase.RECOVERY_REQUIRED
        or operation.finished_at is not None
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or operation.generation != request.original_generation
        or plan.authority_sha256() != request.original_plan_sha256
        or plan.source.manifest_sha256 != request.expected_source_sha256
    ):
        raise UpgradeConflict("Original recovery request authority differs")
    support = await get_release(session, request.support_release_id)
    if (
        support.manifest.kind != operation.kind
        or support.manifest.platform != plan.source.manifest.platform
        or plan.source.manifest_sha256 not in support.manifest.compatibility.source_sha256s
    ):
        raise UpgradeConflict("Recovery support is incompatible with retained Source")
    return owner, None, operation, plan, support


async def accept_with_observation(
    sessions: async_sessionmaker[AsyncSession],
    principal: Principal,
    request: UpgradeExpiredRecoveryRequest,
    observe: Observer,
) -> tuple[UpgradeExpiredRecoveryIntent, bool]:
    """Only an application-installed authenticated fixed-destination observer.

    Commit snapshot before I/O; final acceptance rechecks authority/fences/catalog.
    The admin route consumes only the fixed authenticated initial-observer relay.
    """
    with tracer.start_as_current_span("coire.api.expired_recovery_accept"):
        async with sessions.begin() as session:
            owner, existing, operation, plan, support = await load_context(
                session, principal, request
            )
        if existing is not None:
            return existing, False
        if operation is None or plan is None or support is None:
            raise UpgradeUnavailable("Recovery context is incomplete")
        try:
            async with asyncio.timeout(5):
                observation = await observe(request, support)
        except TimeoutError:
            raise UpgradeUnavailable("Recovery observation budget exhausted") from None
        observation = UpgradeExpiredRecoveryObservation.model_validate_json(
            observation.model_dump_json()
        )
        now = datetime.now(UTC)
        if (
            not 0 <= (now - observation.observed_at).total_seconds() <= 5
            or observation.request.sha256() != request.sha256()
            or observation.plan.authority_sha256() != plan.authority_sha256()
            or observation.support.sha256() != support.sha256()
        ):
            raise UpgradeUnavailable("Recovery observation is stale or differs")
        intent = capture_expired_native_recovery(
            request=request,
            operation=operation,
            plan=plan,
            native=observation.native,
            support=support,
            scopes=observation.scopes,
            recovery_id=training_id(),
            accepted_by=owner,
            now=now,
        )
        async with sessions.begin() as session:
            from coire_api.upgrades.expired_recovery_data import capture_data_snapshot

            # Capture before any accepted native effect. It is committed with
            # the immutable intent and its audit, never recaptured on replay.
            baseline = await capture_data_snapshot(session, intent)
            return await accept_expired_recovery(
                session,
                principal,
                intent,
                data_baseline=baseline,
                native_baseline=observation.native_data,
            )
