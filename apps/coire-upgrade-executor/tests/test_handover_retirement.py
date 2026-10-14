"""Exact predecessor retirement requires a real held writer; physical readiness is a fixture."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_handover_supervisor_epochs import epoch
from upgrade_bootstrap_fixtures import prepared_bootstrap

from coire_api.upgrades.handover_host_hold import publish_handover_hold
from coire_core.deploy import deployment_mutex, host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreHandoverRetirementIntent,
    CoreHandoverSummary,
    DeploymentHandoverHold,
    DeploymentOwnership,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import DockerRead
from coire_upgrade_executor.handover_retirement import (
    RetirementObserver,
    observe_candidate_ready,
    observe_retired_incumbent,
    retire_incumbent,
)
from coire_upgrade_executor.ownership import BootstrapAuthority, bootstrap_ownership


@pytest.mark.parametrize(
    "fault",
    [None, "unready", "changed_writer", "foreign_ack", "uncertain", "redispatch", "unreleased"],
)
async def test_retirement_never_stops_candidate_or_retries_uncertain_predecessor_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    import coire_upgrade_executor.handover_retirement as module

    settings, creation, bootstrap = epoch(tmp_path)
    prep, seed = creation.preparation, creation.retained_candidate_seed()
    _, physical = prepared_bootstrap(prep.incumbent.source)
    physical = physical.model_copy(update={"seed_sha256": prep.incumbent.sha256()})
    coordination = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    with bootstrap_ownership(
        coordination.root, prep.incumbent, physical, settings.upgrade_executor_id
    ):
        publish_handover_hold(settings, prep)
    original = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
    assert original is not None
    at = datetime.now(UTC)
    bootstrap.write(
        "bootstrap-handover.json",
        BootstrapHandover.model_validate(
            original.model_dump()
            | {
                "phase": "committed",
                "stability_started_at": at - timedelta(seconds=120),
                "deadline_at": at,
                "updated_at": at,
            }
        ),
    )
    bootstrap.write("selected-seed.json", seed)
    summary = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=prep.generation,
        phase="committed",
        preparation_sha256=prep.sha256(),
        candidate_release_id=prep.candidate_release.release_id,
        incumbent_image_id=prep.incumbent.executor_image_id,
        candidate_image_id=prep.candidate_image_id,
        prepared_at=prep.prepared_at,
        deadline_at=prep.deadline_at,
    )
    creation.store.write("commit-ack.json", summary)
    hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
    assert hold is not None
    coordination.write(
        "handover-hold.json", hold.model_copy(update={"released": fault != "unreleased"})
    )
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        stage="preparing",
        owner_identity=settings.upgrade_executor_id,
        executor_runtime_id=seed.executor_runtime_id,
        executor_container_id=seed.executor_container_id,
        boot_identity=host_boot_identity(),
        generation=prep.generation,
        source_sha256=seed.source.manifest_sha256,
    )
    monkeypatch.setattr(module, "verify_executor_namespace", lambda seed: None)
    observer = Mock()
    observer.proxy = httpx.URL("http://docker-socket-proxy:2375")
    observer.verify_container = AsyncMock()
    monkeypatch.setattr(module, "RetirementObserver", Mock(return_value=observer))

    async def ready(_observer: object) -> None:
        if fault == "unready":
            raise UpgradeUnavailable("candidate is not ready")
        if fault == "changed_writer":
            coordination.write(
                "ownership.json", owner.model_copy(update={"generation": owner.generation + 1})
            )

    monkeypatch.setattr(module, "observe_candidate_ready", AsyncMock(side_effect=ready))
    api = Mock(spec=ExecutorClient)
    api.handover_status = AsyncMock(
        return_value=summary.model_copy(update={"handover_id": uuid4()})
        if fault == "foreign_ack"
        else summary
    )
    effects: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/v1.46/containers/{prep.incumbent.executor_container_id}/stop"
        assert request.url.query == b"t=5"
        effects.append(request.url.path)
        return httpx.Response(503 if fault == "uncertain" else 204)

    with deployment_mutex(
        coordination,
        "coire",
        settings.upgrade_executor_id,
        handover=(prep.sha256(), prep.generation),
    ) as descriptor:
        coordination.write("ownership.json", owner)
        if fault == "redispatch":
            creation.store.write(
                "retirement-intent.json",
                CoreHandoverRetirementIntent(
                    preparation_sha256=prep.sha256(),
                    candidate_seed_sha256=seed.sha256(),
                    generation=prep.generation,
                    incumbent_container_id=prep.incumbent.executor_container_id,
                    candidate_container_id=seed.executor_container_id,
                    intent_at=at,
                    outcome="dispatched",
                ),
            )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport), trust_env=False
        ) as http:
            if fault:
                with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                    await retire_incumbent(settings, creation, api, http)
            else:
                await retire_incumbent(settings, creation, api, http)
                with pytest.raises(UpgradeUnavailable):
                    await retire_incumbent(settings, creation, api, http)
                observer.incumbent = prep.incumbent.executor_container_id
                stop_fault: str | None = "running"

                async def observed_stop(
                    container: str, **kwargs: bool
                ) -> tuple[datetime, datetime]:
                    assert container == prep.incumbent.executor_container_id
                    assert kwargs == {"require_healthy": False, "require_running": False}
                    read_at = datetime.now(UTC) - timedelta(
                        seconds=6 if stop_fault == "stale" else 0
                    )
                    observer.verified_reads[f"/containers/{container}/json"] = DockerRead(
                        {"State": {"Running": stop_fault == "running"}}, read_at
                    )
                    return read_at, read_at

                observer.verify_container = AsyncMock(side_effect=observed_stop)
                authority = BootstrapAuthority(coordination, descriptor, owner)
                for stop_fault in ("running", "stale"):
                    with pytest.raises(UpgradeUnavailable):
                        await observe_retired_incumbent(settings, creation, authority, http)
                    retained = creation.store.read(
                        "retirement-intent.json", CoreHandoverRetirementIntent
                    )
                    assert retained is not None and retained.outcome == "dispatched", stop_fault
                stop_fault = None
                await observe_retired_incumbent(settings, creation, authority, http)
                stopped = creation.store.read(
                    "retirement-intent.json", CoreHandoverRetirementIntent
                )
                assert stopped is not None and stopped.outcome == "stopped"
                assert stopped.stopped_observed_at is not None
                await observe_retired_incumbent(settings, creation, authority, http)
                assert (
                    creation.store.read("retirement-intent.json", CoreHandoverRetirementIntent)
                    == stopped
                )
        coordination.write("ownership.json", owner.model_copy(update={"state": "committed"}))
    assert len(effects) == (1 if fault in {None, "uncertain"} else 0)
    if fault in {"uncertain", "redispatch"}:
        intent = creation.store.read("retirement-intent.json", CoreHandoverRetirementIntent)
        assert intent is not None and intent.outcome == "dispatched"
    with pytest.raises(UpgradeConflict):
        module.retained_retirement_owner(settings, creation)


@pytest.mark.parametrize(
    "fault",
    [None, "public", "redirect", "service", "oversized", "moved", "restarted", "health_log"],
)
async def test_readiness_is_bound_to_exact_private_address_and_process(fault: str | None) -> None:
    observer = Mock(spec=RetirementObserver)
    observer.candidate = "a" * 64
    observer.settings = Settings(upgrade_executor_token=SecretStr("coire_upgrade_test-fixture"))
    observer.roles = {
        observer.candidate: (Mock(), {"networks": {"coire-internal": {"name": "coire-internal"}}})
    }
    observer.verified_reads = {}
    reads = 0
    calls = 0

    async def verify(container: str) -> tuple[datetime, datetime]:
        nonlocal reads
        assert container == observer.candidate
        address = "8.8.8.8" if fault == "public" else "172.24.0.7"
        if fault == "moved" and reads:
            address = "172.24.0.8"
        born = "2026-10-11T00:00:00Z"
        if fault == "restarted" and reads:
            born = "2026-10-11T01:00:00Z"
        observer.verified_reads[f"/containers/{container}/json"] = {
            "State": {
                "Running": True,
                "StartedAt": born,
                "Health": {
                    "Status": "healthy",
                    "Log": [reads] if fault == "health_log" else [],
                },
            },
            "NetworkSettings": {
                "Networks": {
                    "coire-internal": {
                        "NetworkID": "b" * 64,
                        "IPAddress": address,
                    }
                }
            },
        }
        reads += 1
        return datetime.now(UTC), datetime.now(UTC)

    observer.verify_container = AsyncMock(side_effect=verify)

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert str(request.url) == "http://172.24.0.7:8011/ready"
        assert request.method == "GET"
        if fault == "redirect":
            return httpx.Response(307, headers={"Location": "http://8.8.8.8/ready"})
        if fault == "oversized":
            return httpx.Response(200, content=b" " * 4097)
        return httpx.Response(
            200,
            json={
                "service": "foreign" if fault == "service" else "coire-upgrade-executor",
                "version": "fixture",
                "ready": True,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        observer.client = http
        if fault not in {None, "health_log"}:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await observe_candidate_ready(observer)
        else:
            await observe_candidate_ready(observer)
    assert calls == (0 if fault == "public" else 1)
