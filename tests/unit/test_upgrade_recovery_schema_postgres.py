"""Actual transactional legacy preparation, immutable authority and unchanged history."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable
from upgrade_recovery_fixtures import retained_native_fixture

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import AuditRow, Base, UpgradeMaintenanceRow, UpgradeOperationRow, UserRow
from coire_api.upgrades import recovery_schema as service
from coire_api.upgrades.persistence import register_release
from coire_core.errors import UpgradeConflict
from coire_core.models.auth import UserRole
from coire_core.models.upgrade_recovery import UpgradeRecoverySchemaRequest
from coire_core.models.upgrades import UpgradePhase


@pytest.mark.parametrize("fault", [None, "partial", "unknown_checks", "source"])
async def test_legacy_schema_known_preimage_audited_atomic_replay_and_refusal(
    training_postgres_url, tmp_path, monkeypatch, fault
):
    plan, _, _, _, _ = retained_native_fixture(tmp_path)
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    actor = Principal(kind=PrincipalKind.ADMIN, user_id=operation.user_id, role=UserRole.ADMIN)
    namespace = "legacy_recovery_" + uuid4().hex
    root = create_async_engine(training_postgres_url)
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": namespace}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with root.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {namespace}"))
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        t
                        for t in Base.metadata.sorted_tables
                        if t.name not in {t.name for t in service.TABLES}
                    ],
                )
            )
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
            )
            await conn.execute(text("INSERT INTO alembic_version VALUES ('0034_upgrades')"))
            for name, definition in service.legacy_checks().items():
                await conn.execute(text(f"ALTER TABLE upgrade_operations DROP CONSTRAINT {name}"))
                await conn.execute(
                    text(f"ALTER TABLE upgrade_operations ADD CONSTRAINT {name} {definition}")
                )
            if fault == "unknown_checks":
                await conn.execute(
                    text("ALTER TABLE upgrade_operations DROP CONSTRAINT ck_upgrade_phase")
                )
                await conn.execute(
                    text(
                        "ALTER TABLE upgrade_operations ADD CONSTRAINT ck_upgrade_phase CHECK (length(phase)>0)"
                    )
                )
            if fault == "partial":
                await conn.execute(CreateTable(service.TABLES[0]))
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=operation.user_id,
                    email=f"{operation.user_id}@test.invalid",
                    display_name="Preserved",
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
                    phase="recovery_required",
                    payload=operation.model_dump(mode="json"),
                    execution_plan=plan.model_dump(mode="json"),
                )
            )
            await session.flush()
            maintenance = plan.maintenance.model_copy(
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
                        payload=maintenance.model_dump(mode="json"),
                    )
                )
        async with sessions.begin() as session:
            if fault == "partial":
                with pytest.raises(UpgradeConflict, match="partial"):
                    await service.inspect_legacy_schema(session, actor, operation.job_id)
                return
            observed = await service.inspect_legacy_schema(session, actor, operation.job_id)
        request = UpgradeRecoverySchemaRequest(
            original_job_id=operation.job_id,
            original_generation=operation.generation,
            original_plan_sha256=plan.authority_sha256(),
            expected_source_sha256="f" * 64 if fault == "source" else plan.source.manifest_sha256,
            expected_schema_sha256=observed.schema_sha256,
            idempotency_key="known-legacy-preparation",
        )

        async def prepare():
            async with sessions.begin() as session:
                return await service.prepare_legacy_schema(session, actor, request)

        if fault is not None:
            with pytest.raises(UpgradeConflict):
                await prepare()
            async with sessions.begin() as session:
                assert (
                    await service.inspect_legacy_schema(session, actor, operation.job_id)
                ).schema_sha256 == observed.schema_sha256
            return

        async def unavailable_audit(*a, **kw):
            raise RuntimeError("fixture audit failure")

        with monkeypatch.context() as patch:
            patch.setattr(service, "write_principal_audit", unavailable_audit)
            with pytest.raises(RuntimeError, match="audit failure"):
                await prepare()
        async with sessions.begin() as session:
            assert (
                await service.inspect_legacy_schema(session, actor, operation.job_id)
            ).schema_sha256 == observed.schema_sha256
        results = await asyncio.gather(prepare(), prepare())
        assert sorted(created for _, created in results) == [False, True]
        receipt = results[0][0]
        assert results[1][0] == receipt
        assert await prepare() == (receipt, False)
        assert receipt.before.recovery_tables_absent and not receipt.after.recovery_tables_absent
        async with sessions.begin() as session:
            assert (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "0034_upgrades"
            original = await session.get(UpgradeOperationRow, operation.job_id)
            assert original.payload == operation.model_dump(
                mode="json"
            ) and original.execution_plan == plan.model_dump(mode="json")
            assert original.active_slot == 1 and original.phase == "recovery_required"
            assert all(
                not row.released and row.payload == maintenance.model_dump(mode="json")
                for row in (await session.scalars(select(UpgradeMaintenanceRow))).all()
            )
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
            assert (await session.get(UserRow, operation.user_id)).display_name == "Preserved"
            triggers = (
                await session.scalars(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname IN ('expired_recovery_authority','recovery_succession_immutable','recovery_successor_immutable')"
                    )
                )
            ).all()
            assert set(triggers) == {
                "expired_recovery_authority",
                "recovery_succession_immutable",
                "recovery_successor_immutable",
            }
        from coire_api import db
        from coire_api.identity.keys import issue_key
        from coire_api.upgrades import recovery_schema_bootstrap as bootstrap
        from coire_core.errors import UpgradeUnavailable
        from coire_core.models.auth import ApiKeyCreate, AuthScope
        from coire_core.models.upgrade_recovery import UpgradeRecoverySchemaBootstrapInput
        from coire_core.settings import Settings

        async with sessions.begin() as session:
            key = await issue_key(
                session,
                operation.user_id,
                ApiKeyCreate(
                    name="Schema fixture",
                    scopes=frozenset({AuthScope.ADMIN}),
                    requests_per_minute=100,
                    monthly_budget_tokens=1000,
                ),
                actor="fixture",
            )
        secret_file = tmp_path / "admin-token"
        secret_file.write_text(key.secret)
        secret_file.chmod(0o600)
        settings = Settings(
            upgrades_enabled=True, upgrade_recovery_schema_admin_token_file=str(secret_file)
        )
        monkeypatch.setattr(db, "_sessionmaker", sessions)
        monkeypatch.setattr(bootstrap, "init_engine", lambda _: engine)
        monkeypatch.setattr(bootstrap, "dispose_engine", AsyncMock())
        monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
        command = UpgradeRecoverySchemaBootstrapInput(
            command="prepare", original_job_id=operation.job_id, preparation=request
        )
        assert await bootstrap.invoke_admin_api(settings, command) == receipt
        observed_api = await bootstrap.invoke_admin_api(
            settings,
            UpgradeRecoverySchemaBootstrapInput(
                command="observe", original_job_id=operation.job_id
            ),
        )
        assert not observed_api.recovery_tables_absent
        assert observed_api.schema_sha256 == receipt.after.schema_sha256
        secret_file.write_text("invalid-fixture-credential")
        with pytest.raises(UpgradeUnavailable, match="status 401"):
            await bootstrap.invoke_admin_api(settings, command)
        secret_file.write_text(key.secret)
        async with sessions.begin() as session:
            (await session.get(UserRow, operation.user_id)).role = UserRole.USER
        with pytest.raises(UpgradeUnavailable, match="status 403"):
            await bootstrap.invoke_admin_api(settings, command)
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {namespace} CASCADE"))
        await root.dispose()
