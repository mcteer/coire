"""Actual identity locks permit revocation while a scoped worker is draining."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from upgrade_fixtures import plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import Base, UserRow
from coire_api.upgrades import collective
from coire_core.errors import UpgradeForbidden
from coire_core.models import LinkObservation, ProbeOutcome, ProbeTransport
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    NodeUpgradeCollectiveRead,
    NodeUpgradeCommand,
    NodeUpgradeInventory,
    NodeUpgradeStatus,
    UpgradeActivationGrant,
    UpgradeOwnerInventory,
    UpgradePhase,
    UpgradeReceipt,
)

pytestmark = pytest.mark.integration


async def test_revocation_completes_during_collective_and_prevents_evidence_retention(
    training_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = "upgrade_collective_" + uuid4().hex
    root = create_async_engine(training_postgres_url)
    async with root.begin() as connection:
        await connection.execute(text("CREATE SCHEMA " + schema))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        owner = uuid4()
        actor = Principal(kind=PrincipalKind.ADMIN, user_id=owner, role=UserRole.ADMIN)
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=owner,
                    email=str(owner) + "@example.test",
                    display_name="Probe actor",
                    role=UserRole.ADMIN,
                )
            )
        value = plan()
        value = value.model_copy(
            update={
                "peer_runtime_sha256": "c" * 64,
                "maintenance": value.maintenance.model_copy(
                    update={"targets": ("coire-edge-a", "coire-edge-b")}
                ),
            }
        )
        now = datetime.now(UTC)
        intent = UpgradeReceipt(
            job_id=value.operation.job_id,
            generation=value.operation.generation,
            sequence=1,
            phase=UpgradePhase.ACTIVATING,
            effect_id="activation",
            outcome="intent",
            observed_sha256=value.source.manifest_sha256,
            occurred_at=now,
        )
        grant = UpgradeActivationGrant(
            job_id=value.operation.job_id,
            generation=value.operation.generation,
            boundary="runtime",
            grant_id=uuid4(),
            expires_at=now + timedelta(seconds=30),
        )
        command = NodeUpgradeCommand.model_validate(
            value.model_dump(mode="json")
            | {
                "authorization_expires_at": grant.expires_at,
                "activation_grant": grant.model_dump(mode="json"),
                "activation_receipt": intent.model_dump(mode="json"),
                "peer_inventory": NodeUpgradeInventory(
                    node="coire-edge-b",
                    job_id=value.operation.job_id,
                    generation=value.operation.generation,
                    runtime_sha256=value.peer_runtime_sha256,
                    observed_at=now,
                    owners=(),
                ).model_dump(mode="json"),
            }
        )
        row = SimpleNamespace(
            execution_plan=value.model_dump(mode="json"),
            payload=value.operation.model_copy(
                update={"phase": UpgradePhase.VERIFYING, "receipt_sequence": 1}
            ).model_dump(mode="json"),
            node_command=command.model_dump(mode="json"),
        )
        # This test isolates real SQL identity barriers. Native engines and the
        # accepted-operation/fence persistence are tested separately.
        monkeypatch.setattr(collective, "get_operation", AsyncMock(return_value=row))
        monkeypatch.setattr(collective, "verify_fenced", AsyncMock())
        monkeypatch.setattr(collective, "initiating_actor", lambda _: actor)
        appended = AsyncMock()
        monkeypatch.setattr(collective, "append_observation", appended)

        def digest(name: str) -> str:
            return value.target.manifest.environment_sha256 if name == "coire-edge-a" else "c" * 64

        async def status(name: str) -> NodeUpgradeStatus:
            return NodeUpgradeStatus(
                node=name,
                job_id=value.operation.job_id,
                generation=value.operation.generation,
                bootstrap_ready=True,
                phase=UpgradePhase.VERIFYING,
                runtime_sha256=digest(name),
                observed_at=datetime.now(UTC),
            )

        async def inventory(name: str, job: str, generation: int) -> NodeUpgradeInventory:
            return NodeUpgradeInventory(
                node=name,
                job_id=job,
                generation=generation,
                runtime_sha256=digest(name),
                observed_at=datetime.now(UTC),
                owners=tuple(
                    UpgradeOwnerInventory(owner=owner, status="complete", entries=())
                    for owner in NODE_UPGRADE_OWNERS
                ),
            )

        async def dispatch(scope: object) -> LinkObservation:
            async with sessions.begin() as revocation:
                await revocation.execute(text("SET LOCAL lock_timeout = '1s'"))
                user = await revocation.get(UserRow, owner, with_for_update=True)
                assert user is not None
                user.active = False
            return LinkObservation(
                id=uuid4(),
                node_a="coire-edge-a",
                node_b="coire-edge-b",
                transport=ProbeTransport.JACCL,
                outcome=ProbeOutcome.SUCCEEDED,
                os_version_a="isolated",
                os_version_b="isolated",
                engine_version="isolated",
                bandwidth_bytes_per_second=1,
                latency_ms=1,
                runtime_sha256_a=digest("coire-edge-a"),
                runtime_sha256_b=digest("coire-edge-b"),
                observed_at=datetime.now(UTC),
            )

        client = SimpleNamespace(
            upgrade_status=AsyncMock(side_effect=status),
            upgrade_inventory=AsyncMock(side_effect=inventory),
            run_upgrade_collective=AsyncMock(side_effect=dispatch),
        )
        body = NodeUpgradeCollectiveRead(
            generation=value.operation.generation,
            plan_sha256=value.authority_sha256(),
            stage="candidate",
            runtime_sha256=digest("coire-edge-a"),
            deadline_at=now + timedelta(seconds=value.operation.timing.readiness_s),
        )
        async with sessions() as session:
            with pytest.raises(UpgradeForbidden):
                await collective.qualify_collective(
                    session, "coire-edge-a", value.operation.job_id, body, client
                )
        assert client.run_upgrade_collective.await_count == 1
        appended.assert_not_awaited()
    finally:
        await engine.dispose()
        async with root.begin() as connection:
            await connection.execute(text("DROP SCHEMA " + schema + " CASCADE"))
        await root.dispose()
