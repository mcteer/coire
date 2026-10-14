"""Real PostgreSQL serialization of physically qualified bootstrap receipts.

Catalog inspection is a unit-tested boundary here; this proves database replay
and audit semantics, rather than full Docker bootstrap acceptance.
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from upgrade_bootstrap_fixtures import bootstrap_heartbeat

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import AuditRow, Base, UpgradeExecutorRow, UpgradeReleaseRow, UserRow
from coire_api.upgrades import bootstrap_heartbeat as service
from coire_core.errors import UpgradeConflict
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import CoreBootstrapHeartbeat, UpgradeReleaseRegister
from coire_core.settings import Settings


async def test_concurrent_bootstrap_receipts_preserve_original_proof_and_one_audit(
    training_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = f"upgrade_bootstrap_{uuid4().hex}"
    owner_engine = create_async_engine(training_postgres_url)
    async with owner_engine.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        prototype = bootstrap_heartbeat()
        source = prototype.seed.source
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=source.prepared_by,
                    email=f"{source.prepared_by}@example.test",
                    display_name="Bootstrap test",
                    role=UserRole.ADMIN,
                )
            )
            await session.flush()
            session.add(
                UpgradeReleaseRow(
                    id=source.release_id,
                    manifest_sha256=source.manifest_sha256,
                    prepared_artifact_id=source.prepared_artifact_id,
                    prepared_by=source.prepared_by,
                    payload=source.model_dump(mode="json"),
                )
            )
        # Capture original observation after schema setup, never renew it within
        # ingestion. The same immutable original receipt is submitted concurrently.
        document = prototype.model_dump(mode="json")
        now = datetime.now(UTC).isoformat()
        document["physical"]["observed_at"] = now
        document["physical"]["source"]["observed_at"] = now
        body = CoreBootstrapHeartbeat.model_validate(document)
        principal = Principal(
            kind=PrincipalKind.UPGRADE_EXECUTOR,
            subject=body.executor_id,
            scopes=frozenset({"upgrades:execute"}),
        )
        settings = Settings(upgrades_enabled=True)

        def verified(_root: object, request: UpgradeReleaseRegister) -> object:
            assert request.expected_sha256 == source.manifest_sha256
            return source

        monkeypatch.setattr(service, "verify_prepared_release", verified)

        async def submit(value: CoreBootstrapHeartbeat) -> CoreBootstrapHeartbeat:
            async with sessions.begin() as session:
                return await service.retain_bootstrap_heartbeat(session, principal, value, settings)

        results = await asyncio.gather(submit(body), submit(body))
        assert all(result == body for result in results)
        async with sessions.begin() as session:
            row = await session.get(UpgradeExecutorRow, "coire")
            assert row is not None and row.bootstrap_payload == body.model_dump(mode="json")
            assert row.heartbeat_at == body.physical.observed_at
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditRow)
                    .where(AuditRow.action == "upgrades.bootstrap.verified")
                )
                == 1
            )
        changed = body.model_dump(mode="json")
        changed["ownership"]["boot_identity"] = "another-kernel-boot"
        changed["lease"]["kernel_boot_identity"] = "another-kernel-boot"
        with pytest.raises(UpgradeConflict):
            await submit(CoreBootstrapHeartbeat.model_validate(changed))
        async with sessions.begin() as session:
            retained = await session.get(UpgradeExecutorRow, "coire")
            assert retained is not None and retained.bootstrap_payload == body.model_dump(
                mode="json"
            )
    finally:
        await engine.dispose()
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await owner_engine.dispose()
