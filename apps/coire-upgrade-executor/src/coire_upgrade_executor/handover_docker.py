"""Fixed passive executor creation; no starts, stops, pulls, volumes or generic Docker RPC."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from opentelemetry import trace
from pydantic import JsonValue

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapPhysicalObservation,
    CoreHandoverCandidateStart,
    CoreHandoverCreation,
    DeploymentHandoverHold,
    UpgradeComposeService,
    UpgradeDockerImageDefaults,
    UpgradeMountBinding,
    UpgradeReleaseRegister,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import read_prepared_compose, verify_prepared_release
from coire_upgrade_executor.compose import (
    _translate_execution,
    bind_physical_network_names,
    frozen_resources,
    reconcile_execution_mounts,
    verify_execution_configuration,
)
from coire_upgrade_executor.docker import DockerRead, UpgradeDocker, read_docker_object
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.namespace import verify_executor_namespace
from coire_upgrade_executor.ownership import BootstrapAuthority

tracer = trace.get_tracer("coire.upgrade_executor.handover")


class HandoverDocker:
    def __init__(
        self,
        settings: Settings,
        journal: HandoverCreationJournal,
        authority: BootstrapAuthority,
        client: httpx.AsyncClient,
    ) -> None:
        self.settings, self.journal, self.authority, self.client = (
            settings,
            journal,
            authority,
            client,
        )
        self.preparation = journal.preparation
        self.seed = self.preparation.incumbent
        self.candidate_configuration: (
            tuple[
                dict[str, Any],
                UpgradeDockerImageDefaults,
                dict[str, str],
                tuple[UpgradeMountBinding, ...],
            ]
            | None
        ) = None
        self.proxy = httpx.URL(settings.upgrade_proxy_url)
        if (
            str(self.proxy) != "http://docker-socket-proxy:2375"
            or client.follow_redirects
            or client._trust_env
        ):
            raise UpgradeForbidden("Executor handover transport differs")
        verify_executor_namespace(self.seed)
        self.verify_owner()
        prepared = Path(settings.upgrade_prepared_dir)
        for release in (self.seed.executor_source, self.preparation.candidate_release):
            verified = verify_prepared_release(
                prepared,
                UpgradeReleaseRegister(
                    prepared_artifact_id=release.prepared_artifact_id,
                    expected_sha256=release.manifest_sha256,
                ),
            )
            if verified != release:
                raise UpgradeConflict("Executor handover sealed provenance differs")
        self.source_document = read_prepared_compose(prepared, self.seed.executor_source)
        self.target_document = read_prepared_compose(prepared, self.preparation.candidate_release)
        self.source = self.executor_role(self.source_document)
        self.target = self.executor_role(self.target_document)
        if (
            self.source.image != self.seed.executor_image_id
            or self.target.image != self.preparation.candidate_image_id
            or self.source.model_dump(exclude={"image"})
            != self.target.model_dump(exclude={"image"})
        ):
            raise UpgradeConflict("Executor handover may change only its prepared immutable image")
        if frozen_resources(self.source_document, settings.upgrade_project) != frozen_resources(
            self.target_document, settings.upgrade_project
        ):
            raise UpgradeConflict("Executor handover resources or credential generation differ")

    def verify_owner(self) -> None:
        failure = self.journal.store.path("failure-intent.json")
        if failure.exists() or failure.is_symlink():
            raise UpgradeForbidden(
                "Effect-free handover failure durably prohibits candidate effects"
            )
        self.authority.verify()
        owner = self.authority.claim
        hold = self.authority.store.read("handover-hold.json", DeploymentHandoverHold)
        if (
            hold is None
            or hold.released
            or (
                hold.project,
                hold.preparation_sha256,
                hold.generation,
                hold.incumbent_runtime_id,
                hold.incumbent_container_id,
                hold.candidate_runtime_id,
            )
            != (
                self.settings.upgrade_project,
                self.preparation.sha256(),
                self.preparation.generation,
                self.seed.executor_runtime_id,
                self.seed.executor_container_id,
                self.preparation.candidate_runtime_id,
            )
        ):
            raise UpgradeForbidden("Executor creation has no exact durable handover hold")
        if (
            not self.settings.upgrades_enabled
            or (
                owner.project,
                owner.owner_identity,
                owner.executor_runtime_id,
                owner.executor_container_id,
                owner.source_sha256,
            )
            != (
                self.settings.upgrade_project,
                self.settings.upgrade_executor_id,
                self.seed.executor_runtime_id,
                self.seed.executor_container_id,
                self.seed.source.manifest_sha256,
            )
            or owner.job_id is not None
            or self.preparation.generation != owner.generation + 1
        ):
            raise UpgradeForbidden("Executor handover has no exact independent incumbent owner")

    @staticmethod
    def executor_role(document: dict[str, JsonValue]) -> UpgradeComposeService:
        services = document.get("services")
        raw = services.get("coire-upgrade-executor") if isinstance(services, dict) else None
        try:
            role = UpgradeComposeService.model_validate(raw)
        except ValueError:
            raise UpgradeConflict("Executor handover role is absent or unsupported") from None
        if role.ports or set(role.networks) != {
            "coire-docker",
            "coire-internal",
            "coire-telemetry",
        }:
            raise UpgradeForbidden("Executor handover topology differs")
        return role

    async def read(self, path: str, *, allow_absent: bool = False) -> DockerRead:
        self.verify_owner()
        current = self.journal.current()
        allowed = {
            f"/containers/{self.seed.executor_container_id}/json",
            f"/containers/{self.preparation.candidate_name}/json",
            f"/images/{self.seed.executor_image_id}/json",
            f"/images/{self.preparation.candidate_image_id}/json",
        }
        if current is not None and current.candidate_container_id is not None:
            allowed.add(f"/containers/{current.candidate_container_id}/json")
        if path not in allowed or (
            allow_absent and path != f"/containers/{self.preparation.candidate_name}/json"
        ):
            raise UpgradeForbidden("Executor handover read is outside fixed authority")
        result = await read_docker_object(self.client, self.proxy, path, allow_absent=allow_absent)
        self.verify_owner()
        return result

    async def image(self, image_id: str) -> UpgradeDockerImageDefaults:
        body = await self.read(f"/images/{image_id}/json")
        config = body.get("Config")
        if (body.get("Id"), body.get("Os"), body.get("Architecture")) != (
            image_id,
            "linux",
            "arm64",
        ) or not isinstance(config, dict):
            raise UpgradeConflict("Executor handover immutable image differs")
        ports = config.get("ExposedPorts") or {}
        if not isinstance(ports, dict):
            raise UpgradeConflict("Executor handover image ports differ")
        defaults = UpgradeDockerImageDefaults.model_validate(
            {
                "image_id": image_id,
                "environment": config.get("Env") or [],
                "entrypoint": config.get("Entrypoint"),
                "command": config.get("Cmd"),
                "working_dir": config.get("WorkingDir") or "",
                "stop_signal": config.get("StopSignal") or None,
                "exposed_ports": tuple(ports),
            }
        )
        argv = [*(defaults.entrypoint or ()), *(defaults.command or ())]
        if (
            len(argv) != 3
            or argv[-2:] != ["-m", "coire_upgrade_executor"]
            or argv[0] not in {"python", "/app/.venv/bin/python3"}
        ):
            raise UpgradeConflict("Executor handover image command differs from its fixed module")
        return defaults

    @tracer.start_as_current_span(
        "coire.upgrade_executor.handover_create",
        record_exception=False,
        set_status_on_exception=False,
    )
    async def create(self, physical: CoreBootstrapPhysicalObservation) -> CoreHandoverCreation:
        """Create at most once; reconcile lost responses while retaining the incumbent."""
        self.verify_owner()
        self.authority.heartbeat(self.seed, physical)
        with self.journal.store.ownership("dispatch.lock"):
            self.journal.begin()
            source = await self.read(f"/containers/{self.seed.executor_container_id}/json")
            config, state, network_settings = (
                source.get("Config"),
                source.get("State"),
                source.get("NetworkSettings"),
            )
            labels = config.get("Labels") if isinstance(config, dict) else None
            attachments = (
                network_settings.get("Networks") if isinstance(network_settings, dict) else None
            )
            if (
                source.get("Id") != self.seed.executor_container_id
                or not isinstance(state, dict)
                or state.get("Running") is not True
                or not isinstance(labels, dict)
                or labels.get("com.docker.compose.project") != self.settings.upgrade_project
                or labels.get("com.docker.compose.service") != "coire-upgrade-executor"
                or not isinstance(attachments, dict)
            ):
                raise UpgradeConflict("Executor handover incumbent physical identity differs")
            networks: dict[str, str] = {}
            for logical, name in UpgradeDocker._network_names(self.source_document).items():
                if logical not in self.source.networks:
                    continue
                attachment = attachments.get(name)
                if not isinstance(attachment, dict) or not isinstance(
                    attachment.get("NetworkID"), str
                ):
                    raise UpgradeConflict("Executor handover network identity differs")
                networks[logical] = str(attachment["NetworkID"])
            resources = frozen_resources(self.source_document, self.settings.upgrade_project)
            mounts = reconcile_execution_mounts(self.source, source.get("Mounts"), resources)
            source_expected = _translate_execution(
                self.source,
                self.source,
                role="coire-upgrade-executor",
                project=self.settings.upgrade_project,
                network_ids=networks,
                mount_bindings=mounts,
                dependency_states={},
                enforce_dependency_readiness=False,
            )
            verify_execution_configuration(
                source, await self.image(self.source.image), source_expected, network_ids=networks
            )
            defaults = await self.image(self.target.image)
            payload: dict[str, Any] = _translate_execution(
                self.source,
                self.target,
                role="coire-upgrade-executor",
                project=self.settings.upgrade_project,
                network_ids=networks,
                mount_bindings=mounts,
                dependency_states={},
                enforce_dependency_readiness=False,
            )
            bind_physical_network_names(payload, UpgradeDocker._network_names(self.source_document))
            payload["Labels"].update(
                {
                    "coire.bootstrap.preparation_sha256": self.preparation.sha256(),
                    "coire.bootstrap.generation": str(self.preparation.generation),
                    "coire.bootstrap.runtime_id": self.preparation.candidate_runtime_id,
                }
            )
            candidate_path = f"/containers/{self.preparation.candidate_name}/json"
            body = await self.read(candidate_path, allow_absent=True)
            if not body:
                self.authority.heartbeat(self.seed, physical)
                if not self.journal.dispatch(self.journal.expected()):
                    raise UpgradeUnavailable(
                        "Executor creation remains uncertain; retained dispatch forbids recreation"
                    )
                self.verify_owner()
                remaining = (self.preparation.deadline_at - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise UpgradeUnavailable("Executor creation original deadline elapsed")
                try:
                    async with (
                        asyncio.timeout(min(5, remaining)),
                        self.client.stream(
                            "POST",
                            self.proxy.copy_with(path="/v1.46/containers/create"),
                            params={"name": self.preparation.candidate_name},
                            json=payload,
                            timeout=min(5, remaining),
                        ) as response,
                    ):
                        if response.status_code != 201:
                            raise UpgradeUnavailable("Executor creation outcome remains uncertain")
                except (httpx.HTTPError, TimeoutError):
                    raise UpgradeUnavailable(
                        "Executor creation response remains uncertain"
                    ) from None
                self.verify_owner()
                body = await self.read(candidate_path)
            candidate_config = body.get("Config")
            candidate_labels = (
                candidate_config.get("Labels") if isinstance(candidate_config, dict) else None
            )
            identity = body.get("Id")
            if (
                not isinstance(identity, str)
                or body.get("Name") != "/" + self.preparation.candidate_name
                or not isinstance(candidate_labels, dict)
                or any(
                    candidate_labels.get(key) != value for key, value in payload["Labels"].items()
                )
            ):
                raise UpgradeConflict(
                    "Executor candidate name or retained preparation labels differ"
                )
            if reconcile_execution_mounts(self.target, body.get("Mounts"), resources) != mounts:
                raise UpgradeConflict("Executor candidate mounts differ from retained incumbent")
            verify_execution_configuration(body, defaults, payload, network_ids=networks)
            self.candidate_configuration = payload, defaults, networks, mounts
            self.verify_owner()
            return self.journal.observe(
                self.journal.expected(), identity, observed_at=body.started_at
            )

    def verified_candidate_state(self, body: DockerRead) -> dict[str, JsonValue]:
        self.verify_owner()
        seed = self.journal.retained_candidate_seed()
        if self.candidate_configuration is None:
            raise UpgradeUnavailable("Candidate configuration has not been verified")
        payload, defaults, networks, mounts = self.candidate_configuration
        config, state = body.get("Config"), body.get("State")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if (
            body.get("Id") != seed.executor_container_id
            or body.get("Name") != "/" + self.preparation.candidate_name
            or not isinstance(state, dict)
            or type(state.get("Running")) is not bool
            or not isinstance(labels, dict)
            or any(labels.get(key) != value for key, value in payload["Labels"].items())
        ):
            raise UpgradeConflict("Candidate start physical authority differs")
        resources = frozen_resources(self.target_document, self.settings.upgrade_project)
        if reconcile_execution_mounts(self.target, body.get("Mounts"), resources) != mounts:
            raise UpgradeConflict("Candidate start mounts changed")
        verify_execution_configuration(body, defaults, payload, network_ids=networks)
        return state

    @tracer.start_as_current_span(
        "coire.upgrade_executor.handover_start",
        record_exception=False,
        set_status_on_exception=False,
    )
    async def start_candidate(
        self,
        physical: CoreBootstrapPhysicalObservation,
    ) -> CoreHandoverCandidateStart:
        """Start the sole fixed candidate once; uncertainty never renews dispatch."""
        creation = await self.create(physical)
        seed = self.journal.retained_candidate_seed()
        if creation.candidate_container_id != seed.executor_container_id:
            raise UpgradeConflict("Candidate start differs from its published creation")
        if self.candidate_configuration is None:
            raise UpgradeUnavailable("Candidate start configuration has not been verified")
        path = f"/containers/{seed.executor_container_id}/json"

        with self.journal.store.ownership("start-dispatch.lock"):
            self.verify_owner()
            intent = self.journal.begin_start()
            body = await self.read(path)
            state = self.verified_candidate_state(body)
            if state["Running"] is not True:
                if not self.journal.dispatch_start(self.journal.expected_start()):
                    raise UpgradeUnavailable(
                        "Candidate start remains uncertain; redispatch is forbidden"
                    )
                self.authority.heartbeat(self.seed, physical)
                remaining = (intent.deadline_at - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise UpgradeUnavailable("Candidate start original deadline elapsed")
                self.verify_owner()
                try:
                    async with (
                        asyncio.timeout(min(5, remaining)),
                        self.client.stream(
                            "POST",
                            self.proxy.copy_with(
                                path="/v1.46" + path.removesuffix("/json") + "/start"
                            ),
                            timeout=min(5, remaining),
                        ) as response,
                    ):
                        if response.status_code not in {204, 304}:
                            raise UpgradeUnavailable("Candidate start outcome remains uncertain")
                except (httpx.HTTPError, TimeoutError):
                    raise UpgradeUnavailable("Candidate start response remains uncertain") from None
                self.verify_owner()
                body = await self.read(path)
                state = self.verified_candidate_state(body)
            if state["Running"] is not True:
                raise UpgradeUnavailable("Candidate start has no running process observation")
            try:
                started_at = datetime.fromisoformat(
                    str(state.get("StartedAt")).replace("Z", "+00:00")
                )
            except ValueError:
                raise UpgradeConflict("Candidate process birth is malformed") from None
            if started_at.tzinfo is None:
                raise UpgradeConflict("Candidate process birth has no clock authority")
            self.verify_owner()
            return self.journal.observe_start(started_at=started_at, observed_at=body.started_at)
