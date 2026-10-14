"""Actual SQL and Source kernel closure; full Linux/Docker qualification is a fixture."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import AuditRow, Base, UpgradeExecutorRow, UpgradeHandoverRow, UserRow
from coire_api.upgrades import handover_unchanged
from coire_api.upgrades.handover_authority import (
    accept_handover,
    claim_handover,
    recovery_claim_handover,
)
from coire_api.upgrades.handover_host_hold import publish_handover_hold
from coire_api.upgrades.persistence import register_release
from coire_core.errors import UpgradeConflict
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import (
    CoreHandoverPreparation,
    CoreHandoverTransferIntent,
    CoreHandoverUnchangedIntent,
    CoreHandoverUnchangedRequest,
    DeploymentHandoverHold,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.ownership import bootstrap_ownership


@pytest.mark.parametrize("revoked,published", [(False, False), (True, False), (False, True)])
async def test_unchanged_failure_survives_revocation_but_cannot_erase_published_authority(
    training_postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revoked: bool,
    published: bool,
) -> None:
    schema = f"upgrade_unchanged_{uuid4().hex}"
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
        seed, physical = prepared_bootstrap()
        owner = seed.source.prepared_by
        admin = Principal(kind=PrincipalKind.ADMIN, user_id=owner, role=UserRole.ADMIN)
        executor = Principal(
            kind=PrincipalKind.UPGRADE_EXECUTOR,
            subject="coire-upgrade-executor",
            scopes=frozenset({"upgrades:execute"}),
        )
        candidate = core_plan().target.model_copy(update={"prepared_by": owner})
        settings = Settings(
            upgrades_enabled=True,
            upgrade_state_dir=str(tmp_path / "state"),
            upgrade_coordination_dir=str(tmp_path / "coordination"),
        )
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
            await register_release(session, admin, seed.source)
            await register_release(session, admin, candidate)
        with bootstrap_ownership(
            Path(settings.upgrade_coordination_dir), seed, physical, settings.upgrade_executor_id
        ) as authority:
            heartbeat = authority.heartbeat(seed, physical)
            async with sessions.begin() as session:
                session.add(
                    UpgradeExecutorRow(
                        project="coire",
                        identity=executor.subject,
                        image_id=seed.executor_image_id,
                        source_release_id=seed.source.release_id,
                        bootstrap_ready=True,
                        bootstrap_payload=heartbeat.model_dump(mode="json"),
                        heartbeat_at=physical.observed_at,
                        generation=authority.claim.generation,
                    )
                )
            at = datetime.now(UTC)
            prep = CoreHandoverPreparation(
                incumbent=seed,
                candidate_release=candidate,
                candidate_runtime_id="candidate",
                candidate_image_id="sha256:" + "f" * 64,
                generation=authority.claim.generation + 1,
                initiator_user_id=owner,
                idempotency_key=uuid4(),
                prepared_at=at,
                deadline_at=at + timedelta(seconds=120),
            )
            async with sessions.begin() as session:
                await accept_handover(session, admin, prep, settings)
                await claim_handover(session, executor, seed, settings)
            publish_handover_hold(settings, prep)
            bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
            bootstrap.write("initial-seed.json", seed)
            bootstrap.write("handover-preparation.json", prep)
            creation = HandoverCreationJournal(bootstrap.root / "handover/2", prep)
            creation.begin()
            creation.store.write(
                "failure-intent.json",
                CoreHandoverUnchangedIntent(
                    preparation_sha256=prep.sha256(),
                    incumbent_seed_sha256=seed.sha256(),
                    generation=prep.generation,
                    intent_at=datetime.now(UTC),
                ),
            )
            if revoked:
                async with sessions.begin() as session:
                    user = await session.get(UserRow, owner)
                    assert user is not None
                    user.active = False
                    user.role = UserRole.USER
            if published:
                async with sessions.begin() as session:
                    row = await session.scalar(select(UpgradeHandoverRow))
                    assert row is not None
                    transfer = CoreHandoverTransferIntent(
                        handover_id=row.id,
                        intent_id=uuid4(),
                        preparation_sha256=prep.sha256(),
                        candidate_seed_sha256="a" * 64,
                        passive_proof_sha256="b" * 64,
                        generation=prep.generation,
                        consumed_at=at,
                        deadline_at=at + timedelta(seconds=30),
                    )
                    row.phase, row.transfer_payload = (
                        "transfer_consumed",
                        transfer.model_dump(mode="json"),
                    )
            _, fresh = prepared_bootstrap(seed.source)
            fresh = fresh.model_copy(update={"seed_sha256": seed.sha256()})
            body = CoreHandoverUnchangedRequest(
                preparation_sha256=prep.sha256(), incumbent=authority.heartbeat(seed, fresh)
            )

            async def qualify(session, principal, heartbeat, settings):
                assert principal == executor and heartbeat.seed == seed
                row = await session.get(UpgradeExecutorRow, "coire", with_for_update=True)
                assert row is not None and row.image_id == seed.executor_image_id
                row.bootstrap_payload = heartbeat.model_dump(mode="json")
                row.heartbeat_at = heartbeat.physical.observed_at
                row.generation = heartbeat.ownership.generation
                return heartbeat

            monkeypatch.setattr(handover_unchanged, "retain_bootstrap_heartbeat", qualify)
            async with sessions.begin() as session:
                await qualify(session, executor, body.incumbent, settings)
                row = await session.scalar(select(UpgradeHandoverRow))
                assert row is not None
                phase_before = row.phase
                if published:
                    with pytest.raises(UpgradeConflict):
                        await recovery_claim_handover(session, executor, seed, settings)
                else:
                    assert await recovery_claim_handover(session, executor, seed, settings) == prep
                    # Revocation does not erase recovery metadata, and reading
                    # it grants no passive creation or SQL phase transition.
                    assert row.phase == phase_before
                    assert row.transfer_payload is None and row.commit_payload is None
            if published:
                async with sessions.begin() as session:
                    with pytest.raises(UpgradeConflict):
                        await handover_unchanged.acknowledge_unchanged(
                            session, executor, body, settings
                        )
            else:
                async with sessions.begin() as session:
                    failed = await handover_unchanged.acknowledge_unchanged(
                        session, executor, body, settings
                    )
                    assert failed.phase == "failed_unchanged"
                async with sessions.begin() as session:
                    assert (
                        await handover_unchanged.acknowledge_unchanged(
                            session, executor, body, settings
                        )
                        == failed
                    )
            async with sessions.begin() as session:
                row = await session.scalar(select(UpgradeHandoverRow))
                assert row is not None and row.user_id == owner
                assert row.phase == ("transfer_consumed" if published else "failed_unchanged")
                assert row.active_slot == (1 if published else None)
                audits = (
                    await session.scalars(
                        select(AuditRow).where(
                            AuditRow.action == "upgrades.handover.failed_unchanged"
                        )
                    )
                ).all()
                assert len(audits) == (0 if published else 1)
            hold = authority.store.read("handover-hold.json", DeploymentHandoverHold)
            assert hold is not None and not hold.released
            authority.verify()
    finally:
        await engine.dispose()
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await owner_engine.dispose()
