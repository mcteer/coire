"""Bootstrap observations need real kernel ownership and exact prepared Docker reads."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_bootstrap_journal import handover
from test_compose_translation import service
from upgrade_fixtures import core_plan

from coire_core.deploy import deployment_mutex, host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import DeploymentOwnership, UpgradeComposeService, UpgradeRelease
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import publish_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.bootstrap import BootstrapJournal
from coire_upgrade_executor.compose import _translate_execution
from coire_upgrade_executor.handover import (
    HandoverObserver,
    advance_handover_stability,
    start_handover_stability,
    verify_candidate_writer,
)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "project",
        "owner",
        "generation",
        "operation",
        "released",
        "runtime",
        "container",
        "missing_incarnation",
    ],
)
def test_candidate_writer_requires_actual_held_kernel_lock(
    tmp_path: Path, fault: str | None
) -> None:
    value = handover(datetime.now(UTC))
    store = PrivateUpgradeStore(tmp_path / "coordination")
    owner = DeploymentOwnership(
        project="other" if fault == "project" else "coire",
        owner_kind="executor",
        owner_identity="foreign" if fault == "owner" else "coire-upgrade-executor",
        executor_runtime_id=(
            None
            if fault == "missing_incarnation"
            else "foreign-runtime"
            if fault == "runtime"
            else value.candidate_id
        ),
        executor_container_id=(
            None
            if fault == "missing_incarnation"
            else "e" * 64
            if fault == "container"
            else value.candidate_container_id
        ),
        boot_identity=host_boot_identity(),
        generation=2 if fault == "generation" else value.generation,
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV" if fault == "operation" else None,
        operation_generation=1 if fault == "operation" else None,
    )
    with deployment_mutex(store, owner.project, owner.owner_identity):
        store.write("ownership.json", owner)
        if fault not in {None, "released"}:
            with pytest.raises(UpgradeConflict):
                verify_candidate_writer(store, value, "coire")
        else:
            assert verify_candidate_writer(store, value, "coire") == host_boot_identity()
    with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
        verify_candidate_writer(store, value, "coire")


def prepared_executor(
    root: Path, original: UpgradeRelease, image: str
) -> tuple[UpgradeRelease, UpgradeComposeService]:
    configuration = {
        "image": image,
        "user": "65532:65532",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "unless-stopped",
        "pids_limit": 32,
        "mem_limit": "256m",
        "networks": {name: {} for name in ("coire-docker", "coire-internal", "coire-telemetry")},
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        "healthcheck": {"test": ["CMD", "python", "-m", "coire_upgrade_executor", "--healthcheck"]},
    }
    role = UpgradeComposeService.model_validate(configuration)
    application = service(image=original.manifest.services[0].image_id)
    document = {
        "name": "coire",
        "services": {
            application.service: application.configuration,
            "coire-upgrade-executor": configuration,
        },
        "networks": {name: {"name": name} for name in role.networks},
        "volumes": {},
        "secrets": {},
    }
    content = json.dumps(document).encode()
    sha = hashlib.sha256(content).hexdigest()
    manifest = original.manifest.model_copy(
        update={
            "services": (application,),
            "compose_sha256": sha,
            "artifact_sha256s": {"compose.json": sha},
            "staged_bytes": len(content),
        }
    )
    release = original.model_copy(
        update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
    )
    artifact = root / (release.prepared_artifact_id + ".json")
    artifact.write_bytes(content)
    publish_prepared_release(root / "prepared", release, {"compose.json": artifact})
    return release, role


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "image",
        "identity",
        "project",
        "health",
        "writable",
        "privileged",
        "platform",
        "path",
        "stopped",
        "invalid_running",
    ],
)
async def test_handover_observation_verifies_prepared_configuration_without_mutation(
    tmp_path: Path, fault: str | None
) -> None:
    now = datetime.now(UTC)
    value = handover(now)
    source, source_role = prepared_executor(tmp_path, core_plan().source, value.incumbent_image_id)
    target, target_role = prepared_executor(tmp_path, core_plan().target, value.candidate_image_id)
    roles = {"c" * 64: source_role, "d" * 64: target_role}
    reads: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        reads.append(request)
        assert request.method == "GET"
        path = request.url.path
        if "/images/" in path:
            image = path.removeprefix("/v1.46/images/").removesuffix("/json")
            assert image in {value.incumbent_image_id, value.candidate_image_id}
            return httpx.Response(
                200,
                json={
                    "Id": image,
                    "Os": "linux",
                    "Architecture": "amd64" if fault == "platform" else "arm64",
                    "Config": {"Cmd": ["python", "-m", "coire_upgrade_executor"]},
                },
            )
        container = path.removeprefix("/v1.46/containers/").removesuffix("/json")
        role = roles[container]
        networks = {name: str(index + 1) * 64 for index, name in enumerate(role.networks)}
        payload = _translate_execution(
            role,
            role,
            role="coire-upgrade-executor",
            project="coire",
            network_ids=networks,
            mount_bindings=(),
            dependency_states={},
        )
        labels = payload["Labels"] | (
            {"com.docker.compose.project": "foreign"} if fault == "project" else {}
        )
        host = (
            payload["HostConfig"]
            | ({"ReadonlyRootfs": False} if fault == "writable" else {})
            | ({"Privileged": True} if fault == "privileged" else {})
        )
        return httpx.Response(
            200,
            json={
                "Id": "e" * 64 if fault == "identity" else container,
                "Image": "sha256:" + "f" * 64 if fault == "image" else role.image,
                "Config": {
                    "User": payload["User"],
                    "Env": payload["Env"],
                    "Entrypoint": None,
                    "Cmd": ["python", "-m", "coire_upgrade_executor"],
                    "WorkingDir": "",
                    "StopTimeout": 5,
                    "Healthcheck": payload["Healthcheck"],
                    "ExposedPorts": payload["ExposedPorts"],
                    "Labels": labels,
                },
                "HostConfig": host,
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
                    "Running": False
                    if fault == "stopped"
                    else "false"
                    if fault == "invalid_running"
                    else True,
                    "Health": {"Status": "unhealthy" if fault == "health" else "healthy"},
                    "StartedAt": (now - timedelta(seconds=1)).isoformat(),
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), trust_env=False, follow_redirects=False
    ) as client:
        observer = HandoverObserver(
            Settings(), tmp_path / "prepared", source, target, value, "c" * 64, "d" * 64, client
        )
        if fault == "path":
            with pytest.raises(UpgradeForbidden):
                await observer.read("POST", "/containers/create")
            assert not reads
        elif fault is not None:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                await observer.verify_container("d" * 64)
            if fault == "stopped":
                at, started = await observer.verify_container(
                    "d" * 64, require_healthy=False, require_running=False
                )
                assert started == now - timedelta(seconds=1) and started < at
            elif fault == "invalid_running":
                with pytest.raises(UpgradeConflict):
                    await observer.verify_container(
                        "d" * 64, require_healthy=False, require_running=False
                    )
        else:
            at, started = await observer.verify_container("d" * 64)
            assert started == now - timedelta(seconds=1) and started < at
            coordination = PrivateUpgradeStore(tmp_path / "coordination")
            with deployment_mutex(coordination, "coire", "coire-upgrade-executor"):
                coordination.write(
                    "ownership.json",
                    DeploymentOwnership(
                        project="coire",
                        owner_kind="executor",
                        owner_identity="coire-upgrade-executor",
                        executor_runtime_id=value.candidate_id,
                        executor_container_id=value.candidate_container_id,
                        boot_identity=host_boot_identity(),
                        generation=value.generation,
                    ),
                )
                result = await observer.observe(coordination)
                assert result.candidate_container_id == "d" * 64
                assert result.incumbent_container_id == "c" * 64
                assert result.exclusive_writer_verified
                journal = BootstrapJournal(tmp_path / "bootstrap")
                initial = journal.begin(value, now=now)
                verified = journal.advance(initial, "verified", now=now)
                claimed = await start_handover_stability(journal, observer, coordination)
                assert claimed.stability_started_at is not None
                assert claimed.stability_started_at >= verified.updated_at
                assert claimed.deadline_at == claimed.stability_started_at + timedelta(seconds=120)
                assert await start_handover_stability(journal, observer, coordination) == claimed
                assert await advance_handover_stability(journal, observer, coordination) == claimed
            before = len(reads)
            restoring = await advance_handover_stability(journal, observer, coordination)
            assert restoring.phase == "restoring"
            assert restoring.deadline_at == claimed.deadline_at
            assert restoring.incumbent_id == value.incumbent_id
            assert len(reads) == before


async def test_stability_start_failure_retains_original_deadline_and_incumbent(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock, Mock

    now = datetime.now(UTC)
    value = handover(now)
    journal = BootstrapJournal(tmp_path / "bootstrap")
    initial = journal.begin(value, now=now)
    verified = journal.advance(initial, "verified", now=now)
    observer = Mock(spec=HandoverObserver)
    observer.handover = value
    observer.observe = AsyncMock(side_effect=UpgradeConflict("no candidate kernel writer"))
    coordination = PrivateUpgradeStore(tmp_path / "coordination")
    restored = await start_handover_stability(journal, observer, coordination)
    assert restored.phase == "restoring"
    assert restored.deadline_at == verified.deadline_at
    assert restored.stability_started_at is None
    assert restored.incumbent_container_id == verified.incumbent_container_id
    assert not coordination.path("ownership.json").exists()


@pytest.mark.parametrize("changed", [False, True])
async def test_independent_incumbent_observation_is_bound_to_preparation(
    changed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from test_handover_creation_journal import preparation

    value = preparation()
    observer = HandoverObserver.__new__(HandoverObserver)
    observer.incumbent = value.incumbent.executor_container_id
    observer.handover = handover(value.prepared_at).model_copy(
        update={
            "incumbent_id": value.incumbent.executor_runtime_id,
            "candidate_id": value.candidate_runtime_id,
            "incumbent_container_id": value.incumbent.executor_container_id,
            "incumbent_image_id": value.incumbent.executor_image_id,
            "candidate_image_id": "sha256:" + "a" * 64 if changed else value.candidate_image_id,
            "generation": value.generation,
        }
    )
    now = datetime.now(UTC)
    measured = AsyncMock(return_value=(now, value.prepared_at - timedelta(seconds=1)))
    monkeypatch.setattr(observer, "verify_container", measured)
    if changed:
        with pytest.raises(UpgradeConflict):
            await observer.observe_incumbent(value)
        measured.assert_not_awaited()
    else:
        physical = await observer.observe_incumbent(value)
        assert physical.preparation_sha256 == value.sha256()
        assert physical.incumbent_container_id == value.incumbent.executor_container_id
        assert physical.observed_at == now
        measured.assert_awaited_once_with(value.incumbent.executor_container_id)


@pytest.mark.parametrize("endpoint", [False, True])
async def test_continuous_stability_alone_cannot_commit(tmp_path: Path, endpoint: bool) -> None:
    from unittest.mock import AsyncMock, Mock

    from coire_core.models.upgrades import BootstrapStabilityObservation

    now = datetime.now(UTC)
    started = now - timedelta(seconds=121)
    value = handover(started - timedelta(seconds=119))
    journal = BootstrapJournal(tmp_path / "bootstrap")
    initial = journal.begin(value, now=value.updated_at)
    verified = journal.advance(initial, "verified", now=value.updated_at)
    claimed = journal.advance(verified, "claimed", now=started)
    latest = None
    for seconds in [*range(0, 120, 10), 120 if endpoint else 119.5]:
        at = started + timedelta(seconds=seconds)
        latest = BootstrapStabilityObservation(
            generation=claimed.generation,
            incumbent_id=claimed.incumbent_id,
            candidate_id=claimed.candidate_id,
            incumbent_container_id=claimed.incumbent_container_id,
            candidate_container_id=claimed.candidate_container_id,
            incumbent_image_id=claimed.incumbent_image_id,
            candidate_image_id=claimed.candidate_image_id,
            kernel_boot_identity="isolated-kernel",
            candidate_started_at=started,
            observed_at=at,
            incumbent_healthy=True,
            candidate_healthy=True,
            exclusive_writer_verified=True,
        )
        journal.observe_stability(latest, now=at)
    assert latest is not None
    observer = Mock(spec=HandoverObserver)
    observer.handover = claimed
    observer.observe = AsyncMock(return_value=latest)
    coordination = PrivateUpgradeStore(tmp_path / "coordination")
    stable = await advance_handover_stability(journal, observer, coordination)
    assert stable.phase == ("stable" if endpoint else "claimed")
    assert stable.deadline_at == claimed.deadline_at
    assert await advance_handover_stability(journal, observer, coordination) == stable
    assert journal.current() == stable
