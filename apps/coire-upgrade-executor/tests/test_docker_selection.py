"""A later rollout reads the positively selected Source, including earlier candidate names."""

from datetime import UTC, datetime

import httpx
import pytest
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict, UpgradeForbidden
from coire_core.models.upgrades import (
    CoreRuntimeSelection,
    CoreSelectedService,
    UpgradeDockerContainer,
)
from coire_core.settings import Settings
from coire_upgrade_executor.docker import UpgradeDocker


@pytest.mark.parametrize("fault", [None, "manifest", "image", "container", "origin", "canonical"])
async def test_retained_selection_limits_reads_to_exact_source_container_and_origin(
    fault: str | None,
) -> None:
    plan = core_plan()
    service = plan.source.manifest.services[0]
    assert plan.source.manifest.compose_sha256 is not None
    assert plan.source.manifest.host_release_key is not None
    prior_job = "01ARZ3NDEKTSV4RRFFQ69G5FA0"
    name = f"coire-upgrade-{prior_job.lower()}-{service.service}"
    now = datetime.now(UTC)
    selected = CoreRuntimeSelection(
        project="coire",
        release_id=plan.source.release_id,
        manifest_sha256="f" * 64 if fault == "manifest" else plan.source.manifest_sha256,
        compose_sha256=plan.source.manifest.compose_sha256,
        host_release_key=plan.source.manifest.host_release_key,
        owner_identity="executor-source",
        generation=3,
        selected_at=now,
        services=(
            CoreSelectedService(
                container=UpgradeDockerContainer(
                    container_id="a" * 64,
                    name=name,
                    project="coire",
                    service=service.service,
                    image_id="sha256:" + "f" * 64 if fault == "image" else service.image_id,
                    observed_at=now,
                    running=True,
                    health="healthy",
                    exit_code=0,
                    network_ids={"coire_coire-internal": "b" * 64},
                ),
                origin_job_id=prior_job,
                origin_generation=2,
                origin_plan_sha256="c" * 64,
            ),
        ),
    )
    reads: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        reads.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/v1.46/containers/{name}/json"
        return httpx.Response(
            200,
            json={
                "Id": "f" * 64 if fault == "container" else "a" * 64,
                "Name": "/" + name,
                "Image": service.image_id,
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "coire",
                        "com.docker.compose.service": service.service,
                        "coire.upgrade.job_id": prior_job,
                        "coire.upgrade.generation": "99" if fault == "origin" else "2",
                        "coire.upgrade.plan_sha256": "c" * 64,
                    }
                },
                "State": {"Running": True, "ExitCode": 0, "Health": {"Status": "healthy"}},
                "NetworkSettings": {
                    "Networks": {
                        "coire_coire-internal": {
                            "NetworkID": "b" * 64,
                            "Aliases": [service.service],
                        }
                    }
                },
                "Mounts": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        if fault in {"manifest", "image"}:
            with pytest.raises(UpgradeConflict):
                UpgradeDocker(Settings(), plan, client=http, source_selection=selected)
            assert not reads
            return
        docker = UpgradeDocker(Settings(), plan, client=http, source_selection=selected)
        if fault == "canonical":
            with pytest.raises(UpgradeForbidden):
                await docker.inspect_source(f"coire-{service.service}-1")
            assert not reads
        elif fault in {"container", "origin"}:
            with pytest.raises(UpgradeConflict):
                await docker.inspect_source(name)
        else:
            observed = await docker.inspect_source(name)
            assert observed.container_id == selected.services[0].container.container_id
            assert observed.name == name
