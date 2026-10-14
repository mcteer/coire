"""Real SQL/held-kernel transfer authority; Docker physical observations remain fixtures."""

import asyncio
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
from coire_api.upgrades import handover_commit, handover_completion, handover_restoration
from coire_api.upgrades.handover_authority import accept_handover, claim_handover
from coire_api.upgrades.handover_host_hold import publish_handover_hold
from coire_api.upgrades.handover_transfer import consume_handover_transfer
from coire_api.upgrades.persistence import register_release
from coire_core.deploy import deployment_mutex, host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeForbidden
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import (
    BootstrapHandover,
    BootstrapStabilityObservation,
    CoreBootstrapHeartbeat,
    CoreBootstrapSeed,
    CoreHandoverCandidateStart,
    CoreHandoverCommitRequest,
    CoreHandoverCommittedRequest,
    CoreHandoverCreation,
    CoreHandoverIncumbentObservation,
    CoreHandoverPreparation,
    CoreHandoverRecoveryIntent,
    CoreHandoverRecoveryRequest,
    CoreHandoverRestoreRequest,
    CoreHandoverStoppedObservation,
    CoreHandoverTransferRequest,
    DeploymentOwnership,
    ExecutorPassiveReadiness,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.ownership import BootstrapAuthority, bootstrap_ownership


@pytest.mark.parametrize(
    "revoked,complete,race",
    [
        (False, False, None),
        (True, False, None),
        (False, True, None),
        (False, True, "commit"),
        (False, True, "restore"),
    ],
)
async def test_transfer_rechecks_actor_and_replays_single_captured_intent(
    training_postgres_url: str,
    tmp_path: Path,
    revoked: bool,
    complete: bool,
    race: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = f"upgrade_transfer_{uuid4().hex}"
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
            upgrade_coordination_dir=str(tmp_path / "coordination"),
            upgrade_state_dir=str(tmp_path / "state"),
        )
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@example.test",
                    display_name="Transfer",
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
            now = datetime.now(UTC)
            preparation = CoreHandoverPreparation(
                incumbent=seed,
                candidate_release=candidate,
                candidate_runtime_id="candidate-executor",
                candidate_image_id="sha256:" + "f" * 64,
                generation=authority.claim.generation + 1,
                initiator_user_id=owner,
                idempotency_key=uuid4(),
                prepared_at=now,
                deadline_at=now + timedelta(seconds=120),
            )
            async with sessions.begin() as session:
                await accept_handover(session, admin, preparation, settings)
                await claim_handover(session, executor, seed, settings)
            publish_handover_hold(settings, preparation)
            root = PrivateUpgradeStore(tmp_path / "state/bootstrap")
            root.write("handover-preparation.json", preparation)
            journal = PrivateUpgradeStore(root.root / "handover" / str(preparation.generation))
            journal.write("preparation.json", preparation)
            candidate_seed = CoreBootstrapSeed(
                source=seed.source,
                executor_release=candidate,
                source_container_ids=seed.source_container_ids,
                executor_runtime_id=preparation.candidate_runtime_id,
                executor_container_id="a" * 64,
                executor_image_id=preparation.candidate_image_id,
                generation=preparation.generation,
                prepared_at=now,
            )
            journal.write("candidate-seed.json", candidate_seed)
            journal.write(
                "creation.json",
                CoreHandoverCreation(
                    preparation_sha256=preparation.sha256(),
                    candidate_name=preparation.candidate_name,
                    candidate_image_id=preparation.candidate_image_id,
                    generation=preparation.generation,
                    outcome="observed",
                    candidate_container_id=candidate_seed.executor_container_id,
                    observed_at=now,
                ),
            )
            journal.write(
                "candidate-start.json",
                CoreHandoverCandidateStart(
                    preparation_sha256=preparation.sha256(),
                    candidate_seed_sha256=candidate_seed.sha256(),
                    candidate_container_id=candidate_seed.executor_container_id,
                    generation=preparation.generation,
                    deadline_at=preparation.deadline_at,
                    outcome="observed",
                    started_at=now,
                    observed_at=now,
                ),
            )
            proof = ExecutorPassiveReadiness(
                preparation_sha256=preparation.sha256(),
                candidate_seed_sha256=candidate_seed.sha256(),
                generation=preparation.generation,
                candidate_runtime_id=candidate_seed.executor_runtime_id,
                candidate_container_id=candidate_seed.executor_container_id,
                candidate_image_id=candidate_seed.executor_image_id,
                incumbent_runtime_id=seed.executor_runtime_id,
                incumbent_container_id=seed.executor_container_id,
                observed_at=now,
            )
            journal.write("passive-proof.json", proof)
            root.write(
                "bootstrap-handover.json",
                BootstrapHandover(
                    incumbent_id=seed.executor_runtime_id,
                    candidate_id=candidate_seed.executor_runtime_id,
                    incumbent_container_id=seed.executor_container_id,
                    candidate_container_id=candidate_seed.executor_container_id,
                    incumbent_image_id=seed.executor_image_id,
                    candidate_image_id=candidate_seed.executor_image_id,
                    generation=preparation.generation,
                    phase="verified",
                    updated_at=now,
                    deadline_at=preparation.deadline_at,
                ),
            )
            request = CoreHandoverTransferRequest(
                preparation_sha256=preparation.sha256(), passive_proof_sha256=proof.sha256()
            )
            if revoked:
                async with sessions.begin() as session:
                    user = await session.get(UserRow, owner)
                    assert user is not None
                    user.role = UserRole.USER
                with pytest.raises(UpgradeForbidden):
                    async with sessions.begin() as session:
                        await consume_handover_transfer(session, executor, request, settings)
            else:
                async with sessions.begin() as session:
                    intent = await consume_handover_transfer(session, executor, request, settings)
                async with sessions.begin() as session:
                    assert (
                        await consume_handover_transfer(session, executor, request, settings)
                        == intent
                    )
                assert intent.deadline_at <= preparation.deadline_at
                assert 0 < (intent.deadline_at - intent.consumed_at).total_seconds() <= 30
            async with sessions.begin() as session:
                row = await session.scalar(select(UpgradeHandoverRow))
                assert row is not None
                assert row.phase == ("passive" if revoked else "transfer_consumed")
                assert (row.transfer_payload is None) == revoked
                audits = (
                    await session.scalars(
                        select(AuditRow).where(
                            AuditRow.action == "upgrades.handover.transfer_consumed"
                        )
                    )
                ).all()
                assert len(audits) == (0 if revoked else 1)
            authority.verify()
            if not revoked:
                # SQL decision replay uses a mocked independent physical boundary;
                # actual candidate kernel/configuration checks are separate tests.
                stability = BootstrapStabilityObservation(
                    generation=preparation.generation,
                    incumbent_id=seed.executor_runtime_id,
                    incumbent_container_id=seed.executor_container_id,
                    incumbent_image_id=seed.executor_image_id,
                    candidate_id=candidate_seed.executor_runtime_id,
                    candidate_container_id=candidate_seed.executor_container_id,
                    candidate_image_id=candidate_seed.executor_image_id,
                    kernel_boot_identity=authority.claim.boot_identity,
                    candidate_started_at=now,
                    observed_at=datetime.now(UTC),
                    incumbent_healthy=True,
                    candidate_healthy=True,
                    exclusive_writer_verified=True,
                )
                commit_request = CoreHandoverCommitRequest(
                    preparation_sha256=preparation.sha256(), stability=stability
                )
                candidate_owner = authority.claim.model_copy(
                    update={
                        "executor_runtime_id": candidate_seed.executor_runtime_id,
                        "executor_container_id": candidate_seed.executor_container_id,
                        "generation": preparation.generation,
                    }
                )

                def independently_stable(settings_arg, prep_arg, transfer_arg, request_arg):
                    assert settings_arg == settings and prep_arg == preparation
                    assert transfer_arg == intent and request_arg == commit_request
                    return candidate_seed, candidate_owner

                monkeypatch.setattr(
                    handover_commit, "verify_commit_candidate", independently_stable
                )
                async with sessions.begin() as session:
                    decision = await handover_commit.decide_handover_commit(
                        session, executor, commit_request, settings
                    )
                async with sessions.begin() as session:
                    assert (
                        await handover_commit.decide_handover_commit(
                            session, executor, commit_request, settings
                        )
                        == decision
                    )
                    row = await session.scalar(select(UpgradeHandoverRow))
                    assert row is not None and row.phase == "committing" and row.active_slot == 1
                    audits = (
                        await session.scalars(
                            select(AuditRow).where(
                                AuditRow.action == "upgrades.handover.commit_decided"
                            )
                        )
                    ).all()
                    assert len(audits) == 1
                authority.verify()
                if complete:
                    candidate_observed_at = datetime.now(UTC)
                    candidate_physical = physical.model_copy(
                        update={
                            "seed_sha256": candidate_seed.sha256(),
                            "executor_runtime_id": candidate_seed.executor_runtime_id,
                            "executor_container_id": candidate_seed.executor_container_id,
                            "executor_image_id": candidate_seed.executor_image_id,
                            "executor_started_at": now,
                            "observed_at": candidate_observed_at,
                            "source": physical.source.model_copy(
                                update={"observed_at": candidate_observed_at}
                            ),
                        }
                    )
                    candidate_heartbeat = CoreBootstrapHeartbeat(
                        executor_id=executor.subject,
                        seed=candidate_seed,
                        physical=candidate_physical,
                        ownership=candidate_owner,
                        lease=heartbeat.lease,
                        exclusive_writer_verified=True,
                    )
                    incumbent_proof = CoreHandoverIncumbentObservation(
                        preparation_sha256=preparation.sha256(),
                        incumbent_runtime_id=seed.executor_runtime_id,
                        incumbent_container_id=seed.executor_container_id,
                        incumbent_image_id=seed.executor_image_id,
                        incumbent_started_at=physical.executor_started_at,
                        observed_at=datetime.now(UTC),
                        incumbent_healthy=True,
                    )
                    committed_request = CoreHandoverCommittedRequest(
                        preparation_sha256=preparation.sha256(),
                        candidate=candidate_heartbeat,
                        incumbent=incumbent_proof,
                    )

                    def selected(settings_arg, prep_arg, decision_arg, transfer_arg, body_arg):
                        assert (settings_arg, prep_arg, decision_arg, transfer_arg, body_arg) == (
                            settings,
                            preparation,
                            decision,
                            intent,
                            candidate_heartbeat,
                        )

                    async def qualified_candidate(
                        session_arg, principal_arg, body_arg, settings_arg
                    ):
                        assert principal_arg == executor and body_arg == candidate_heartbeat
                        owner_row = await session_arg.get(
                            UpgradeExecutorRow, "coire", with_for_update=True
                        )
                        assert owner_row is not None
                        owner_row.image_id = candidate_seed.executor_image_id
                        owner_row.bootstrap_payload = body_arg.model_dump(mode="json")
                        owner_row.heartbeat_at = body_arg.physical.observed_at
                        owner_row.generation = body_arg.ownership.generation
                        return body_arg

                    monkeypatch.setattr(handover_completion, "verify_selected_candidate", selected)
                    monkeypatch.setattr(
                        handover_completion, "retain_bootstrap_heartbeat", qualified_candidate
                    )
                    if race is not None:
                        # Exercise the actual PostgreSQL advisory/row locks with
                        # both requests in flight. Physical validation remains
                        # the same explicit fixture boundary as the tests above.
                        recovery_request = CoreHandoverRecoveryRequest(
                            recovery=CoreHandoverRecoveryIntent(
                                preparation_sha256=preparation.sha256(),
                                candidate_seed_sha256=candidate_seed.sha256(),
                                generation=preparation.generation,
                                incumbent_container_id=seed.executor_container_id,
                                candidate_container_id=candidate_seed.executor_container_id,
                                detected_at=now,
                                deadline_at=now + timedelta(seconds=300),
                            )
                        )
                        monkeypatch.setattr(
                            handover_restoration, "verify_recovery_request", lambda *_args: None
                        )
                        locked, attempted, proceed = (
                            asyncio.Event(),
                            asyncio.Event(),
                            asyncio.Event(),
                        )
                        original_lock = handover_completion.operation_lock

                        async def coordinated_lock(session_arg):
                            task = asyncio.current_task()
                            assert task is not None
                            if task.get_name() == "handover-loser":
                                attempted.set()
                            await original_lock(session_arg)
                            if task.get_name() == "handover-winner":
                                locked.set()
                                await proceed.wait()

                        monkeypatch.setattr(handover_completion, "operation_lock", coordinated_lock)
                        monkeypatch.setattr(
                            handover_restoration, "operation_lock", coordinated_lock
                        )

                        async def compete(which):
                            try:
                                async with sessions.begin() as race_session:
                                    if which == "commit":
                                        return (
                                            await handover_completion.acknowledge_handover_commit(
                                                race_session, executor, committed_request, settings
                                            )
                                        )
                                    return await handover_restoration.begin_restoration(
                                        race_session, executor, recovery_request, settings
                                    )
                            except UpgradeConflict as error:
                                return error

                        winner = asyncio.create_task(compete(race), name="handover-winner")
                        async with asyncio.timeout(10):
                            await locked.wait()
                            loser = asyncio.create_task(
                                compete("restore" if race == "commit" else "commit"),
                                name="handover-loser",
                            )
                            await attempted.wait()
                            proceed.set()
                            outcomes = await asyncio.gather(winner, loser)
                        assert outcomes[0].phase == (
                            "committed" if race == "commit" else "restoring"
                        )
                        assert isinstance(outcomes[1], UpgradeConflict)
                        async with sessions.begin() as race_session:
                            raced_row = await race_session.scalar(select(UpgradeHandoverRow))
                            assert raced_row is not None
                            assert raced_row.phase == outcomes[0].phase
                            assert raced_row.active_slot == (None if race == "commit" else 1)
                            race_audits = (
                                await race_session.scalars(
                                    select(AuditRow).where(
                                        AuditRow.action.in_(
                                            [
                                                "upgrades.handover.committed",
                                                "upgrades.handover.restoring",
                                            ]
                                        )
                                    )
                                )
                            ).all()
                            assert len(race_audits) == 1
                        return
                    async with sessions.begin() as session:
                        completed = await handover_completion.acknowledge_handover_commit(
                            session, executor, committed_request, settings
                        )
                        assert completed.phase == "committed"
                    async with sessions.begin() as session:
                        assert (
                            await handover_completion.acknowledge_handover_commit(
                                session, executor, committed_request, settings
                            )
                            == completed
                        )
                        row = await session.scalar(select(UpgradeHandoverRow))
                        assert (
                            row is not None and row.active_slot is None and row.phase == "committed"
                        )
                        audits = (
                            await session.scalars(
                                select(AuditRow).where(
                                    AuditRow.action == "upgrades.handover.committed"
                                )
                            )
                        ).all()
                        assert len(audits) == 1
                    restore_after_commit = CoreHandoverRestoreRequest(
                        preparation_sha256=preparation.sha256(),
                        incumbent=heartbeat,
                        candidate=CoreHandoverStoppedObservation(
                            preparation_sha256=preparation.sha256(),
                            candidate_seed_sha256=candidate_seed.sha256(),
                            candidate_container_id=candidate_seed.executor_container_id,
                            generation=preparation.generation,
                            observed_at=datetime.now(UTC),
                        ),
                    )
                    with pytest.raises(UpgradeConflict):
                        async with sessions.begin() as session:
                            await handover_restoration.acknowledge_restoration(
                                session, executor, restore_after_commit, settings
                            )
                    return
        # CPU/SQL boundary: the real shared kernel and restoration verification
        # run here; Linux-only heartbeat/catalog qualification is a separate gate.
        handover = root.read("bootstrap-handover.json", BootstrapHandover)
        assert handover is not None
        root.write("bootstrap-handover.json", handover.model_copy(update={"phase": "restoring"}))
        recovery = CoreHandoverRecoveryIntent(
            preparation_sha256=preparation.sha256(),
            candidate_seed_sha256=candidate_seed.sha256(),
            generation=preparation.generation,
            incumbent_container_id=seed.executor_container_id,
            candidate_container_id=candidate_seed.executor_container_id,
            detected_at=now,
            deadline_at=now + timedelta(seconds=300),
            outcome="stopped",
            stopped_observed_at=now,
        )
        journal.write("recovery-intent.json", recovery)
        async with sessions.begin() as session:
            restoring = await handover_restoration.begin_restoration(
                session, executor, CoreHandoverRecoveryRequest(recovery=recovery), settings
            )
            assert restoring.phase == "restoring"
        async with sessions.begin() as session:
            assert (
                await handover_restoration.begin_restoration(
                    session, executor, CoreHandoverRecoveryRequest(recovery=recovery), settings
                )
                == restoring
            )
            audits = (
                await session.scalars(
                    select(AuditRow).where(AuditRow.action == "upgrades.handover.restoring")
                )
            ).all()
            assert len(audits) == 1
        if not revoked:
            async with sessions.begin() as session:
                with pytest.raises(UpgradeConflict):
                    await handover_commit.decide_handover_commit(
                        session, executor, commit_request, settings
                    )

        async def qualified_heartbeat(session, principal, body, settings):
            assert principal == executor and body.seed == seed
            owner_row = await session.get(UpgradeExecutorRow, "coire", with_for_update=True)
            assert owner_row is not None and owner_row.image_id == seed.executor_image_id
            owner_row.bootstrap_payload = body.model_dump(mode="json")
            owner_row.generation = body.ownership.generation
            owner_row.heartbeat_at = body.physical.observed_at
            owner_row.bootstrap_ready = True
            return body

        monkeypatch.setattr(handover_restoration, "retain_bootstrap_heartbeat", qualified_heartbeat)
        coordination = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
        with deployment_mutex(
            coordination,
            "coire",
            executor.subject,
            handover=(preparation.sha256(), preparation.generation),
        ) as descriptor:
            claim = DeploymentOwnership(
                project="coire",
                owner_kind="executor",
                stage="preparing",
                owner_identity=executor.subject,
                executor_runtime_id=seed.executor_runtime_id,
                executor_container_id=seed.executor_container_id,
                boot_identity=host_boot_identity(),
                generation=preparation.generation + 1,
                source_sha256=seed.source.manifest_sha256,
            )
            coordination.write("ownership.json", claim)
            authority_restored = BootstrapAuthority(coordination, descriptor, claim)
            # Capture a new fixture observation for restoration, never rewrite the
            # original transfer/passive evidence or any captured deadline.
            _, fresh = prepared_bootstrap(seed.source)
            fresh = fresh.model_copy(update={"seed_sha256": seed.sha256()})
            stopped = CoreHandoverStoppedObservation(
                preparation_sha256=preparation.sha256(),
                candidate_seed_sha256=candidate_seed.sha256(),
                candidate_container_id=candidate_seed.executor_container_id,
                generation=preparation.generation,
                observed_at=fresh.observed_at,
            )
            journal.write("candidate-stopped-observation.json", stopped)
            restore = CoreHandoverRestoreRequest(
                preparation_sha256=preparation.sha256(),
                incumbent=authority_restored.heartbeat(seed, fresh),
                candidate=stopped,
            )
            async with sessions.begin() as session:
                restored = await handover_restoration.acknowledge_restoration(
                    session, executor, restore, settings
                )
                assert restored.phase == "restored"
            async with sessions.begin() as session:
                assert (
                    await handover_restoration.acknowledge_restoration(
                        session, executor, restore, settings
                    )
                    == restored
                )
                row = await session.scalar(select(UpgradeHandoverRow))
                assert row is not None and row.active_slot is None and row.phase == "restored"
                audits = (
                    await session.scalars(
                        select(AuditRow).where(AuditRow.action == "upgrades.handover.restored")
                    )
                ).all()
                assert len(audits) == 1
            coordination.write("ownership.json", claim.model_copy(update={"state": "committed"}))
    finally:
        await engine.dispose()
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await owner_engine.dispose()
