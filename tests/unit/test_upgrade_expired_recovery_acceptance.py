"""Actual PostgreSQL acceptance serializes retries and preserves old authority."""

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_upgrade_expired_recovery_contract import intent_payload

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    AuditRow,
    Base,
    UpgradeExpiredRecoveryRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UserRow,
)
from coire_api.upgrades.expired_recovery import accept_expired_recovery
from coire_api.upgrades.persistence import register_release
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.auth import UserRole
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryIntent
from coire_core.models.upgrades import UpgradePhase


async def test_real_acceptance_replay_conflict_and_original_preservation(
    training_postgres_url, monkeypatch
):
    body = intent_payload()
    owner = UUID(body["original_plan"]["operation"]["user_id"])
    body["accepted_by"] = str(owner)
    intent = UpgradeExpiredRecoveryIntent.model_validate_json(json.dumps(body))
    plan = intent.original_plan
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=owner, role=UserRole.ADMIN)
    schema = "expired_recovery_" + uuid4().hex
    root = create_async_engine(training_postgres_url)
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with root.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@example.test",
                    display_name="Recovery admin",
                    role=UserRole.ADMIN,
                )
            )
        async with sessions.begin() as session:
            await register_release(session, principal, plan.source)
            await register_release(session, principal, plan.target)
            session.add(
                UpgradeOperationRow(
                    id=operation.job_id,
                    user_id=owner,
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

        async def accept(value):
            async with sessions.begin() as session:
                return await accept_expired_recovery(session, principal, value)

        for shift in (timedelta(seconds=10), timedelta(seconds=-10), timedelta(minutes=-10)):
            stale = intent.model_copy(
                update={
                    "accepted_at": intent.accepted_at + shift,
                    "deadline_at": intent.deadline_at + shift,
                }
            )
            with pytest.raises(UpgradeUnavailable):
                await accept(stale)

        from coire_api.upgrades import expired_recovery

        async def unavailable_audit(*args, **kwargs):
            raise RuntimeError("fixture audit failure")

        with monkeypatch.context() as patch:
            patch.setattr(expired_recovery, "write_principal_audit", unavailable_audit)
            with pytest.raises(RuntimeError, match="fixture audit failure"):
                await accept(intent)
        async with sessions.begin() as session:
            assert not (await session.scalars(select(UpgradeExpiredRecoveryRow))).all()
        results = await asyncio.gather(accept(intent), accept(intent))
        assert sum(created for _, created in results) == 1
        assert all(retained == intent for retained, _ in results)
        recaptured = intent.model_copy(
            update={
                "recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAX",
                "accepted_at": intent.accepted_at + timedelta(hours=1),
                "deadline_at": intent.deadline_at + timedelta(hours=1),
            }
        )
        retained, created = await accept(recaptured)
        assert retained == intent and not created
        assert retained.deadline_at == intent.deadline_at
        # A valid, differently scoped request must conflict on the same key.
        changed = intent.model_copy(
            update={
                "request": intent.request.model_copy(update={"original_scope_sha256s": ("f" * 64,)})
            }
        )
        with pytest.raises(UpgradeConflict):
            await accept(changed)
        async with sessions.begin() as session:
            row = await session.get(UpgradeOperationRow, operation.job_id)
            assert row.payload == operation.model_dump(mode="json")
            assert row.execution_plan == plan.model_dump(mode="json")
            assert row.active_slot == 1
            assert len((await session.scalars(select(UpgradeExpiredRecoveryRow))).all()) == 1
            audits = (
                await session.scalars(
                    select(AuditRow).where(AuditRow.action == "upgrades.expired_recovery.accept")
                )
            ).all()
            assert len(audits) == 1
        async with sessions.begin() as session:
            user = await session.get(UserRow, owner)
            user.role = UserRole.USER
        with pytest.raises(UpgradeForbidden):
            await accept(intent)
        async with sessions.begin() as session:
            assert len((await session.scalars(select(UpgradeExpiredRecoveryRow))).all()) == 1
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await root.dispose()
