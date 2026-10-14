"""A cached accepted plan resumes only its exact fenced independent owner."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import DeploymentOperationHold, DeploymentOwnership
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor import operation_supervisor
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.executor import CoreExecutor
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import ExecutorAuthority


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("fault", [None, "hold", "incarnation", "other_job"])
async def test_operation_supervisor_reuses_plan_and_retains_uncertain_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restart: bool, fault: str | None
) -> None:
    plan = core_plan()
    seed, proof = prepared_bootstrap(plan.source)
    settings = Settings(
        upgrade_state_dir=str(tmp_path / "state"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    coordination = PrivateUpgradeStore(tmp_path / "coordination")
    prior = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity=settings.upgrade_executor_id,
        executor_runtime_id=seed.executor_runtime_id,
        executor_container_id="f" * 64 if fault == "incarnation" else seed.executor_container_id,
        boot_identity=host_boot_identity(),
        generation=1,
        state="active" if restart else "committed",
        stage="preparing",
        job_id=("01M6MHS0000000000000000003" if fault == "other_job" else plan.operation.job_id)
        if restart
        else None,
        operation_generation=plan.operation.generation if restart else None,
        source_sha256=seed.source.manifest_sha256,
    )
    coordination.write("ownership.json", prior)
    if fault != "hold":
        coordination.write(
            "operation-hold.json",
            DeploymentOperationHold(
                project="coire",
                job_id=plan.operation.job_id,
                generation=plan.operation.generation,
                plan_sha256=plan.authority_sha256(),
                source_sha256=plan.operation.source_sha256,
                created_at=proof.observed_at,
            ),
        )
    operation_supervisor.cache_accepted_plan(settings, plan)
    source_read = AsyncMock(return_value=proof.source)
    own_read = AsyncMock(return_value=(proof.observed_at, proof.executor_started_at))
    monkeypatch.setattr(
        operation_supervisor,
        "InitialBootstrapObserver",
        Mock(
            return_value=SimpleNamespace(
                source=SimpleNamespace(observe=source_read),
                executor=SimpleNamespace(verify_container=own_read),
            )
        ),
    )
    monkeypatch.setattr(
        operation_supervisor, "UpgradeDocker", Mock(return_value=SimpleNamespace(source_names={}))
    )
    client = cast(ExecutorClient, SimpleNamespace(bind=Mock()))
    stop = asyncio.Event()

    def engine(
        journal: ExecutorJournal,
        _docker: UpgradeDocker,
        bound_client: ExecutorClient,
        authority: ExecutorAuthority,
        _prepared: Path,
    ) -> CoreExecutor:
        return cast(
            CoreExecutor, SimpleNamespace(journal=journal, client=bound_client, authority=authority)
        )

    async def work(executor: CoreExecutor, _settings: Settings, stopped: asyncio.Event) -> None:
        executor.authority.verify()
        assert executor.authority.claim.job_id == plan.operation.job_id
        assert executor.journal.plan.authority_sha256() == plan.authority_sha256()
        stopped.set()

    worker = AsyncMock(side_effect=work)
    monkeypatch.setattr(operation_supervisor, "CoreExecutor", engine)
    monkeypatch.setattr(operation_supervisor, "run_owned_operation", worker)
    async with httpx.AsyncClient(trust_env=False) as http:
        if fault == "other_job" and not restart:
            fault = None
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await operation_supervisor.supervise_operation(
                    settings, seed, plan, client, http, stop
                )
            worker.assert_not_awaited()
            assert coordination.read("ownership.json", DeploymentOwnership) == prior
        else:
            await operation_supervisor.supervise_operation(settings, seed, plan, client, http, stop)
            worker.assert_awaited_once()
            retained = coordination.read("ownership.json", DeploymentOwnership)
            assert retained is not None and retained.state == "active"
            assert retained.generation == prior.generation + 1
            assert retained.job_id == plan.operation.job_id
            assert coordination.path("deployment.lease").exists()
            assert source_read.await_count == int(restart)
            assert own_read.await_count == 1
