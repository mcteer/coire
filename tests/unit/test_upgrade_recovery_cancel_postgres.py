"""Actual PostgreSQL cancellation before native ownership or after budget expiry."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    AuditRow,
    Base,
    UpgradeExpiredRecoveryRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UserRow,
)
from coire_api.upgrades import expired_recovery_cancel as service
from coire_api.upgrades.expired_recovery import accept_expired_recovery
from coire_api.upgrades.expired_recovery_consumption import consume_transfer
from coire_api.upgrades.expired_recovery_transfer import prepare_transfer
from coire_api.upgrades.persistence import register_release
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.auth import UserRole
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancelRequest,
    UpgradeExpiredRecoveryOwnerTransfer,
)
from coire_core.models.upgrades import UpgradePhase


@pytest.mark.parametrize("stage", ["accepted", "transferring", "expired"])
async def test_cancel_retains_original_and_prevents_first_consumption(
    training_postgres_url, tmp_path, monkeypatch, stage
):
    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    plan = transfer.intent.original_plan
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    actor = Principal(
        kind=PrincipalKind.ADMIN, user_id=transfer.intent.accepted_by, role=UserRole.ADMIN
    )
    preparer = Principal(kind=PrincipalKind.ADMIN, user_id=operation.user_id, role=UserRole.ADMIN)
    schema = "recovery_cancel_" + uuid4().hex
    root = create_async_engine(training_postgres_url)
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with root.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            for owner in {actor.user_id, preparer.user_id}:
                session.add(
                    UserRow(
                        id=owner,
                        email=f"{owner}@test.invalid",
                        display_name="Fixture",
                        role=UserRole.ADMIN,
                    )
                )
        async with sessions.begin() as session:
            await register_release(session, preparer, plan.source)
            await register_release(session, preparer, plan.target)
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
            for target in plan.maintenance.targets:
                session.add(
                    UpgradeMaintenanceRow(
                        target=target,
                        job_id=operation.job_id,
                        generation=operation.generation,
                        released=False,
                        payload=plan.maintenance.model_dump(mode="json"),
                    )
                )
        now = datetime.now(UTC)
        intent = transfer.intent.model_copy(
            update={"accepted_at": now, "deadline_at": now + timedelta(seconds=300)}
        )
        async with sessions.begin() as session:
            await accept_expired_recovery(session, actor, intent)
        transfer = transfer.model_copy(
            update={
                "intent": intent,
                "issued_at": now,
                "expires_at": now + timedelta(seconds=30),
                "legacy_guard": transfer.legacy_guard.model_copy(update={"observed_at": now}),
            }
        )
        if stage != "accepted":
            async with sessions.begin() as session:
                await prepare_transfer(session, actor, transfer)
        if stage == "expired":
            monkeypatch.setattr(
                service,
                "datetime",
                SimpleNamespace(now=lambda zone: intent.deadline_at + timedelta(seconds=1)),
            )
        request = UpgradeExpiredRecoveryCancelRequest(intent_sha256=intent.sha256())
        async with sessions.begin() as session:
            proof, created = await service.cancel_recovery(
                session, actor, intent.recovery_id, request
            )
            assert created
        async with sessions.begin() as session:
            assert await service.cancel_recovery(session, actor, intent.recovery_id, request) == (
                proof,
                False,
            )
        with pytest.raises(UpgradeUnavailable):
            async with sessions.begin() as session:
                await consume_transfer(session, actor, transfer)
        with pytest.raises(UpgradeConflict):
            async with sessions.begin() as session:
                await accept_expired_recovery(
                    session,
                    actor,
                    intent.model_copy(
                        update={
                            "recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAY",
                            "request": intent.request.model_copy(
                                update={"idempotency_key": "new-intent"}
                            ),
                        }
                    ),
                )
        async with sessions.begin() as session:
            row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            assert row.phase == "recovery_required" and row.active_slot == 1
            assert row.intent_payload == intent.model_dump(mode="json")
            assert row.owner_claim_payload is None and row.consumption_payload is None
            assert row.transfer_payload == (
                None if stage == "accepted" else transfer.model_dump(mode="json")
            )
            original = await session.get(UpgradeOperationRow, operation.job_id)
            assert (
                original.payload == operation.model_dump(mode="json") and original.active_slot == 1
            )
            assert all(
                not fence.released
                for fence in (await session.scalars(select(UpgradeMaintenanceRow))).all()
            )
            assert (
                len(
                    (
                        await session.scalars(
                            select(AuditRow).where(
                                AuditRow.action == "upgrades.expired_recovery.cancel"
                            )
                        )
                    ).all()
                )
                == 1
            )
    finally:
        await engine.dispose()
        async with root.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await root.dispose()
