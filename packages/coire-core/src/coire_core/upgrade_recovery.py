"""Private expired-recovery intent retention, independent of effect grants."""

from datetime import datetime, timedelta
from uuid import UUID

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryNativeDataSnapshot,
    UpgradeExpiredRecoveryRequest,
)
from coire_core.models.upgrades import (
    NativeRecoveryIntent,
    NodeUpgradeCollectiveScope,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeOperation,
    UpgradePhase,
    UpgradeRelease,
)
from coire_core.upgrade_journal import PrivateUpgradeStore


def verify_native_data_preserved(
    before: UpgradeExpiredRecoveryNativeDataSnapshot,
    after: UpgradeExpiredRecoveryNativeDataSnapshot,
    intent: UpgradeExpiredRecoveryIntent,
) -> None:
    for snapshot in (before, after):
        if (
            snapshot.original_job_id,
            snapshot.original_generation,
            snapshot.node,
            snapshot.source_sha256,
        ) != (
            intent.request.original_job_id,
            intent.request.original_generation,
            intent.original_plan.operation.node,
            intent.request.expected_source_sha256,
        ):
            raise UpgradeConflict(
                "Native preservation observations belong to another original scope"
            )
    if (
        before.root_sha256s != after.root_sha256s
        or before.file_counts != after.file_counts
        or before.total_bytes != after.total_bytes
        or not before.observed_at <= intent.accepted_at <= after.observed_at < intent.deadline_at
    ):
        raise UpgradeUnavailable(
            "Recovery cannot prove unchanged native published data and obligations"
        )


def retain_expired_recovery_intent(
    store: PrivateUpgradeStore, intent: UpgradeExpiredRecoveryIntent
) -> UpgradeExpiredRecoveryIntent:
    """Retain an already captured intent; never infer/recreate a deadline.

    The caller supplies a trusted private store, not a wire path. Retention
    alone does not authorize ownership transfer, Source mutation or engines;
    an authenticated/audited acceptance and separate scoped effect authority
    must be verified by their producers before any such effect.
    """
    intent = UpgradeExpiredRecoveryIntent.model_validate_json(intent.model_dump_json())
    key = intent.recovery_id + ".json"
    with store.ownership("expired-recovery.lock"):
        existing = store.read(key, UpgradeExpiredRecoveryIntent)
        if existing is not None:
            if existing.sha256() != intent.sha256():
                raise UpgradeConflict("Recovery intent changes captured authority or budget")
            return existing
        store.write(key, intent, replace=False)
        return intent


def capture_expired_native_recovery(
    *,
    request: UpgradeExpiredRecoveryRequest,
    operation: UpgradeOperation,
    plan: UpgradeExecutionPlan,
    native: NativeRecoveryIntent,
    support: UpgradeRelease,
    scopes: tuple[NodeUpgradeCollectiveScope, ...],
    recovery_id: str,
    accepted_by: UUID,
    now: datetime,
) -> UpgradeExpiredRecoveryIntent:
    """Pure capture from trusted observations; no audit/authorization/effects.

    Producers must authenticate and pin Core/native authority before calling,
    resolve the support catalog entry, and commit acceptance/audit atomically.
    Replay must return persisted capture instead of calling this function again.
    """
    native = NativeRecoveryIntent.model_validate_json(native.model_dump_json())
    if (
        operation.kind is not UpgradeKind.STUDIO
        or operation.phase is not UpgradePhase.RECOVERY_REQUIRED
        or operation.finished_at is not None
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or native.phase
        not in {"recovery_required", "restoring", "source_selected", "source_verified"}
        or (native.job_id, native.generation, native.plan_sha256)
        != (operation.job_id, operation.generation, plan.authority_sha256())
        or native.source.release_sha256 != plan.source.manifest_sha256
        or native.source.runtime.environment_sha256 != plan.source.manifest.environment_sha256
    ):
        raise UpgradeConflict("Expired recovery retained authority differs")
    deadline = native.recovery_deadline_at
    if now.tzinfo is None or deadline is None or deadline > now:
        raise UpgradeUnavailable("Native recovery epoch is not verifiably expired")
    if sorted(scope.sha256() for scope in scopes) != sorted(request.original_scope_sha256s) or any(
        scope.plan.authority_sha256() != plan.authority_sha256() for scope in scopes
    ):
        raise UpgradeConflict("Expired recovery retained dispatch scope differs")
    return UpgradeExpiredRecoveryIntent(
        recovery_id=recovery_id,
        request=request,
        original_plan=plan,
        support_release=support,
        accepted_by=accepted_by,
        accepted_at=now,
        original_recovery_deadline_at=deadline,
        deadline_at=now + timedelta(seconds=request.recovery_s),
    )
