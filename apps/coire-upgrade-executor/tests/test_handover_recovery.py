"""Independent stop fault boundary; composed Docker acceptance remains a separate gate."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from test_handover_recovery_journal import recovery
from upgrade_bootstrap_fixtures import prepared_bootstrap

from coire_api.upgrades.handover_host_hold import publish_handover_hold
from coire_api.upgrades.handover_restoration import verify_restoration
from coire_core.deploy import deployment_mutex
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreHandoverIncumbentObservation,
    CoreHandoverRestoreRequest,
    CoreHandoverSummary,
    DeploymentHandoverHold,
    DeploymentOwnership,
    UpgradeWire,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.docker import DockerRead
from coire_upgrade_executor.handover_recovery import HandoverRecovery, RecoveryObserver
from coire_upgrade_executor.handover_restoration import finalize_restoration
from coire_upgrade_executor.ownership import bootstrap_ownership
from coire_upgrade_executor.source_observer import InitialBootstrapObserver


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "lost_stopped",
        "lost_running",
        "expired",
        "foreign_owner",
        "released_hold",
        "phase",
        "candidate_owner",
        "held_candidate",
        "restarted_before_reclaim",
        "finalization_interrupted",
        "selected_candidate",
        "foreign_selected",
    ],
)
async def test_stop_is_fixed_uncertain_dispatch_is_not_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import coire_upgrade_executor.handover_recovery as module

    monkeypatch.setattr(module, "verify_executor_namespace", lambda seed: None)
    bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    journal = recovery(tmp_path / "state/bootstrap/handover/2", aged=fault == "expired")
    prep = journal.creation.preparation
    seed = journal.creation.retained_candidate_seed()
    _, physical = prepared_bootstrap(prep.incumbent.source)
    physical = physical.model_copy(update={"seed_sha256": prep.incumbent.sha256()})
    settings = Settings(
        upgrades_enabled=True,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    coordination = PrivateUpgradeStore(tmp_path / "coordination")
    bootstrap.write("handover-preparation.json", prep)
    handover = BootstrapHandover(
        incumbent_id=prep.incumbent.executor_runtime_id,
        incumbent_container_id=prep.incumbent.executor_container_id,
        incumbent_image_id=prep.incumbent.executor_image_id,
        candidate_id=seed.executor_runtime_id,
        candidate_container_id=seed.executor_container_id,
        candidate_image_id=seed.executor_image_id,
        generation=prep.generation,
        phase="restoring",
        updated_at=prep.prepared_at,
        deadline_at=prep.deadline_at,
    )
    bootstrap.write("bootstrap-handover.json", handover)
    if fault in {"selected_candidate", "foreign_selected"}:
        bootstrap.write(
            "selected-seed.json",
            seed
            if fault == "selected_candidate"
            else seed.model_copy(update={"executor_runtime_id": "foreign-selected"}),
        )
    with bootstrap_ownership(
        Path(settings.upgrade_coordination_dir),
        prep.incumbent,
        physical,
        settings.upgrade_executor_id,
    ):
        publish_handover_hold(settings, prep)
    if fault == "foreign_owner":
        owner = coordination.read("ownership.json", DeploymentOwnership)
        assert owner is not None
        coordination.write(
            "ownership.json", owner.model_copy(update={"executor_runtime_id": "foreign"})
        )
    elif fault == "released_hold":
        hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
        assert hold is not None
        coordination.write("handover-hold.json", hold.model_copy(update={"released": True}))
    elif fault == "phase":
        bootstrap.write(
            "bootstrap-handover.json", handover.model_copy(update={"phase": "verified"})
        )
    elif fault == "expired":
        journal.begin(now=prep.prepared_at)

    posts: list[httpx.Request] = []
    running = True

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal running
        posts.append(request)
        assert request.method == "POST"
        assert request.url.path == f"/v1.46/containers/{seed.executor_container_id}/stop"
        assert request.url.query == b"t=5"
        if fault != "lost_running":
            running = False
        if fault in {"lost_stopped", "lost_running"}:
            raise httpx.ReadTimeout("injected lost response")
        return httpx.Response(204)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), trust_env=False
    ) as client:
        observer = RecoveryObserver.__new__(RecoveryObserver)
        observer.handover = handover
        observer.client, observer.proxy = client, httpx.URL(settings.upgrade_proxy_url)
        observer.incumbent, observer.candidate = (
            handover.incumbent_container_id,
            handover.candidate_container_id,
        )
        monkeypatch.setattr(
            observer,
            "observe_incumbent",
            AsyncMock(
                return_value=CoreHandoverIncumbentObservation(
                    preparation_sha256=prep.sha256(),
                    incumbent_runtime_id=prep.incumbent.executor_runtime_id,
                    incumbent_container_id=prep.incumbent.executor_container_id,
                    incumbent_image_id=prep.incumbent.executor_image_id,
                    incumbent_started_at=prep.prepared_at,
                    observed_at=datetime.now(UTC),
                    incumbent_healthy=True,
                )
            ),
        )

        async def verified(
            identity: str, *, require_healthy: bool, require_running: bool
        ) -> tuple[datetime, datetime]:
            assert identity == seed.executor_container_id
            assert require_healthy is False and require_running is False
            now = datetime.now(UTC)
            observer.verified_reads[f"/containers/{identity}/json"] = DockerRead(
                {"State": {"Running": running}}, now
            )
            return now, prep.prepared_at

        monkeypatch.setattr(observer, "verify_container", verified)
        if fault in {"foreign_owner", "released_hold", "phase"}:
            with pytest.raises(UpgradeConflict):
                HandoverRecovery(settings, journal, observer, bootstrap, coordination)
            assert not posts
            return
        executor = HandoverRecovery(settings, journal, observer, bootstrap, coordination)
        if fault in {"lost_stopped", "lost_running", "expired"}:
            with pytest.raises(UpgradeUnavailable):
                await executor.stop_candidate()
            if fault == "lost_stopped":
                result = await executor.stop_candidate()
                assert result.outcome == "stopped"
            else:
                with pytest.raises(UpgradeUnavailable):
                    await executor.stop_candidate()
        else:
            result = await executor.stop_candidate()
            assert result.outcome == "stopped"
            assert await executor.stop_candidate() == result
        assert len(posts) == (0 if fault == "expired" else 1)
        if fault in {
            None,
            "lost_stopped",
            "candidate_owner",
            "held_candidate",
            "restarted_before_reclaim",
            "finalization_interrupted",
            "selected_candidate",
            "foreign_selected",
        }:
            source_observer = Mock(spec=InitialBootstrapObserver)
            source_observer.observe = AsyncMock(return_value=physical)
            if fault in {"candidate_owner", "held_candidate"}:
                previous = coordination.read("ownership.json", DeploymentOwnership)
                assert previous is not None
                coordination.write(
                    "ownership.json",
                    previous.model_copy(
                        update={
                            "state": "active",
                            "generation": prep.generation,
                            "executor_runtime_id": seed.executor_runtime_id,
                            "executor_container_id": seed.executor_container_id,
                        }
                    ),
                )
            if fault == "held_candidate":
                previous = coordination.read("ownership.json", DeploymentOwnership)
                assert previous is not None
                with (
                    deployment_mutex(
                        coordination,
                        settings.upgrade_project,
                        settings.upgrade_executor_id,
                        reconciled_prior=previous,
                        handover=(prep.sha256(), prep.generation),
                    ),
                    pytest.raises(UpgradeConflict),
                ):
                    async with executor.incumbent_ownership(source_observer):
                        pytest.fail("Incumbent took an already held kernel")
                assert coordination.read("ownership.json", DeploymentOwnership) == previous
                return
            if fault == "restarted_before_reclaim":
                running = True
                previous = coordination.read("ownership.json", DeploymentOwnership)
                with pytest.raises(UpgradeUnavailable):
                    async with executor.incumbent_ownership(source_observer):
                        pytest.fail("Incumbent reclaimed a restarted candidate")
                assert coordination.read("ownership.json", DeploymentOwnership) == previous
                source_observer.observe.assert_not_awaited()
                return
            async with executor.incumbent_ownership(source_observer) as authority:
                authority.verify()
                assert authority.claim.generation == prep.generation + 1
                assert authority.claim.executor_container_id == prep.incumbent.executor_container_id
                assert authority.heartbeat(prep.incumbent, physical).ownership == authority.claim
                stopped = await executor.observe_stopped()
                restored_request = CoreHandoverRestoreRequest(
                    preparation_sha256=prep.sha256(),
                    incumbent=authority.heartbeat(prep.incumbent, physical),
                    candidate=stopped,
                )
                verify_restoration(settings, prep, restored_request)
                with pytest.raises(UpgradeConflict):
                    verify_restoration(
                        settings,
                        prep,
                        restored_request.model_copy(
                            update={
                                "candidate": stopped.model_copy(
                                    update={"candidate_container_id": "b" * 64}
                                )
                            }
                        ),
                    )
                hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
                assert hold is not None and not hold.released
                summary = CoreHandoverSummary(
                    handover_id=uuid4(),
                    project=settings.upgrade_project,
                    generation=prep.generation,
                    phase="restored",
                    preparation_sha256=prep.sha256(),
                    candidate_release_id=prep.candidate_release.release_id,
                    incumbent_image_id=prep.incumbent.executor_image_id,
                    candidate_image_id=prep.candidate_image_id,
                    prepared_at=prep.prepared_at,
                    deadline_at=prep.deadline_at,
                )
                with pytest.raises(UpgradeConflict):
                    finalize_restoration(
                        executor,
                        authority,
                        physical,
                        summary.model_copy(update={"phase": "committed"}),
                    )
                assert coordination.read("handover-hold.json", DeploymentHandoverHold) == hold
                if fault == "foreign_selected":
                    selected = bootstrap.path("selected-seed.json").read_bytes()
                    with pytest.raises(UpgradeConflict):
                        finalize_restoration(executor, authority, physical, summary)
                    assert bootstrap.path("selected-seed.json").read_bytes() == selected
                    assert coordination.read("handover-hold.json", DeploymentHandoverHold) == hold
                    return
                if fault == "finalization_interrupted":
                    write = coordination.write

                    def failed_release(
                        key: str, value: UpgradeWire, *, replace: bool = True
                    ) -> None:
                        if key == "handover-hold.json":
                            raise OSError("injected interrupted hold release")
                        return write(key, value, replace=replace)

                    with monkeypatch.context() as interruption:
                        interruption.setattr(coordination, "write", failed_release)
                        with pytest.raises(OSError):
                            finalize_restoration(executor, authority, physical, summary)
                    assert coordination.read("handover-hold.json", DeploymentHandoverHold) == hold
                    partial = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
                    assert partial is not None and partial.phase == "restored"
                    assert (
                        journal.store.read("restoration-ack.json", CoreHandoverSummary) == summary
                    )
                    executor.verify_retained()
                assert (
                    finalize_restoration(executor, authority, physical, summary) == prep.incumbent
                )
                assert (
                    finalize_restoration(executor, authority, physical, summary) == prep.incumbent
                )
                released = coordination.read("handover-hold.json", DeploymentHandoverHold)
                assert released is not None and released.released
                restored = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
                assert restored is not None and restored.phase == "restored"
                if fault == "selected_candidate":
                    assert bootstrap.read("selected-seed.json", type(seed)) == prep.incumbent
            owner = coordination.read("ownership.json", DeploymentOwnership)
            assert owner is not None and owner.state == "committed"
            with pytest.raises(UpgradeConflict):
                verify_restoration(settings, prep, restored_request)
