"""Actual SQL acceptance excludes application activation and preserves handover actor."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from upgrade_bootstrap_fixtures import bootstrap_heartbeat
from upgrade_fixtures import core_plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import AuditRow, Base, UpgradeExecutorRow, UpgradeHandoverRow, UserRow
from coire_api.upgrades.handover_authority import accept_handover, claim_handover
from coire_api.upgrades.persistence import accept, register_release
from coire_core.errors import UpgradeConflict, UpgradeForbidden
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import CoreHandoverPreparation, UpgradeStartRequest
from coire_core.settings import Settings


async def test_concurrent_handover_acceptance_replay_and_application_exclusion(
    training_postgres_url: str,
) -> None:
    schema = f"upgrade_handover_{uuid4().hex}"
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
            await connection.execute(text("CREATE SEQUENCE upgrade_generation_seq"))
        heartbeat = bootstrap_heartbeat()
        owner = heartbeat.seed.source.prepared_by
        principal = Principal(kind=PrincipalKind.ADMIN, user_id=owner, role=UserRole.ADMIN)
        candidate = core_plan().target.model_copy(update={"prepared_by": owner})
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@example.test",
                    display_name="Admin",
                    role=UserRole.ADMIN,
                )
            )
        async with sessions.begin() as session:
            await register_release(session, principal, heartbeat.seed.source)
            await register_release(session, principal, candidate)
            session.add(
                UpgradeExecutorRow(
                    project="coire",
                    identity=heartbeat.executor_id,
                    image_id=heartbeat.seed.executor_image_id,
                    source_release_id=heartbeat.seed.source.release_id,
                    bootstrap_ready=True,
                    bootstrap_payload=heartbeat.model_dump(mode="json"),
                    heartbeat_at=heartbeat.physical.observed_at,
                    generation=heartbeat.ownership.generation,
                )
            )
        now = datetime.now(UTC)
        request = CoreHandoverPreparation(
            incumbent=heartbeat.seed,
            candidate_release=candidate,
            candidate_runtime_id="candidate-executor",
            candidate_image_id="sha256:" + "f" * 64,
            generation=2,
            initiator_user_id=owner,
            idempotency_key=uuid4(),
            prepared_at=now,
            deadline_at=now + timedelta(seconds=120),
        )
        settings = Settings(upgrades_enabled=True)

        async def submit(value: CoreHandoverPreparation) -> bool | UpgradeConflict:
            try:
                async with sessions.begin() as session:
                    _, created = await accept_handover(session, principal, value, settings)
                    return created
            except UpgradeConflict as error:
                return error

        outcomes = await asyncio.gather(submit(request), submit(request))
        assert sorted(outcomes) == [False, True]
        other = request.model_copy(update={"idempotency_key": uuid4()})
        assert isinstance(await submit(other), UpgradeConflict)
        changed = request.model_copy(update={"candidate_runtime_id": "different-executor"})
        assert isinstance(await submit(changed), UpgradeConflict)
        async with sessions.begin() as session:
            rows = (await session.scalars(select(UpgradeHandoverRow))).all()
            assert len(rows) == 1 and rows[0].active_slot == 1
            audits = (
                await session.scalars(
                    select(AuditRow).where(AuditRow.action == "upgrades.handover.accepted")
                )
            ).all()
            assert len(audits) == 1
        executor = Principal(
            kind=PrincipalKind.UPGRADE_EXECUTOR,
            subject="coire-upgrade-executor",
            scopes=frozenset({"upgrades:execute"}),
        )
        async with sessions.begin() as session:
            assert await claim_handover(session, executor, heartbeat.seed, settings) == request
            assert await claim_handover(session, executor, heartbeat.seed, settings) == request
        async with sessions.begin() as session:
            app_request = UpgradeStartRequest(
                release_id=candidate.release_id,
                expected_source_sha256=heartbeat.seed.source.manifest_sha256,
                idempotency_key=uuid4(),
                acknowledge_maintenance=True,
            )
            try:
                await accept(session, principal, app_request, heartbeat.seed.source.release_id)
            except UpgradeConflict as error:
                assert "handover" in str(error).lower()
            else:
                raise AssertionError("application acceptance crossed active handover")
        async with sessions.begin() as session:
            user = await session.get(UserRow, owner)
            assert user is not None
            user.role = UserRole.USER
        try:
            async with sessions.begin() as session:
                await accept_handover(session, principal, request, settings)
        except UpgradeForbidden:
            pass
        else:
            raise AssertionError("revoked actor replay retained handover authority")
        async with sessions.begin() as session:
            retained = await session.scalar(select(UpgradeHandoverRow))
            assert retained is not None
            assert retained.phase == "passive" and retained.active_slot == 1
            assert retained.preparation_payload == request.model_dump(mode="json")
        try:
            async with sessions.begin() as session:
                await claim_handover(session, executor, heartbeat.seed, settings)
        except UpgradeForbidden:
            pass
        else:
            raise AssertionError("revoked initiating actor authorized passive creation")
    finally:
        await engine.dispose()
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await owner_engine.dispose()
