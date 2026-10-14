"""Retained native authority fixtures shared by contract and unit tests."""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from upgrade_native_fixtures import native_plan

from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryRequest
from coire_core.models.upgrades import (
    NativeRecoveryIntent,
    NodeAgentProcessIdentity,
    NodeUpgradeCollectiveRead,
    NodeUpgradeCollectiveScope,
)


def retained_native_fixture(tmp_path):
    plan, source, candidate = native_plan(tmp_path)
    plan = plan.model_copy(
        update={
            "peer_runtime_sha256": "c" * 64,
            "maintenance": plan.maintenance.model_copy(
                update={"targets": ("coire-edge-a", "coire-edge-b")}
            ),
        }
    )
    now = datetime.now(UTC)
    started = now - timedelta(minutes=10)
    native = NativeRecoveryIntent(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        plan_sha256=plan.authority_sha256(),
        source=source,
        candidate=candidate,
        source_process=NodeAgentProcessIdentity(
            pid=123450, create_time_s=123.0, environment_sha256=source.runtime.environment_sha256
        ),
        phase="recovery_required",
        recovery_started_at=started,
        recovery_deadline_at=started + timedelta(seconds=300),
        updated_at=now,
    )
    read = NodeUpgradeCollectiveRead(
        generation=native.generation,
        plan_sha256=native.plan_sha256,
        stage="candidate",
        runtime_sha256=candidate.environment_sha256,
        deadline_at=started + timedelta(seconds=120),
    )
    scope = NodeUpgradeCollectiveScope(
        plan=plan,
        request=read,
        index=0,
        command_id=uuid5(
            NAMESPACE_URL,
            f"coire-upgrade-collective/{native.job_id}/candidate/{read.plan_sha256}/0",
        ),
    )
    request = UpgradeExpiredRecoveryRequest(
        original_job_id=native.job_id,
        original_generation=native.generation,
        original_plan_sha256=native.plan_sha256,
        original_scope_sha256s=(scope.sha256(),),
        expected_source_sha256=plan.source.manifest_sha256,
        support_release_id=plan.target.release_id,
        idempotency_key="capture-test",
    )
    return plan, native, scope, request, now
