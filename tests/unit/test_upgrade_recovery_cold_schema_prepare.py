"""Actual audited schema extension retains cold and original obligations."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_upgrade_recovery_cold_continuation_binding import binding_fixture

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    AuditRow,
    Base,
    UpgradeExpiredRecoveryRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UserRow,
)
from coire_api.upgrades import cold_continuation_context as context
from coire_api.upgrades import cold_continuation_schema as service
from coire_api.upgrades import expired_recovery_data as data
from coire_api.upgrades.cold_continuation_schema_checks import RECOVERY_PHASES, known_checks
from coire_api.upgrades.persistence import register_release
from coire_core.errors import UpgradeConflict
from coire_core.models.auth import UserRole
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancellation,
    UpgradeExpiredRecoveryNativeDataSnapshot,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationRead,
    UpgradeColdContinuationSchemaRequest,
)
from coire_core.models.upgrades import UpgradePhase


@pytest.mark.parametrize("fault", [None, "already_ready", "unknown", "audit"])
async def test_cold_schema_audit_atomicity_replay_and_original_preservation(
    training_postgres_url, tmp_path, monkeypatch, fault
):
    binding, owner, _source = binding_fixture(tmp_path, monkeypatch, with_context=True)
    intent = owner.intent
    plan = intent.original_plan
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    actor = Principal(kind=PrincipalKind.ADMIN, user_id=intent.accepted_by, role=UserRole.ADMIN)
    original_actor = (
        actor.model_copy(update={"user_id": operation.user_id})
        if hasattr(actor, "model_copy")
        else Principal(kind=PrincipalKind.ADMIN, user_id=operation.user_id, role=UserRole.ADMIN)
    )
    namespace = "cold_schema_" + uuid4().hex
    root = create_async_engine(training_postgres_url)
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": namespace}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    expired = binding.observed_at
    try:
        async with root.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {namespace}"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
            )
            await conn.execute(text("INSERT INTO alembic_version VALUES ('0034_upgrades')"))
            if fault != "already_ready":
                for name in known_checks(continuation=True):
                    await conn.execute(
                        text(f"ALTER TABLE upgrade_expired_recoveries DROP CONSTRAINT {name}")
                    )
                phases = ",".join(f"'{phase}'" for phase in RECOVERY_PHASES)
                await conn.execute(
                    text(
                        "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_phase "
                        + f"CHECK (phase IN ({phases}))"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_terminal_owner CHECK ((active_slot IS NULL) = (phase IN ('recovered','cancelled_unchanged')))"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_cancel_retained "
                        "CHECK (cancellation_payload IS NULL OR phase IN ('recovery_required','recovered'))"
                    )
                )
                if fault == "unknown":
                    await conn.execute(
                        text(
                            "ALTER TABLE upgrade_expired_recoveries DROP CONSTRAINT ck_expired_recovery_phase"
                        )
                    )
                    await conn.execute(
                        text(
                            "ALTER TABLE upgrade_expired_recoveries ADD CONSTRAINT ck_expired_recovery_phase CHECK (length(phase)>0)"
                        )
                    )
        async with sessions.begin() as session:
            for user_id in {operation.user_id, intent.accepted_by}:
                session.add(
                    UserRow(
                        id=user_id,
                        email=f"{user_id}@test.invalid",
                        display_name="Retained",
                        role=UserRole.ADMIN,
                    )
                )
        async with sessions.begin() as session:
            await register_release(session, original_actor, plan.source)
            await register_release(session, original_actor, plan.target)
            session.add(
                UpgradeOperationRow(
                    id=operation.job_id,
                    user_id=operation.user_id,
                    source_release_id=plan.source.release_id,
                    target_release_id=plan.target.release_id,
                    idempotency_key=operation.idempotency_key,
                    request_sha256=operation.request_sha256,
                    generation=operation.generation,
                    active_slot=1,
                    phase="recovery_required",
                    payload=operation.model_dump(mode="json"),
                    execution_plan=plan.model_dump(mode="json"),
                )
            )
            await session.flush()
            active = plan.maintenance.model_copy(
                update={
                    "state": "active",
                    "acknowledged_generations": dict.fromkeys(
                        plan.maintenance.targets, operation.generation
                    ),
                }
            )
            for target in plan.maintenance.targets:
                session.add(
                    UpgradeMaintenanceRow(
                        target=target,
                        job_id=operation.job_id,
                        generation=operation.generation,
                        released=False,
                        payload=active.model_dump(mode="json"),
                    )
                )
            baseline = await data.capture_data_snapshot(session, intent)
            native = UpgradeExpiredRecoveryNativeDataSnapshot(
                original_job_id=operation.job_id,
                original_generation=operation.generation,
                source_sha256=plan.source.manifest_sha256,
                node=operation.node,
                root_sha256s=dict.fromkeys(("datasets", "artifacts", "components"), "b" * 64),
                file_counts=dict.fromkeys(("datasets", "artifacts", "components"), 0),
                total_bytes=0,
                observed_at=intent.accepted_at,
            )
            cancellation = UpgradeExpiredRecoveryCancellation(
                recovery_id=intent.recovery_id,
                intent_sha256=intent.sha256(),
                original_job_id=operation.job_id,
                original_generation=operation.generation,
                cancelled_by=actor.user_id,
                cancelled_at=expired,
            )
            session.add(
                UpgradeExpiredRecoveryRow(
                    id=intent.recovery_id,
                    original_job_id=operation.job_id,
                    user_id=actor.user_id,
                    idempotency_key=intent.request.idempotency_key,
                    request_sha256=intent.request.sha256(),
                    intent_sha256=intent.sha256(),
                    intent_payload=intent.model_dump(mode="json"),
                    cold_claim_payload=owner.model_dump(mode="json"),
                    cancellation_payload=cancellation.model_dump(mode="json"),
                    data_baseline_payload=baseline.model_dump(mode="json"),
                    native_baseline_payload=native.model_dump(mode="json"),
                    phase="recovery_required",
                    active_slot=1,
                )
            )

        class ExpiredClock(datetime):
            @classmethod
            def now(cls, tz=UTC):
                return expired

        for module in (service, context, data):
            monkeypatch.setattr(module, "datetime", ExpiredClock)
        read = UpgradeColdContinuationRead(
            previous_intent_sha256=intent.sha256(), previous_owner_sha256=owner.sha256()
        )
        if fault == "unknown":
            with pytest.raises(UpgradeConflict):
                async with sessions.begin() as session:
                    await service.observe_schema(session, actor, intent.recovery_id, read)
            return
        async with sessions.begin() as session:
            before = await service.observe_schema(session, actor, intent.recovery_id, read)
            metadata = await context.read_cold_predecessor_context(
                session, intent.recovery_id, read, "coire-edge-a"
            )
            assert metadata.current_data.table_sha256s == baseline.table_sha256s
        request = UpgradeColdContinuationSchemaRequest(
            previous_recovery_id=intent.recovery_id,
            previous_intent_sha256=intent.sha256(),
            previous_owner_sha256=owner.sha256(),
            expected_schema_sha256=before.schema_sha256,
            idempotency_key="a" * 64,
        )
        if fault == "audit":

            async def failed_audit(*args, **kwargs):
                raise RuntimeError("fixture audit failure")

            with monkeypatch.context() as patch:
                patch.setattr(service, "write_principal_audit", failed_audit)
                with pytest.raises(RuntimeError, match="fixture audit failure"):
                    async with sessions.begin() as session:
                        await service.prepare_schema(session, actor, request)
            async with sessions.begin() as session:
                unchanged = await service.observe_schema(session, actor, intent.recovery_id, read)
                assert unchanged.schema_sha256 == before.schema_sha256
        async with sessions.begin() as session:
            receipt, created = await service.prepare_schema(session, actor, request)
            assert created and receipt.continuation_state_ready
        async with sessions.begin() as session:
            replay, created = await service.prepare_schema(session, actor, request)
            assert not created and replay == receipt
            with pytest.raises(UpgradeConflict):
                await service.prepare_schema(
                    session, actor, request.model_copy(update={"expected_schema_sha256": "e" * 64})
                )
            original = await session.get(UpgradeOperationRow, operation.job_id)
            retained = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            assert original.active_slot == retained.active_slot == 1
            assert original.payload == operation.model_dump(mode="json")
            assert original.execution_plan == plan.model_dump(mode="json")
            assert retained.intent_payload == intent.model_dump(mode="json")
            assert retained.cold_claim_payload == owner.model_dump(mode="json")
            assert all(
                not row.released
                for row in (await session.scalars(select(UpgradeMaintenanceRow))).all()
            )
            audit = await session.scalar(select(AuditRow).where(AuditRow.action == service.ACTION))
            assert (
                audit.detail["idempotency_sha256"]
                == hashlib.sha256(request.idempotency_key.encode()).hexdigest()
            )
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {namespace} CASCADE"))
        await root.dispose()
