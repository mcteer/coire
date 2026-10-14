"""Commit needs the actual candidate kernel and its originally captured transfer epoch.

The continuous stability interval/physical reads are fixtures, not measured Docker acceptance.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_api.upgrades.handover_bootstrap import verify_selected_candidate
from coire_api.upgrades.handover_commit import verify_commit_candidate
from coire_api.upgrades.handover_host_hold import publish_handover_hold
from coire_core.deploy import deployment_mutex, host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    BootstrapStabilityObservation,
    CoreHandoverCommitIntent,
    CoreHandoverCommitRequest,
    CoreHandoverIncumbentObservation,
    CoreHandoverPreparation,
    CoreHandoverSummary,
    CoreHandoverTransferIntent,
    DeploymentHandoverHold,
    DeploymentOwnership,
    ExecutorPassiveReadiness,
    UpgradeWire,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.bootstrap import BootstrapJournal
from coire_upgrade_executor.handover_candidate_supervisor import verify_probe_continuity
from coire_upgrade_executor.handover_completion import (
    finalize_handover_commit,
    reconcile_committed_handover,
)
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_ownership import committed_candidate_ownership
from coire_upgrade_executor.handover_selection import publish_candidate_selection
from coire_upgrade_executor.ownership import BootstrapAuthority, bootstrap_ownership


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "phase",
        "proof",
        "generation",
        "process",
        "expired_proof",
        "early_claim",
        "released",
        "commit_interrupted",
    ],
)
def test_commit_cannot_replace_stability_with_elapsed_time_or_another_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import coire_api.upgrades.handover_commit as module

    incumbent, physical = prepared_bootstrap()
    now = datetime.now(UTC)
    prep = CoreHandoverPreparation(
        incumbent=incumbent,
        candidate_release=core_plan().target,
        candidate_runtime_id="candidate",
        candidate_image_id="sha256:" + "f" * 64,
        generation=2,
        initiator_user_id=incumbent.source.prepared_by,
        idempotency_key=uuid4(),
        prepared_at=now,
        deadline_at=now + timedelta(seconds=120),
    )
    settings = Settings(
        upgrades_enabled=True,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    bootstrap.write("handover-preparation.json", prep)
    creation = HandoverCreationJournal(bootstrap.root / "handover/2", prep)
    pending = creation.begin()
    creation.dispatch(pending)
    observed = datetime.now(UTC)
    creation.observe(pending, "a" * 64, observed_at=observed)
    seed = creation.candidate_seed()
    start = creation.begin_start()
    creation.dispatch_start(start)
    creation.observe_start(started_at=observed, observed_at=observed)
    passive = ExecutorPassiveReadiness(
        preparation_sha256=prep.sha256(),
        candidate_seed_sha256=seed.sha256(),
        generation=prep.generation,
        candidate_runtime_id=seed.executor_runtime_id,
        candidate_container_id=seed.executor_container_id,
        candidate_image_id=seed.executor_image_id,
        incumbent_runtime_id=incumbent.executor_runtime_id,
        incumbent_container_id=incumbent.executor_container_id,
        observed_at=observed,
    )
    creation.record_passive(passive)
    transfer = CoreHandoverTransferIntent(
        handover_id=uuid4(),
        intent_id=uuid4(),
        preparation_sha256=prep.sha256(),
        candidate_seed_sha256=seed.sha256(),
        passive_proof_sha256=passive.sha256(),
        generation=2,
        consumed_at=now,
        deadline_at=now + timedelta(seconds=30),
    )
    if fault == "early_claim":
        transfer = transfer.model_copy(update={"consumed_at": now + timedelta(seconds=20)})
    creation.store.write("transfer-intent.json", transfer)
    coordination = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    with bootstrap_ownership(coordination.root, incumbent, physical, settings.upgrade_executor_id):
        publish_handover_hold(settings, prep)
    with deployment_mutex(
        coordination, "coire", settings.upgrade_executor_id, handover=(prep.sha256(), 2)
    ) as descriptor:
        owner = DeploymentOwnership(
            project="coire",
            owner_kind="executor",
            stage="preparing",
            owner_identity=settings.upgrade_executor_id,
            executor_runtime_id=seed.executor_runtime_id,
            executor_container_id=seed.executor_container_id,
            boot_identity=host_boot_identity(),
            generation=3 if fault == "generation" else 2,
            source_sha256=seed.source.manifest_sha256,
        )
        coordination.write("ownership.json", owner)
        stability_started = datetime.now(UTC) + timedelta(seconds=1)
        proof_at = stability_started + timedelta(seconds=120)
        handover = BootstrapHandover(
            incumbent_id=incumbent.executor_runtime_id,
            incumbent_container_id=incumbent.executor_container_id,
            incumbent_image_id=incumbent.executor_image_id,
            candidate_id=seed.executor_runtime_id,
            candidate_container_id=seed.executor_container_id,
            candidate_image_id=seed.executor_image_id,
            generation=2,
            phase="claimed" if fault == "phase" else "stable",
            stability_started_at=stability_started,
            deadline_at=proof_at,
            updated_at=proof_at,
        )
        proof = BootstrapStabilityObservation(
            generation=2,
            incumbent_id=handover.incumbent_id,
            incumbent_container_id=handover.incumbent_container_id,
            incumbent_image_id=handover.incumbent_image_id,
            candidate_id=handover.candidate_id,
            candidate_container_id=handover.candidate_container_id,
            candidate_image_id=handover.candidate_image_id,
            kernel_boot_identity=host_boot_identity(),
            candidate_started_at=observed + timedelta(seconds=1)
            if fault == "process"
            else observed,
            observed_at=proof_at,
            incumbent_healthy=True,
            candidate_healthy=True,
            exclusive_writer_verified=True,
        )
        bootstrap.write("bootstrap-handover.json", handover)
        bootstrap.write("stability-observation.json", proof)
        request = CoreHandoverCommitRequest(preparation_sha256=prep.sha256(), stability=proof)
        if fault == "proof":
            request = request.model_copy(
                update={"stability": proof.model_copy(update={"kernel_boot_identity": "foreign"})}
            )

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return proof_at + timedelta(seconds=6 if fault == "expired_proof" else 0)

        monkeypatch.setattr(module, "datetime", Clock)
        if fault not in {None, "released", "commit_interrupted"}:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                verify_commit_candidate(settings, prep, transfer, request)
        else:
            assert verify_commit_candidate(settings, prep, transfer, request) == (seed, owner)
            import coire_upgrade_executor.handover_selection as selection_module

            monkeypatch.setattr(selection_module, "datetime", Clock)
            decision = CoreHandoverCommitIntent(
                handover_id=transfer.handover_id,
                decision_id=uuid4(),
                preparation_sha256=prep.sha256(),
                candidate_seed_sha256=seed.sha256(),
                stability_sha256=request.stability.sha256(),
                generation=2,
                decided_at=proof_at,
            )
            _, candidate_physical = prepared_bootstrap(seed.source)
            candidate_physical = candidate_physical.model_copy(
                update={
                    "seed_sha256": seed.sha256(),
                    "executor_runtime_id": seed.executor_runtime_id,
                    "executor_container_id": seed.executor_container_id,
                    "executor_image_id": seed.executor_image_id,
                    "executor_started_at": observed,
                }
            )
            incumbent_observation = CoreHandoverIncumbentObservation(
                preparation_sha256=prep.sha256(),
                incumbent_runtime_id=incumbent.executor_runtime_id,
                incumbent_container_id=incumbent.executor_container_id,
                incumbent_image_id=incumbent.executor_image_id,
                incumbent_started_at=prep.prepared_at - timedelta(seconds=1),
                observed_at=proof_at,
                incumbent_healthy=True,
            )
            authority = BootstrapAuthority(coordination, descriptor, owner)
            if fault is None:
                import coire_upgrade_executor.handover_candidate_supervisor as candidate_module

                monkeypatch.setattr(candidate_module, "datetime", Clock)
                # A claimed phase is 120 seconds old; its independent probe is
                # fresh. The healthy interval must not be aborted at ten seconds.
                bootstrap.write(
                    "bootstrap-handover.json",
                    handover.model_copy(
                        update={
                            "phase": "claimed",
                            "updated_at": stability_started,
                        }
                    ),
                )
                verify_probe_continuity(BootstrapJournal(bootstrap.root), creation, authority)
                for changed in (
                    {"observed_at": proof_at - timedelta(seconds=11)},
                    {"observed_at": proof_at + timedelta(seconds=1)},
                    {"generation": 3},
                    {"kernel_boot_identity": "foreign"},
                    {"candidate_started_at": observed + timedelta(seconds=1)},
                ):
                    bootstrap.write("stability-observation.json", proof.model_copy(update=changed))
                    with pytest.raises(UpgradeUnavailable):
                        verify_probe_continuity(
                            BootstrapJournal(bootstrap.root), creation, authority
                        )
                bootstrap.write("bootstrap-handover.json", handover)
                bootstrap.write("stability-observation.json", proof)
            with pytest.raises(UpgradeConflict):
                publish_candidate_selection(
                    bootstrap,
                    creation,
                    authority,
                    candidate_physical,
                    incumbent_observation,
                    request,
                    decision.model_copy(update={"candidate_seed_sha256": "f" * 64}),
                )
            assert not bootstrap.path("selected-seed.json").exists()
            assert (
                publish_candidate_selection(
                    bootstrap,
                    creation,
                    authority,
                    candidate_physical,
                    incumbent_observation,
                    request,
                    decision,
                )
                == seed
            )
            heartbeat = authority.heartbeat(seed, candidate_physical)
            verify_selected_candidate(settings, prep, decision, transfer, heartbeat)
            hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
            assert hold is not None and not hold.released
            assert bootstrap.read("bootstrap-handover.json", BootstrapHandover) == handover
            if fault != "released":
                import coire_upgrade_executor.handover_completion as completion_module

                monkeypatch.setattr(completion_module, "datetime", Clock)
                summary = CoreHandoverSummary(
                    handover_id=decision.handover_id,
                    project="coire",
                    generation=2,
                    phase="committed",
                    preparation_sha256=prep.sha256(),
                    candidate_release_id=prep.candidate_release.release_id,
                    incumbent_image_id=incumbent.executor_image_id,
                    candidate_image_id=seed.executor_image_id,
                    prepared_at=prep.prepared_at,
                    deadline_at=prep.deadline_at,
                )
                with pytest.raises(UpgradeConflict):
                    finalize_handover_commit(
                        bootstrap,
                        creation,
                        authority,
                        candidate_physical,
                        incumbent_observation,
                        summary.model_copy(update={"phase": "restored"}),
                    )
                assert not creation.store.path("commit-ack.json").exists()
                if fault == "commit_interrupted":
                    write = coordination.write

                    def failed_release(
                        key: str, value: UpgradeWire, *, replace: bool = True
                    ) -> None:
                        if key == "handover-hold.json":
                            raise OSError("injected commit hold release interruption")
                        write(key, value, replace=replace)

                    with monkeypatch.context() as interruption:
                        interruption.setattr(coordination, "write", failed_release)
                        with pytest.raises(OSError):
                            finalize_handover_commit(
                                bootstrap,
                                creation,
                                authority,
                                candidate_physical,
                                incumbent_observation,
                                summary,
                            )
                    assert creation.store.read("commit-ack.json", CoreHandoverSummary) == summary
                    current = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
                    assert current is not None and current.phase == "committed"
                    assert coordination.read("handover-hold.json", DeploymentHandoverHold) == hold
                assert (
                    finalize_handover_commit(
                        bootstrap,
                        creation,
                        authority,
                        candidate_physical,
                        incumbent_observation,
                        summary,
                    )
                    == seed
                )
                assert (
                    finalize_handover_commit(
                        bootstrap,
                        creation,
                        authority,
                        candidate_physical,
                        incumbent_observation,
                        summary,
                    )
                    == seed
                )
                released = coordination.read("handover-hold.json", DeploymentHandoverHold)
                assert released is not None and released.released
        coordination.write("ownership.json", owner.model_copy(update={"state": "committed"}))
    if fault == "released":
        with pytest.raises(UpgradeConflict):
            verify_commit_candidate(settings, prep, transfer, request)
    if fault is None:
        import coire_upgrade_executor.handover_ownership as ownership_module

        monkeypatch.setattr(ownership_module, "verify_executor_namespace", lambda seed: None)
        # Simulate a crash after SQL acknowledgment but before private phase
        # publication and hold release. Reboot captures a fresh generation;
        # its original completed stability epoch remains unchanged.
        bootstrap.write("bootstrap-handover.json", handover)
        assert hold is not None
        coordination.write("handover-hold.json", hold)
        original_epoch = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
        with committed_candidate_ownership(
            settings, creation, candidate_physical, summary
        ) as rebooted:
            assert rebooted.claim.generation == owner.generation + 1
            rebooted.verify()
            assert bootstrap.read("bootstrap-handover.json", BootstrapHandover) == original_epoch
            with (
                pytest.raises(UpgradeConflict),
                committed_candidate_ownership(settings, creation, candidate_physical, summary),
            ):
                pytest.fail("a held candidate writer was captured twice")
            qualified = rebooted.heartbeat(seed, candidate_physical)
            with pytest.raises(UpgradeConflict):
                reconcile_committed_handover(
                    bootstrap,
                    creation,
                    rebooted,
                    qualified,
                    summary.model_copy(update={"phase": "restoring"}),
                )
            assert (
                reconcile_committed_handover(bootstrap, creation, rebooted, qualified, summary)
                == seed
            )
            terminal = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
            assert terminal is not None and terminal.phase == "committed"
            assert terminal.stability_started_at == handover.stability_started_at
            assert terminal.deadline_at == handover.deadline_at
            closed = coordination.read("handover-hold.json", DeploymentHandoverHold)
            assert closed is not None and closed.released
        with (
            pytest.raises(UpgradeConflict),
            committed_candidate_ownership(
                settings,
                creation,
                candidate_physical,
                summary.model_copy(update={"phase": "committing"}),
            ),
        ):
            pytest.fail("an uncommitted candidate received a new bootstrap epoch")
