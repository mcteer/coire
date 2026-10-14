"""An uncertain one-shot schema effect is retained and never automatically repeated."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from upgrade_fixtures import core_plan

from coire_api.upgrades import migration
from coire_core.errors import UpgradeConflict, UpgradeForbidden
from coire_core.models.upgrades import UpgradePhase, UpgradeReceipt
from coire_core.settings import Settings


async def test_not_started_fence_prevents_a_late_migration_claim(monkeypatch) -> None:
    from coire_core.models.upgrades import UpgradeMigrationNotStarted

    plan = core_plan(schema_changed=True)
    proof = UpgradeMigrationNotStarted(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        plan_sha256=plan.authority_sha256(),
        grant_id=uuid4(),
        schema_head=plan.initial_schema_head,
        schema_sha256=plan.initial_schema_sha256,
        observed_at=datetime.now(UTC),
    )
    row = SimpleNamespace(
        execution_plan=plan.model_dump(mode="json"),
        payload=plan.operation.model_dump(mode="json"),
        migration_execution=proof.model_dump(mode="json"),
    )
    monkeypatch.setattr(migration, "operation_lock", AsyncMock())
    monkeypatch.setattr(migration, "get_operation", AsyncMock(return_value=row))
    session = AsyncMock()
    with pytest.raises(UpgradeConflict, match="fenced"):
        await migration.claim_migration(
            session,
            plan.operation.job_id,
            plan.operation.generation,
            Settings(upgrades_enabled=True),
        )
    session.scalar.assert_not_awaited()
    assert row.migration_execution == proof.model_dump(mode="json")


@pytest.mark.parametrize("fault", [None, "started", "schema", "unconsumed"])
async def test_migration_not_started_is_an_atomic_positive_fence(monkeypatch, fault) -> None:
    plan = core_plan(schema_changed=True)
    row = SimpleNamespace(
        execution_plan=plan.model_dump(mode="json"),
        payload=plan.operation.model_dump(mode="json"),
        migration_execution={"state": "intent"} if fault == "started" else None,
        executor_identity="coire-upgrade-executor",
    )
    grant = SimpleNamespace(
        id=uuid4(), consumed_at=None if fault == "unconsumed" else datetime.now(UTC)
    )
    session = AsyncMock()
    session.scalar.return_value = grant
    monkeypatch.setattr(migration, "operation_lock", AsyncMock())
    monkeypatch.setattr(migration, "get_operation", AsyncMock(return_value=row))
    monkeypatch.setattr(migration, "verify_fenced", AsyncMock())
    observed = AsyncMock(
        return_value=(
            plan.initial_schema_head,
            "f" * 64 if fault == "schema" else plan.initial_schema_sha256,
        )
    )
    monkeypatch.setattr(migration, "inspect_schema", observed)
    audit = AsyncMock()
    monkeypatch.setattr(migration, "write_principal_audit", audit)
    principal = SimpleNamespace(subject="coire-upgrade-executor")
    if fault:
        with pytest.raises(UpgradeConflict):
            await migration.fence_not_started(
                session, plan, principal, Settings(upgrades_enabled=True)
            )
        audit.assert_not_awaited()
        return
    result = await migration.fence_not_started(
        session, plan, principal, Settings(upgrades_enabled=True)
    )
    assert result.state == "not_started" and result.execution_fenced
    assert row.migration_execution == result.model_dump(mode="json")
    observed.assert_awaited_once_with(session)
    audit.assert_awaited_once()
    session.flush.assert_awaited_once()
    replay = await migration.fence_not_started(
        session, plan, principal, Settings(upgrades_enabled=True)
    )
    assert replay == result
    assert observed.await_count == 1


@pytest.mark.parametrize("fault", [None, "cancel", "unconsumed", "deadline", "schema", "uncertain"])
async def test_migration_claim_is_phase_bound_durable_and_non_replayable(
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    plan = core_plan(schema_changed=True)
    now = datetime.now(UTC)
    captured = now - timedelta(seconds=121) if fault == "deadline" else now
    operation = plan.operation.model_copy(
        update={"phase": UpgradePhase.MIGRATING, "cancel_requested": fault == "cancel"}
    )
    row = SimpleNamespace(
        execution_plan=plan.model_dump(mode="json"),
        payload=operation.model_dump(mode="json"),
        migration_execution=None,
        executor_identity="coire-upgrade-executor",
        user_id=plan.operation.user_id,
        key_id=None,
        credential_version=None,
    )
    proof = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=1,
        effect_id="migration-activation",
        phase=UpgradePhase.MIGRATING,
        occurred_at=captured,
        outcome="intent",
        observed_sha256=operation.source_sha256,
    )
    grant = SimpleNamespace(
        id=uuid4(),
        consumed_at=None if fault == "unconsumed" else captured,
        expires_at=captured + timedelta(seconds=30),
    )
    session = AsyncMock()
    session.scalar.side_effect = [grant, SimpleNamespace(payload=proof.model_dump(mode="json"))]
    monkeypatch.setattr(migration, "operation_lock", AsyncMock())
    monkeypatch.setattr(migration, "get_operation", AsyncMock(return_value=row))
    monkeypatch.setattr(migration, "authorize", AsyncMock())
    monkeypatch.setattr(migration, "verify_fenced", AsyncMock())
    monkeypatch.setattr(migration, "write_principal_audit", AsyncMock())
    monkeypatch.setattr(
        migration,
        "inspect_schema",
        AsyncMock(
            return_value=(
                plan.initial_schema_head,
                "f" * 64 if fault == "schema" else plan.initial_schema_sha256,
            )
        ),
    )
    settings = Settings(upgrades_enabled=True)
    if fault in {"cancel", "unconsumed", "deadline", "schema"}:
        with pytest.raises((UpgradeConflict, UpgradeForbidden)):
            await migration.claim_migration(
                session, operation.job_id, operation.generation, settings, now=now
            )
        assert row.migration_execution is None
        return
    _, intent = await migration.claim_migration(
        session, operation.job_id, operation.generation, settings, now=now
    )
    assert row.migration_execution == intent.model_dump(mode="json")
    assert intent.deadline_at == captured + timedelta(seconds=120)
    session.scalar.reset_mock()
    with pytest.raises(UpgradeConflict, match="re-execution"):
        await migration.claim_migration(
            session, operation.job_id, operation.generation, settings, now=now
        )
    session.scalar.assert_not_awaited()
    if fault == "uncertain":
        exit_code, after_head = 1, plan.initial_schema_head
    else:
        exit_code, after_head = 0, plan.schema_head_after
    monkeypatch.setattr(migration, "inspect_schema", AsyncMock(return_value=(after_head, "c" * 64)))
    result = await migration.record_migration(session, plan, intent, exit_code=exit_code, now=now)
    assert result.state == ("uncertain" if fault else "complete")
    if fault:
        with pytest.raises(UpgradeConflict, match="re-execution"):
            await migration.claim_migration(
                session, operation.job_id, operation.generation, settings, now=now
            )
    else:
        _, replay = await migration.claim_migration(
            session, operation.job_id, operation.generation, settings, now=now
        )
        assert replay == result
