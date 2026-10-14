"""First-install verification needs trusted Source bytes, never a fabricated plan."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_handover_observer import prepared_executor
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict, UpgradeForbidden
from coire_core.models.upgrades import UpgradeComposeService
from coire_core.settings import Settings
from coire_upgrade_executor.compose import _translate_execution
from coire_upgrade_executor.source_observer import SourceObserver


@pytest.mark.parametrize("fault", [None, "source_unhealthy", "stopped", "foreign", "writable"])
async def test_initial_bootstrap_checks_source_and_its_starting_executor(
    tmp_path: Path, fault: str | None
) -> None:
    from coire_core.models.upgrades import CoreBootstrapSeed
    from coire_upgrade_executor.source_observer import InitialBootstrapObserver

    image = "sha256:" + "e" * 64
    source, executor_role = prepared_executor(tmp_path, core_plan().source, image)
    source_service = source.manifest.services[0]
    source_role = UpgradeComposeService.model_validate(source_service.configuration)
    seed = CoreBootstrapSeed(
        source=source,
        source_container_ids={source_service.service: "c" * 64},
        executor_runtime_id="initial-executor",
        executor_container_id="d" * 64,
        executor_image_id=image,
        generation=1,
        prepared_at=datetime.now(UTC),
    )
    reads = []
    started = datetime.now(UTC) - timedelta(seconds=10)

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        reads.append(request.url.path)
        if "/images/" in request.url.path:
            requested_image = request.url.path.removeprefix("/v1.46/images/").removesuffix("/json")
            return httpx.Response(
                200,
                json={
                    "Id": requested_image,
                    "Os": "linux",
                    "Architecture": "arm64",
                    "Config": {"Cmd": ["python", "-m", "fixture"]},
                },
            )
        container = request.url.path.removeprefix("/v1.46/containers/").removesuffix("/json")
        own = container == seed.executor_container_id
        role = executor_role if own else source_role
        service = "coire-upgrade-executor" if own else source_service.service
        networks = {name: str(index + 1) * 64 for index, name in enumerate(role.networks)}
        payload = _translate_execution(
            role,
            role,
            role=service,
            project="coire",
            network_ids=networks,
            mount_bindings=(),
            dependency_states={},
        )
        return httpx.Response(
            200,
            json={
                "Id": container,
                "Image": role.image,
                "Config": {
                    "User": payload["User"],
                    "Env": payload["Env"],
                    "Entrypoint": None,
                    "Cmd": ["python", "-m", "fixture"],
                    "WorkingDir": "",
                    "StopTimeout": 5,
                    "Healthcheck": payload["Healthcheck"],
                    "ExposedPorts": payload["ExposedPorts"],
                    "Labels": payload["Labels"]
                    | (
                        {"com.docker.compose.project": "foreign"}
                        if own and fault == "foreign"
                        else {}
                    ),
                },
                "HostConfig": payload["HostConfig"]
                | ({"ReadonlyRootfs": False} if own and fault == "writable" else {}),
                "Mounts": [],
                "NetworkSettings": {
                    "Networks": {
                        name: {
                            "NetworkID": identity,
                            "Aliases": [payload["Labels"]["com.docker.compose.service"]],
                        }
                        for name, identity in networks.items()
                    }
                },
                "State": {
                    "Running": not (own and fault == "stopped"),
                    "StartedAt": started.isoformat(),
                    "Health": {
                        "Status": "starting"
                        if own
                        else "unhealthy"
                        if fault == "source_unhealthy"
                        else "healthy"
                    },
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), trust_env=False
    ) as client:
        observer = InitialBootstrapObserver(Settings(), tmp_path / "prepared", seed, client)
        if fault:
            with pytest.raises(UpgradeConflict):
                await observer.observe()
        else:
            proof = await observer.observe()
            assert proof.seed_sha256 == seed.sha256()
            assert proof.source.container_ids == seed.source_container_ids
            assert proof.executor_container_id == seed.executor_container_id
            assert proof.executor_started_at == started
            assert not hasattr(proof, "bootstrap_ready")
            expected_reads = [
                f"/v1.46/containers/{'c' * 64}/json",
                f"/v1.46/images/{source_role.image}/json",
                f"/v1.46/containers/{'d' * 64}/json",
                f"/v1.46/images/{image}/json",
            ]
            # Source and executor observations overlap, but every physical read
            # remains mandatory and each image follows its own container read.
            assert sorted(reads) == sorted(expected_reads)
            assert reads.index(expected_reads[0]) < reads.index(expected_reads[1])
            assert reads.index(expected_reads[2]) < reads.index(expected_reads[3])
            from coire_core.upgrade_journal import PrivateUpgradeStore
            from coire_upgrade_executor.bootstrap import prepare_initial_bootstrap

            settings = Settings(
                upgrade_prepared_dir=str(tmp_path / "prepared"),
                upgrade_state_dir=str(tmp_path / "state"),
            )
            first = await prepare_initial_bootstrap(settings, seed, client)
            replay = await prepare_initial_bootstrap(settings, seed, client)
            assert first.seed_sha256 == replay.seed_sha256 == seed.sha256()
            retained = PrivateUpgradeStore(tmp_path / "state/bootstrap")
            assert retained.read("initial-seed.json", CoreBootstrapSeed) == seed
            assert not retained.path("bootstrap-handover.json").exists()
            with pytest.raises(UpgradeConflict):
                await prepare_initial_bootstrap(
                    settings, seed.model_copy(update={"generation": 2}), client
                )
            assert retained.read("initial-seed.json", CoreBootstrapSeed) == seed


@pytest.mark.parametrize("fault", [None, "missing", "foreign", "mutate", "health"])
async def test_source_bootstrap_reads_only_its_complete_prepared_runtime(
    tmp_path: Path, fault: str | None
) -> None:
    source, _ = prepared_executor(tmp_path, core_plan().source, "sha256:" + "e" * 64)
    declared = source.manifest.services[0]
    role = UpgradeComposeService.model_validate(declared.configuration)
    container = "c" * 64
    reads: list[str] = []
    started = datetime.now(UTC) - timedelta(seconds=10)

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        reads.append(request.url.path)
        if "/images/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "Id": role.image,
                    "Os": "linux",
                    "Architecture": "arm64",
                    "Config": {"Cmd": ["python", "-m", "fixture"]},
                },
            )
        networks = {name: str(index + 1) * 64 for index, name in enumerate(role.networks)}
        payload = _translate_execution(
            role,
            role,
            role=declared.service,
            project="coire",
            network_ids=networks,
            mount_bindings=(),
            dependency_states={},
        )
        return httpx.Response(
            200,
            json={
                "Id": container,
                "Image": role.image,
                "Config": {
                    "User": payload["User"],
                    "Env": payload["Env"],
                    "Entrypoint": None,
                    "Cmd": ["python", "-m", "fixture"],
                    "WorkingDir": "",
                    "StopTimeout": 5,
                    "Healthcheck": payload["Healthcheck"],
                    "ExposedPorts": payload["ExposedPorts"],
                    "Labels": payload["Labels"]
                    | ({"com.docker.compose.project": "foreign"} if fault == "foreign" else {}),
                },
                "HostConfig": payload["HostConfig"],
                "Mounts": [],
                "NetworkSettings": {
                    "Networks": {
                        name: {
                            "NetworkID": identity,
                            "Aliases": [payload["Labels"]["com.docker.compose.service"]],
                        }
                        for name, identity in networks.items()
                    }
                },
                "State": {
                    "Running": True,
                    "StartedAt": started.isoformat(),
                    "Health": {"Status": "unhealthy" if fault == "health" else "healthy"},
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), trust_env=False
    ) as client:
        if fault == "missing":
            with pytest.raises(UpgradeConflict):
                SourceObserver(Settings(), tmp_path / "prepared", source, {}, client)
            assert not reads
            return
        observer = SourceObserver(
            Settings(), tmp_path / "prepared", source, {declared.service: container}, client
        )
        if fault == "mutate":
            with pytest.raises(UpgradeForbidden):
                await observer.read("POST", "/containers/create")
            assert not reads
        elif fault in {"health", "foreign"}:
            with pytest.raises(UpgradeConflict):
                await observer.observe()
        else:
            observed = await observer.observe()
            assert 0 <= (datetime.now(UTC) - observed.observed_at).total_seconds() < 5
            assert observed.source_sha256 == source.manifest_sha256
            assert observed.container_ids == {declared.service: container}
            assert observed.image_ids == {declared.service: role.image}
            assert reads == [
                f"/v1.46/containers/{container}/json",
                f"/v1.46/images/{role.image}/json",
            ]
