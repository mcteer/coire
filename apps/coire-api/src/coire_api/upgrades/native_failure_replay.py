"""Node-scoped failure reconciliation with all ordinary receipt/audit guards."""

from datetime import UTC, datetime

from opentelemetry import metrics, trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.upgrades.persistence import get_operation, operation_lock
from coire_api.upgrades.service import ingest_receipt
from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_native_progress import NativeRecoveryFailureReplay
from coire_core.models.upgrades import (
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeOperation,
    UpgradeReceiptAck,
)

replays = metrics.get_meter("coire.api.native_failure_replay").create_counter(
    "coire_upgrade_native_failure_replays_total"
)
tracer = trace.get_tracer("coire.api.native_failure_replay")


@tracer.start_as_current_span("coire.api.native_failure_replay")
async def reconcile_native_failure(
    session: AsyncSession, body: NativeRecoveryFailureReplay, node: str
) -> UpgradeReceiptAck:
    body = NativeRecoveryFailureReplay.model_validate_json(body.model_dump_json())
    native = body.native
    await operation_lock(session)
    row = await get_operation(session, native.job_id, lock=True)
    if row.execution_plan is None:
        raise UpgradeConflict("Native failure replay has no accepted plan")
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    operation = UpgradeOperation.model_validate(row.payload)
    from coire_api.upgrades.recovery_support_scope import require_original

    require_original(native.job_id, native.generation, plan.source.manifest_sha256)
    if (
        plan.operation.kind is not UpgradeKind.STUDIO
        or plan.operation.node != node
        or (native.job_id, native.generation, native.plan_sha256)
        != (operation.job_id, operation.generation, plan.authority_sha256())
        or native.source.release_sha256 != plan.source.manifest_sha256
        or native.source.runtime.environment_sha256 != plan.source.manifest.environment_sha256
        or native.candidate.environment_sha256 != plan.target.manifest.environment_sha256
        or native.recovery_deadline_at is None
        or datetime.now(UTC) < native.recovery_deadline_at
        or any(
            r.phase.value in {"succeeded", "rolled_back", "committing"} for r in body.batch.receipts
        )
    ):
        raise UpgradeConflict("Native failure replay changes retained authority")
    principal = Principal(
        kind=PrincipalKind.SERVICE, subject=node, scopes=frozenset({"upgrades:receipts"})
    )
    for receipt in body.batch.receipts:
        await ingest_receipt(session, receipt, principal)
    operation = UpgradeOperation.model_validate(row.payload)
    return UpgradeReceiptAck(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=operation.receipt_sequence,
    )
