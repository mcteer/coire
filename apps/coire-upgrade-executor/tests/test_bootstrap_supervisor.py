"""Readiness requires fresh physical observation, API acknowledgment and held ownership."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapHeartbeat,
    CoreBootstrapPhysicalObservation,
    CoreBootstrapSeed,
    CoreHandoverPreparation,
    DeploymentOwnership,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor import supervisor
from coire_upgrade_executor.ownership import BootstrapAuthority
from coire_upgrade_executor.passive import CandidatePassiveObserver


@pytest.mark.parametrize("fault", [None, "api", "physical", "application_owner"])
@pytest.mark.parametrize("requested", [False, True])
async def test_initial_supervisor_has_no_job_effects_and_clears_ready_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None, requested: bool
) -> None:
    seed, observation = prepared_bootstrap()
    settings = Settings(
        upgrades_enabled=True,
        upgrade_executor_token=SecretStr("coire_upgrade_private-test"),
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
        upgrade_poll_s=0.001,
    )
    root = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    root.write("initial-seed-request.json" if requested else "initial-seed.json", seed)
    stop = asyncio.Event()
    app = FastAPI()
    app.state.bootstrap_authority = None
    observed_ready = []
    prior = None
    if fault == "application_owner":
        plan = core_plan()
        prior = DeploymentOwnership(
            project="coire",
            owner_kind="executor",
            owner_identity="coire-upgrade-executor",
            executor_runtime_id=seed.executor_runtime_id,
            executor_container_id=seed.executor_container_id,
            boot_identity=host_boot_identity(),
            generation=1,
            job_id=plan.operation.job_id,
            operation_generation=plan.operation.generation,
            source_sha256=seed.source.manifest_sha256,
        )
        PrivateUpgradeStore(tmp_path / "coordination").write("ownership.json", prior)

    async def observe() -> CoreBootstrapPhysicalObservation:
        if fault in {"physical", "application_owner"}:
            stop.set()
        if fault == "physical":
            raise UpgradeUnavailable("physical observation unavailable")
        return observation

    async def heartbeat(body: CoreBootstrapHeartbeat) -> CoreBootstrapHeartbeat:
        assert body.physical.observed_at == observation.observed_at
        assert body.ownership.job_id is None
        assert app.state.bootstrap_authority is not None
        app.state.bootstrap_authority.verify()
        if fault == "api":
            stop.set()
            raise UpgradeUnavailable("API acknowledgment unavailable")

        def ready_then_stop() -> None:
            assert app.state.bootstrap_verified
            assert app.state.bootstrap_verified_at == observation.observed_at
            app.state.bootstrap_authority.verify()
            observed_ready.append(True)
            stop.set()

        asyncio.get_running_loop().call_soon(ready_then_stop)
        return body

    async def prepare(*_args: object) -> CoreBootstrapPhysicalObservation:
        assert not app.state.bootstrap_verified
        assert app.state.bootstrap_authority is None
        physical = await observe()
        root.write("initial-seed.json", seed, replace=False)
        return physical

    monkeypatch.setattr(supervisor, "prepare_initial_bootstrap", prepare)

    client = SimpleNamespace(
        bootstrap_heartbeat=AsyncMock(side_effect=heartbeat),
        close=AsyncMock(),
        claim=AsyncMock(return_value=None),
        claim_handover=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(supervisor, "linux_executor_platform", lambda: True)
    monkeypatch.setattr(supervisor, "verify_executor_namespace", Mock())
    monkeypatch.setattr(
        supervisor, "InitialBootstrapObserver", Mock(return_value=SimpleNamespace(observe=observe))
    )
    monkeypatch.setattr(supervisor, "ExecutorClient", Mock(return_value=client))
    if fault == "application_owner":

        def absent_plan(*_args: object) -> None:
            stop.set()
            raise UpgradeUnavailable("accepted plan absent")

        monkeypatch.setattr(supervisor, "cached_plan", absent_plan)
    async with asyncio.timeout(2):
        await supervisor.run_bootstrap_supervisor(app, settings, stop)
    assert observed_ready == ([True] if fault is None else [])
    assert not app.state.bootstrap_verified
    assert app.state.bootstrap_verified_at is None
    assert app.state.bootstrap_authority is None
    if fault in {"physical", "application_owner"}:
        client.bootstrap_heartbeat.assert_not_awaited()
    else:
        client.close.assert_awaited_once()
        store = PrivateUpgradeStore(tmp_path / "coordination")
        owner = store.read("ownership.json", DeploymentOwnership)
        assert owner is not None and owner.state == "committed" and owner.job_id is None
        assert not store.path("deployment.lease").exists()
    if prior is not None:
        assert (
            PrivateUpgradeStore(tmp_path / "coordination").read(
                "ownership.json", DeploymentOwnership
            )
            == prior
        )


async def test_disabled_supervisor_has_no_filesystem_or_transport_effects(tmp_path: Path) -> None:
    app = FastAPI()
    stop = asyncio.Event()
    task = asyncio.create_task(
        supervisor.run_bootstrap_supervisor(
            app, Settings(upgrade_state_dir=str(tmp_path / "absent")), stop
        )
    )
    try:
        await asyncio.sleep(0)
        assert not task.done()
        assert not app.state.bootstrap_verified
        assert not (tmp_path / "absent").exists()
    finally:
        stop.set()
        await task


async def test_passive_supervision_never_contacts_api_or_claims_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    app = FastAPI()
    app.state.bootstrap_authority = None
    app.state.bootstrap_verified = True
    observed = Mock(spec=CandidatePassiveObserver)
    observed.observe = AsyncMock()
    client_factory = Mock(side_effect=AssertionError("passive candidate must not contact API"))
    monkeypatch.setattr(supervisor, "ExecutorClient", client_factory)

    async def finish(_stop: asyncio.Event, _settings: Settings) -> None:
        assert app.state.passive_observer is observed
        assert not app.state.bootstrap_verified
        assert app.state.bootstrap_authority is None
        stop.set()

    monkeypatch.setattr(supervisor, "wait_poll", finish)
    await supervisor.supervise_passive_candidate(app, Settings(), stop, observed)
    assert app.state.passive_observer is None
    assert not app.state.bootstrap_verified
    client_factory.assert_not_called()
    observed.observe.assert_not_awaited()


async def test_candidate_startup_reads_retained_identity_before_any_incumbent_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_handover_creation_journal import preparation

    from coire_upgrade_executor.handover_creation import HandoverCreationJournal

    request = preparation()
    settings = Settings(
        upgrades_enabled=True,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    bootstrap.write("initial-seed.json", request.incumbent)
    bootstrap.write("handover-preparation.json", request)
    journal = HandoverCreationJournal(
        bootstrap.root / "handover" / str(request.generation), request
    )
    effect = journal.begin(now=request.prepared_at)
    journal.observe(effect, "a" * 64, observed_at=request.prepared_at, now=request.prepared_at)
    journal.candidate_seed()
    monkeypatch.setattr(supervisor, "linux_executor_platform", lambda: True)
    monkeypatch.setattr("coire_upgrade_executor.supervisor.platform.node", lambda: "a" * 12)
    observed = Mock(spec=CandidatePassiveObserver)
    candidate_factory = Mock(return_value=observed)
    monkeypatch.setattr(supervisor, "CandidatePassiveObserver", candidate_factory)
    incumbent_namespace = Mock(side_effect=AssertionError("candidate is not incumbent"))
    monkeypatch.setattr(supervisor, "verify_executor_namespace", incumbent_namespace)
    api_factory = Mock(side_effect=AssertionError("candidate cannot create API client"))
    monkeypatch.setattr(supervisor, "ExecutorClient", api_factory)
    app, stop = FastAPI(), asyncio.Event()

    async def finish(_stop: asyncio.Event, _settings: Settings) -> None:
        assert app.state.passive_observer is observed
        assert not app.state.bootstrap_verified
        stop.set()

    monkeypatch.setattr(supervisor, "wait_poll", finish)
    await supervisor.run_bootstrap_supervisor(app, settings, stop)
    candidate_factory.assert_called_once()
    incumbent_namespace.assert_not_called()
    api_factory.assert_not_called()
    assert not (tmp_path / "coordination").exists()
    assert bootstrap.read("selected-seed.json", type(request.incumbent)) is None


async def test_incumbent_supervisor_prepares_only_an_api_accepted_handover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_handover_creation_journal import preparation

    value = preparation()
    seed, observation = prepared_bootstrap(value.incumbent.source)
    seed = value.incumbent
    observation = observation.model_copy(update={"seed_sha256": seed.sha256()})
    settings = Settings(
        upgrades_enabled=True,
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
        upgrade_poll_s=0.001,
    )
    PrivateUpgradeStore(tmp_path / "state/bootstrap").write("initial-seed.json", seed)
    app, stop = FastAPI(), asyncio.Event()
    api = Mock()
    api.bootstrap_heartbeat = AsyncMock(side_effect=lambda body: body)
    api.claim = AsyncMock(return_value=None)
    api.claim_handover = AsyncMock(return_value=value)
    api.close = AsyncMock()
    monkeypatch.setattr(supervisor, "ExecutorClient", lambda settings: api)
    monkeypatch.setattr(supervisor, "linux_executor_platform", lambda: True)
    monkeypatch.setattr(supervisor, "verify_executor_namespace", Mock())
    monkeypatch.setattr(
        supervisor,
        "InitialBootstrapObserver",
        lambda *args: SimpleNamespace(observe=AsyncMock(return_value=observation)),
    )
    monkeypatch.setattr(supervisor, "qualify_selected_bootstrap", AsyncMock(return_value=seed))

    async def prepared(
        _settings: Settings,
        accepted: CoreHandoverPreparation,
        authority: BootstrapAuthority,
        physical: CoreBootstrapPhysicalObservation,
        _http: httpx.AsyncClient,
    ) -> CoreBootstrapSeed:
        assert accepted == value
        assert physical == observation
        assert app.state.bootstrap_authority is authority
        authority.verify()
        stop.set()
        return seed

    prepare = AsyncMock(side_effect=prepared)
    monkeypatch.setattr(supervisor, "prepare_passive_handover", prepare, raising=False)
    async with asyncio.timeout(2):
        await supervisor.run_bootstrap_supervisor(app, settings, stop)
    prepare.assert_awaited_once()
    api.claim_handover.assert_awaited_once_with(seed)
    assert app.state.bootstrap_authority is None
    assert not app.state.bootstrap_verified
