"""Audited no-activation cancellation uses real transaction and exclusion boundaries."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from upgrade_native_fixtures import native_plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import AuditRow, Base, UpgradeOperationRow, UserRow
from coire_api.upgrades import unclaimed_cancellation as service
from coire_api.upgrades.persistence import register_release
from coire_core.errors import UpgradeConflict, UpgradeForbidden
from coire_core.models.auth import UserRole
from coire_core.models.upgrade_unclaimed import UpgradeUnclaimedCancellationRequest


async def test_audit_rollback_concurrency_replay_and_activation_refusal(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _, _ = native_plan(tmp_path)
    operation = plan.operation
    actor = Principal(kind=PrincipalKind.ADMIN, user_id=operation.user_id, role=UserRole.ADMIN)
    schema = "unclaimed_cancel_" + uuid4().hex
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
                    id=operation.user_id,
                    email="unclaimed@fixture.test",
                    display_name="Operator",
                    role=UserRole.ADMIN,
                )
            )
        async with sessions.begin() as session:
            await register_release(session, actor, plan.source)
            await register_release(session, actor, plan.target)
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
                    phase="accepted",
                    payload=operation.model_dump(mode="json"),
                    execution_plan=plan.model_dump(mode="json"),
                )
            )
        request = UpgradeUnclaimedCancellationRequest(plan_sha256=plan.authority_sha256())
        async with sessions.begin() as session:
            with pytest.raises(UpgradeForbidden, match="audited"):
                await service.read_unclaimed_cancellation(session, operation.job_id, "coire-edge-a")
        write = service.write_principal_audit

        async def lost(*args, **kwargs):
            raise RuntimeError("audit failed")

        monkeypatch.setattr(service, "write_principal_audit", lost)
        with pytest.raises(RuntimeError, match="audit failed"):
            async with sessions.begin() as session:
                await service.request_unclaimed_cancellation(
                    session, actor, operation.job_id, request
                )
        async with sessions.begin() as session:
            row = await session.get(UpgradeOperationRow, operation.job_id)
            assert row is not None and not row.payload["cancel_requested"]
        monkeypatch.setattr(service, "write_principal_audit", write)

        async def request_once():
            async with sessions.begin() as session:
                return await service.request_unclaimed_cancellation(
                    session, actor, operation.job_id, request
                )

        first, replay = await asyncio.gather(request_once(), request_once())
        assert first == replay and first.plan == plan
        async with sessions.begin() as session:
            assert (
                len(
                    (
                        await session.scalars(
                            select(AuditRow).where(AuditRow.action == service.ACTION)
                        )
                    ).all()
                )
                == 1
            )
            row = await session.get(UpgradeOperationRow, operation.job_id)
            assert row is not None and row.payload["cancel_requested"]
            assert row.payload["accepted_at"] == operation.model_dump(mode="json")["accepted_at"]
            assert row.execution_plan == plan.model_dump(mode="json")
            state = await service.read_unclaimed_cancellation(
                session, operation.job_id, "coire-edge-a"
            )
            assert state.grant == first
            with pytest.raises(UpgradeForbidden):
                await service.read_unclaimed_cancellation(session, operation.job_id, "coire-edge-b")
            with pytest.raises(UpgradeConflict):
                await service.request_unclaimed_cancellation(
                    session,
                    actor,
                    operation.job_id,
                    request.model_copy(update={"plan_sha256": "a" * 64}),
                )
            row.node_command = {"potential_activation": True}
        async with sessions.begin() as session:
            with pytest.raises(UpgradeConflict, match="without activation"):
                await service.read_unclaimed_cancellation(session, operation.job_id, "coire-edge-a")
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await root.dispose()
        await asyncio.to_thread(unseal_fixture, tmp_path)


def unseal_fixture(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)
