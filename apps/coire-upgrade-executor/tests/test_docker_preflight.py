"""The whole cohort must pass read-only verification before Source is stopped."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from test_docker_candidate import candidate_plan

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import DeploymentOwnership
from coire_core.settings import Settings
from coire_upgrade_executor.compose import translate_service
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership


@pytest.mark.parametrize(
    "fault",
    [None, "slow_target", "source_config", "target_platform", "unhealthy", "offline_dependencies"],
)
async def test_preflight_never_mutates_docker(
    tmp_path: Path, fault: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = candidate_plan(tmp_path)
    source = plan.source.manifest.services[0]
    target = plan.target.manifest.services[0]
    payload = translate_service(
        source, source, project="coire", network_ids={"coire-internal": "b" * 64}
    )
    body = {
        "Id": "c" * 64,
        "Name": "/coire-coire-api-1",
        "Image": source.image_id,
        "Config": {
            "User": payload["User"],
            "Env": payload["Env"],
            "Entrypoint": None,
            "Cmd": ["python", "-m", "coire_api"],
            "WorkingDir": "",
            "StopTimeout": 5,
            "Healthcheck": payload["Healthcheck"],
            "ExposedPorts": payload["ExposedPorts"],
            "Labels": payload["Labels"],
        },
        "HostConfig": payload["HostConfig"] | {"NetworkMode": "coire_coire-internal"},
        "NetworkSettings": {
            "Networks": {
                "coire_coire-internal": {
                    "NetworkID": "b" * 64,
                    "Aliases": [payload["Labels"]["com.docker.compose.service"]],
                }
            }
        },
        "Mounts": [],
        "State": {
            "Running": fault != "offline_dependencies",
            "ExitCode": 0,
            "Health": {"Status": "unhealthy" if fault == "unhealthy" else "healthy"},
        },
    }
    if fault == "source_config":
        body["HostConfig"]["ReadonlyRootfs"] = False
    reads: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        reads.append(request.url.path)
        if request.url.path == "/v1.46/containers/coire-coire-api-1/json":
            return httpx.Response(200, json=body)
        image = request.url.path.removeprefix("/v1.46/images/").removesuffix("/json")
        assert image in {source.image_id, target.image_id}
        if fault == "slow_target" and image == target.image_id:
            # Target inspection must not age the Source snapshot out of its
            # real, unchanged five-second freshness window.
            await asyncio.sleep(4.1)
        elif fault == "slow_target" and image == source.image_id:
            await asyncio.sleep(1.2)
        return httpx.Response(
            200,
            json={
                "Id": image,
                "Os": "linux",
                "Architecture": "amd64"
                if fault == "target_platform" and image == target.image_id
                else "arm64",
                "Config": {"Env": [], "Cmd": ["python", "-m", "coire_api"], "Entrypoint": None},
            },
        )

    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="executor-source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.operation.source_sha256,
    )
    journal = ExecutorJournal(tmp_path / "journal", plan)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), trust_env=False
    ) as client:
        docker = UpgradeDocker(Settings(), plan, client=client)
        if fault == "offline_dependencies":
            dependencies = AsyncMock(side_effect=UpgradeUnavailable("API dependency unavailable"))
            monkeypatch.setattr(docker, "fresh_dependencies", dependencies)
            with pytest.raises(UpgradeUnavailable):
                await docker.inspect_verified_source(
                    "coire-coire-api-1", tmp_path / "prepared", dependency_states={}
                )
            observed = await docker.inspect_verified_source(
                "coire-coire-api-1",
                tmp_path / "prepared",
                dependency_states={},
                require_dependency_readiness=False,
            )
            assert not observed.running
            dependencies.assert_awaited_once()
            assert reads
            return
        if fault == "source_config":
            with pytest.raises(UpgradeConflict):
                await docker.inspect_verified_source(
                    "coire-coire-api-1",
                    tmp_path / "prepared",
                    dependency_states={},
                    require_dependency_readiness=False,
                )
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
            if fault not in {None, "slow_target"}:
                with pytest.raises((UpgradeConflict, ValueError)):
                    await docker.preflight_cohort(
                        journal,
                        authority,
                        prepared_root=tmp_path / "prepared",
                        dependency_states={},
                    )
                assert not journal.store.path("source-snapshot.json").exists()
            else:
                snapshot = await docker.preflight_cohort(
                    journal, authority, prepared_root=tmp_path / "prepared", dependency_states={}
                )
                assert snapshot == journal.source_snapshot()
                assert snapshot.services[0].container_id == "c" * 64
    assert reads
