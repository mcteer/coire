"""Read exact retained executors without application plans or Docker mutation rights."""

import fcntl
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from opentelemetry import trace
from pydantic import JsonValue

from coire_core.deploy import host_boot_identity
from coire_core.errors import CoireError, UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    BootstrapStabilityObservation,
    CoreHandoverIncumbentObservation,
    CoreHandoverPreparation,
    DeploymentLease,
    DeploymentOwnership,
    UpgradeComposeService,
    UpgradeDockerImageDefaults,
    UpgradeKind,
    UpgradeRelease,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import read_prepared_compose
from coire_core.upgrade_journal import PrivateUpgradeStore

from .bootstrap import BootstrapJournal
from .compose import (
    _translate_execution,
    frozen_resources,
    reconcile_execution_mounts,
    verify_execution_configuration,
)
from .docker import DockerRead, UpgradeDocker, read_docker_object

tracer = trace.get_tracer("coire.upgrade_executor.handover")


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_writer", record_exception=False, set_status_on_exception=False
)
def verify_candidate_writer(
    store: PrivateUpgradeStore,
    handover: BootstrapHandover,
    project: str,
    executor_id: str = "coire-upgrade-executor",
) -> str:
    """An independent incumbent observes a held kernel lock and its exact private lease.

    This is a witness, never permission to take ownership. A dead process, lease
    timestamp, or unchanged PID cannot substitute for the retained kernel lock.
    """
    owner = store.read("ownership.json", DeploymentOwnership)
    lease = store.read("deployment.lease", DeploymentLease)
    boot = host_boot_identity()
    if (
        owner is None
        or lease is None
        or owner.owner_kind != "executor"
        or owner.project != project
        or owner.state != "active"
        or owner.job_id is not None
        or owner.owner_identity != executor_id
        or (
            owner.executor_runtime_id,
            owner.executor_container_id,
            owner.generation,
            owner.boot_identity,
        )
        != (handover.candidate_id, handover.candidate_container_id, handover.generation, boot)
        or (lease.project, lease.owner_identity, lease.kernel_platform, lease.kernel_boot_identity)
        != (owner.project, owner.owner_identity, sys.platform, boot)
    ):
        raise UpgradeConflict("Executor handover has no exact candidate writer lease")
    path = store.path("deployment.lock")
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            held, expected = os.fstat(descriptor), path.lstat()
            if (
                not stat.S_ISREG(held.st_mode)
                or held.st_uid != os.getuid()
                or held.st_mode & 0o077
                or held.st_nlink != 1
                or (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino)
            ):
                raise UpgradeUnavailable("Executor handover writer lock differs")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise UpgradeUnavailable("Executor handover candidate holds no kernel lock")
            if (
                store.read("ownership.json", DeploymentOwnership) != owner
                or store.read("deployment.lease", DeploymentLease) != lease
            ):
                raise UpgradeConflict("Executor handover writer changed during observation")
        finally:
            os.close(descriptor)
    except OSError:
        raise UpgradeUnavailable("Executor handover writer lock is unavailable") from None
    return boot


class PreparedRuntimeObserver:
    """Read-only checks shared by bootstrap Source and executor handover."""

    proxy: httpx.URL
    client: httpx.AsyncClient
    project: str
    roles: dict[str, tuple[UpgradeComposeService, dict[str, JsonValue]]]
    service_names: dict[str, str]

    async def read(self, method: str, path: str) -> DockerRead:
        allowed = {f"/containers/{container}/json" for container in self.roles} | {
            f"/images/{role.image}/json" for role, _ in self.roles.values()
        }
        if method != "GET" or path not in allowed:
            raise UpgradeForbidden("Executor handover cannot mutate or inspect another resource")
        return await read_docker_object(self.client, self.proxy, path)

    async def verify_container(
        self,
        container_id: str,
        *,
        require_healthy: bool = True,
        require_running: bool = True,
    ) -> tuple[datetime, datetime]:
        if container_id not in self.roles:
            raise UpgradeForbidden("Executor handover container is outside retained authority")
        role, document = self.roles[container_id]
        body = await self.read("GET", f"/containers/{container_id}/json")
        state, config, network_settings = (
            body.get("State"),
            body.get("Config"),
            body.get("NetworkSettings"),
        )
        labels = config.get("Labels") if isinstance(config, dict) else None
        health = state.get("Health") if isinstance(state, dict) else None
        attachments = (
            network_settings.get("Networks") if isinstance(network_settings, dict) else None
        )
        if (
            body.get("Id") != container_id
            or body.get("Image") != role.image
            or not isinstance(labels, dict)
            or labels.get("com.docker.compose.project") != self.project
            or labels.get("com.docker.compose.service") != self.service_names[container_id]
            or not isinstance(state, dict)
            or type(state.get("Running")) is not bool
            or (require_running and state.get("Running") is not True)
            or (
                require_healthy
                and (not isinstance(health, dict) or health.get("Status") != "healthy")
            )
            or not isinstance(attachments, dict)
        ):
            raise UpgradeConflict("Retained executor physical identity or health differs")
        networks: dict[str, str] = {}
        for logical, physical in UpgradeDocker._network_names(document).items():
            if logical not in role.networks:
                continue
            attachment = attachments.get(physical)
            if not isinstance(attachment, dict) or not isinstance(attachment.get("NetworkID"), str):
                raise UpgradeConflict("Retained executor network identity differs")
            networks[logical] = str(attachment["NetworkID"])
        image = await self.read("GET", f"/images/{role.image}/json")
        defaults = image.get("Config")
        if (image.get("Id"), image.get("Os"), image.get("Architecture")) != (
            role.image,
            "linux",
            "arm64",
        ) or not isinstance(defaults, dict):
            raise UpgradeConflict("Retained executor image platform or defaults differ")
        exposed = defaults.get("ExposedPorts") or {}
        if not isinstance(exposed, dict):
            raise UpgradeConflict("Retained executor image ports differ")
        try:
            image_defaults = UpgradeDockerImageDefaults.model_validate(
                {
                    "image_id": role.image,
                    "environment": defaults.get("Env") or [],
                    "entrypoint": defaults.get("Entrypoint"),
                    "command": defaults.get("Cmd"),
                    "working_dir": defaults.get("WorkingDir") or "",
                    "stop_signal": defaults.get("StopSignal") or None,
                    "exposed_ports": tuple(exposed),
                }
            )
            started = datetime.fromisoformat(str(state.get("StartedAt")).replace("Z", "+00:00"))
            if started.utcoffset() is None or started > body.started_at:
                raise ValueError("executor start time differs")
        except ValueError:
            raise UpgradeConflict("Retained executor process or defaults are malformed") from None
        mounts = reconcile_execution_mounts(
            role, body.get("Mounts"), frozen_resources(document, self.project)
        )
        expected = _translate_execution(
            role,
            role,
            role=self.service_names[container_id],
            project=self.project,
            network_ids=networks,
            mount_bindings=mounts,
            dependency_states={},
            enforce_dependency_readiness=False,
        )
        verify_execution_configuration(body, image_defaults, expected, network_ids=networks)
        if not 0 <= (datetime.now(UTC) - body.started_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Retained executor physical observation expired")
        return body.started_at, started


class HandoverObserver(PreparedRuntimeObserver):
    """Only inspect two pinned containers and their prepared Linux ARM64 images."""

    def __init__(
        self,
        settings: Settings,
        prepared_root: Path,
        source: UpgradeRelease,
        target: UpgradeRelease,
        handover: BootstrapHandover,
        incumbent_container_id: str,
        candidate_container_id: str,
        client: httpx.AsyncClient,
    ) -> None:
        import re

        self.proxy = httpx.URL(settings.upgrade_proxy_url)
        if (
            str(self.proxy) != "http://docker-socket-proxy:2375"
            or client.follow_redirects
            or client._trust_env
            or any(
                re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in (incumbent_container_id, candidate_container_id)
            )
            or incumbent_container_id == candidate_container_id
            or (incumbent_container_id, candidate_container_id)
            != (handover.incumbent_container_id, handover.candidate_container_id)
        ):
            raise UpgradeForbidden("Executor handover transport or physical identities differ")
        self.client, self.handover = client, handover
        self.executor_id = settings.upgrade_executor_id
        self.project = settings.upgrade_project
        self.roles: dict[str, tuple[UpgradeComposeService, dict[str, JsonValue]]] = {}
        for release, container, image in (
            (source, incumbent_container_id, handover.incumbent_image_id),
            (target, candidate_container_id, handover.candidate_image_id),
        ):
            if (
                release.manifest.kind is not UpgradeKind.CONTROL_PLANE
                or release.manifest.project != self.project
            ):
                raise UpgradeForbidden("Executor bootstrap release is outside its project")
            document = read_prepared_compose(prepared_root, release)
            services = document.get("services")
            configuration = (
                services.get("coire-upgrade-executor") if isinstance(services, dict) else None
            )
            try:
                role = UpgradeComposeService.model_validate(configuration)
            except ValueError:
                raise UpgradeConflict(
                    "Prepared executor configuration is absent or unsupported"
                ) from None
            if (
                role.image != image
                or role.ports
                or set(role.networks) != {"coire-docker", "coire-internal", "coire-telemetry"}
            ):
                raise UpgradeConflict("Prepared executor image or fixed topology differs")
            self.roles[container] = role, document
        self.incumbent, self.candidate = incumbent_container_id, candidate_container_id
        self.service_names = dict.fromkeys(self.roles, "coire-upgrade-executor")

    @tracer.start_as_current_span(
        "coire.upgrade_executor.handover_incumbent_observe",
        record_exception=False,
        set_status_on_exception=False,
    )
    async def observe_incumbent(
        self, preparation: CoreHandoverPreparation
    ) -> CoreHandoverIncumbentObservation:
        if (
            self.incumbent != preparation.incumbent.executor_container_id
            or self.handover.incumbent_id != preparation.incumbent.executor_runtime_id
            or self.handover.incumbent_image_id != preparation.incumbent.executor_image_id
            or self.handover.candidate_id != preparation.candidate_runtime_id
            or self.handover.candidate_image_id != preparation.candidate_image_id
            or self.handover.generation != preparation.generation
        ):
            raise UpgradeConflict("Independent incumbent observer belongs to another handover")
        at, started = await self.verify_container(self.incumbent)
        if not 0 <= (datetime.now(UTC) - at).total_seconds() <= 5:
            raise UpgradeUnavailable("Independent incumbent physical observation expired")
        return CoreHandoverIncumbentObservation(
            preparation_sha256=preparation.sha256(),
            incumbent_runtime_id=preparation.incumbent.executor_runtime_id,
            incumbent_container_id=self.incumbent,
            incumbent_image_id=self.handover.incumbent_image_id,
            incumbent_started_at=started,
            observed_at=at,
            incumbent_healthy=True,
        )

    @tracer.start_as_current_span(
        "coire.upgrade_executor.handover_observe",
        record_exception=False,
        set_status_on_exception=False,
    )
    async def observe(self, coordination: PrivateUpgradeStore) -> BootstrapStabilityObservation:
        boot = verify_candidate_writer(coordination, self.handover, self.project, self.executor_id)
        incumbent_at, _ = await self.verify_container(self.incumbent)
        candidate_at, started = await self.verify_container(self.candidate)
        if (
            verify_candidate_writer(coordination, self.handover, self.project, self.executor_id)
            != boot
        ):
            raise UpgradeConflict("Executor writer changed during physical observations")
        at = min(incumbent_at, candidate_at)
        if not 0 <= (datetime.now(UTC) - at).total_seconds() <= 5:
            raise UpgradeUnavailable("Retained executor observations are no longer fresh")
        return BootstrapStabilityObservation(
            generation=self.handover.generation,
            incumbent_id=self.handover.incumbent_id,
            candidate_id=self.handover.candidate_id,
            incumbent_container_id=self.incumbent,
            candidate_container_id=self.candidate,
            incumbent_image_id=self.handover.incumbent_image_id,
            candidate_image_id=self.handover.candidate_image_id,
            kernel_boot_identity=boot,
            candidate_started_at=started,
            observed_at=at,
            incumbent_healthy=True,
            candidate_healthy=True,
            exclusive_writer_verified=True,
        )


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_stability_start",
    record_exception=False,
    set_status_on_exception=False,
)
async def start_handover_stability(
    journal: BootstrapJournal,
    observer: HandoverObserver,
    coordination: PrivateUpgradeStore,
) -> BootstrapHandover:
    """Capture stability from the incumbent's first actual candidate writer observation.

    A retained claimed interval is replayed unchanged. This read-only step does
    not authorize or perform the preceding ownership transfer.
    """
    current = journal.current()
    if current is None or current.phase not in {"verified", "claimed", "stable"}:
        raise UpgradeConflict("Executor stability has no verified handover")
    identity = (
        "incumbent_id",
        "candidate_id",
        "incumbent_container_id",
        "candidate_container_id",
        "incumbent_image_id",
        "candidate_image_id",
        "generation",
    )
    if any(getattr(current, key) != getattr(observer.handover, key) for key in identity):
        raise UpgradeConflict("Executor stability observer belongs to another handover")
    if current.phase in {"claimed", "stable"}:
        return current
    try:
        observation = await observer.observe(coordination)
    except CoireError:
        return journal.advance(current, "restoring")
    journal.advance(current, "claimed", now=observation.observed_at)
    return journal.observe_stability(observation)


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_stability",
    record_exception=False,
    set_status_on_exception=False,
)
async def advance_handover_stability(
    journal: BootstrapJournal,
    observer: HandoverObserver,
    coordination: PrivateUpgradeStore,
) -> BootstrapHandover:
    """The incumbent performs one physical step before any candidate commit.

    Failure retains both owners and records restoration intent. This function
    never starts, stops, deletes or transfers an executor; the retained supervisor
    must reconcile that independent lifecycle before acknowledging restoration.
    """
    current = journal.current()
    if current is None or current.phase not in {"claimed", "stable"}:
        raise UpgradeConflict("Executor stability is outside its retained handover phase")
    identity = (
        "incumbent_id",
        "candidate_id",
        "incumbent_container_id",
        "candidate_container_id",
        "incumbent_image_id",
        "candidate_image_id",
        "generation",
    )
    if any(getattr(current, key) != getattr(observer.handover, key) for key in identity):
        raise UpgradeConflict("Executor stability observer belongs to another handover")
    try:
        observation = await observer.observe(coordination)
    except CoireError:
        return journal.advance(current, "restoring")
    observed = journal.observe_stability(observation)
    if observed.phase == "restoring":
        return observed
    if observed.phase == "stable":
        # Stability alone cannot acknowledge the API's durable commit decision.
        return observed
    if observation.observed_at >= observed.deadline_at:
        return journal.advance(observed, "stable", now=observation.observed_at)
    return observed
