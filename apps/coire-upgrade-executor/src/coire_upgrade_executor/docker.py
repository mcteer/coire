"""Exact accepted Docker read boundary; no generic Engine API forwarding."""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
from opentelemetry import trace
from pydantic import JsonValue

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreDependencyObservation,
    CoreExecutorEffect,
    CoreRuntimeSelection,
    CoreSelectedMigration,
    CoreSourceSnapshot,
    UpgradeComposeDatabase,
    UpgradeComposeMigration,
    UpgradeComposeResources,
    UpgradeDockerContainer,
    UpgradeDockerImage,
    UpgradeDockerImageDefaults,
    UpgradeDockerMigration,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMigrationExecution,
    UpgradeRelease,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import read_prepared_compose

from .compose import (
    bind_physical_network_names,
    frozen_resources,
    reconcile_execution_mounts,
    reconcile_mounts,
    translate_migration,
    translate_service,
    verify_database_configuration,
    verify_execution_configuration,
    verify_source_configuration,
)
from .journal import ExecutorJournal
from .ownership import ExecutorAuthority

tracer = trace.get_tracer("coire.upgrade_executor.docker")
IMAGE_PATH = re.compile(r"/images/(sha256:[a-f0-9]{64})/json")
MAX_RESPONSE = 1024 * 1024


class DockerRead(dict[str, JsonValue]):
    """One bounded response with its original read time, outside the JSON fields."""

    def __init__(self, values: dict[str, JsonValue], started_at: datetime) -> None:
        super().__init__(values)
        self.started_at = started_at


async def read_docker_object(
    client: httpx.AsyncClient, proxy: httpx.URL, path: str, *, allow_absent: bool = False
) -> DockerRead:
    """Bound transport after the caller has enforced its exact resource allowlist."""
    url = proxy.copy_with(path="/v1.46" + path)
    payload = bytearray()
    started_at = datetime.now(UTC)
    try:
        async with asyncio.timeout(5), client.stream("GET", url, timeout=5) as response:
            if allow_absent and response.status_code == 404:
                return DockerRead({}, started_at)
            if response.status_code != 200:
                raise UpgradeUnavailable("Accepted Docker image cannot be inspected")
            async for chunk in response.aiter_bytes(chunk_size=32768):
                payload.extend(chunk)
                if len(payload) > MAX_RESPONSE:
                    raise UpgradeUnavailable("Docker image inspection exceeds its bound")
    except (httpx.HTTPError, TimeoutError):
        raise UpgradeUnavailable("Docker image inspection is unavailable") from None

    def unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def finite(value: str) -> None:
        raise ValueError("nonfinite JSON value")

    try:
        decoded = json.loads(payload, object_pairs_hook=unique, parse_constant=finite)
    except (ValueError, RecursionError):
        raise UpgradeUnavailable("Docker image inspection is malformed") from None
    if not isinstance(decoded, dict) or not decoded:
        raise UpgradeUnavailable("Docker image inspection is not an object")
    return DockerRead(cast(dict[str, JsonValue], decoded), started_at)


class UpgradeDocker:
    def __init__(
        self,
        settings: Settings,
        plan: UpgradeExecutionPlan,
        *,
        client: httpx.AsyncClient | None = None,
        prepared_root: Path | None = None,
        source_selection: CoreRuntimeSelection | None = None,
    ) -> None:
        if (
            plan.operation.kind is not UpgradeKind.CONTROL_PLANE
            or plan.source.manifest.project != settings.upgrade_project
        ):
            raise UpgradeForbidden("Docker authority requires the accepted Core project")
        proxy = httpx.URL(settings.upgrade_proxy_url)
        if (
            (proxy.scheme, proxy.host, proxy.port, proxy.raw_path)
            != ("http", "docker-socket-proxy", 2375, b"/")
            or proxy.userinfo
            or proxy.fragment
            or proxy.query
        ):
            raise UpgradeForbidden("Docker proxy address differs from its fixed private endpoint")
        self.plan = plan
        self.proxy = proxy
        images = {
            service.image_id
            for release in (plan.source, plan.target)
            for service in release.manifest.services
        }
        self.migration_roles: dict[str, UpgradeComposeMigration] = {}
        self.source_migration_release: UpgradeRelease = plan.source
        self.source_migration_selection = source_selection.migration if source_selection else None
        self.database_role: UpgradeComposeDatabase | None = None
        self.database_name = f"{settings.upgrade_project}-postgres-1"
        self.migration_source_name = f"{settings.upgrade_project}-coire-migrate-1"
        self.migration_candidate_name = (
            f"{settings.upgrade_project}-upgrade-{plan.operation.job_id.lower()}-coire-migrate"
        )
        self.executor_id = settings.upgrade_executor_id
        if prepared_root is not None:
            database_presence: list[bool] = []
            for release, name in (
                (plan.source, self.migration_source_name),
                (plan.target, self.migration_candidate_name),
            ):
                document = read_prepared_compose(prepared_root, release)
                services = document.get("services")
                database = services.get("postgres") if isinstance(services, dict) else None
                database_presence.append(database is not None)
                if database is not None:
                    try:
                        database_role = UpgradeComposeDatabase.model_validate(database)
                    except ValueError:
                        raise UpgradeConflict("Frozen database dependency is unsupported") from None
                    if self.database_role is not None and self.database_role != database_role:
                        raise UpgradeConflict(
                            "Managed upgrades cannot change the database dependency"
                        )
                    self.database_role = database_role
                    images.add(database_role.image)
                migration = services.get("coire-migrate") if isinstance(services, dict) else None
                if migration is not None:
                    try:
                        role = UpgradeComposeMigration.model_validate(migration)
                    except ValueError:
                        raise UpgradeConflict(
                            "Accepted migration configuration is unsupported"
                        ) from None
                    self.migration_roles[name] = role
                    images.add(role.image)
            if database_presence[0] != database_presence[1]:
                raise UpgradeConflict("Managed upgrade cannot add or remove PostgreSQL")
        self.source_names = {
            f"{settings.upgrade_project}-{service.service}-1": service
            for service in plan.source.manifest.services
        }
        self.source_selection = source_selection
        if source_selection is not None:
            if (
                source_selection.project,
                source_selection.release_id,
                source_selection.manifest_sha256,
                source_selection.compose_sha256,
                source_selection.host_release_key,
            ) != (
                settings.upgrade_project,
                plan.source.release_id,
                plan.source.manifest_sha256,
                plan.source.manifest.compose_sha256,
                plan.source.manifest.host_release_key,
            ) or {
                item.container.service: item.container.image_id
                for item in source_selection.services
            } != {item.service: item.image_id for item in plan.source.manifest.services}:
                raise UpgradeConflict("Selected Core Source differs from the accepted manifest")
            source_roles = {item.service: item for item in plan.source.manifest.services}
            self.source_names = {
                item.container.name: source_roles[item.container.service]
                for item in source_selection.services
            }
            selected_migration = source_selection.migration
            if selected_migration is not None:
                if prepared_root is None:
                    raise UpgradeUnavailable(
                        "Retained migration requires its protected frozen release"
                    )
                migration_document = read_prepared_compose(
                    prepared_root, selected_migration.release
                )
                migration_services = migration_document.get("services")
                frozen_migration = (
                    migration_services.get("coire-migrate")
                    if isinstance(migration_services, dict)
                    else None
                )
                try:
                    retained_role = UpgradeComposeMigration.model_validate(frozen_migration)
                except ValueError:
                    raise UpgradeConflict(
                        "Retained migration configuration is unavailable"
                    ) from None
                if selected_migration.origin_job_id is not None:
                    retained_control = {
                        "UPGRADES_ENABLED": "true",
                        "UPGRADE_PROJECT": settings.upgrade_project,
                        "UPGRADE_EXECUTOR_ID": settings.upgrade_executor_id,
                        "UPGRADE_MIGRATION_JOB_ID": selected_migration.origin_job_id,
                        "UPGRADE_MIGRATION_GENERATION": str(selected_migration.origin_generation),
                    }
                    if any(
                        key in retained_role.environment and retained_role.environment[key] != value
                        for key, value in retained_control.items()
                    ):
                        raise UpgradeConflict(
                            "Retained migration overrides its historical authority"
                        )
                    retained_role = UpgradeComposeMigration.model_validate(
                        retained_role.model_dump()
                        | {"environment": retained_role.environment | retained_control}
                    )
                if retained_role != selected_migration.configuration:
                    raise UpgradeConflict(
                        "Retained migration differs from its protected frozen configuration"
                    )
                self.migration_roles.pop(self.migration_source_name, None)
                self.migration_source_name = selected_migration.container.name
                self.migration_roles[self.migration_source_name] = retained_role
                self.source_migration_release = selected_migration.release
                images.add(retained_role.image)
            elif prepared_root is not None and self.migration_source_name in self.migration_roles:
                raise UpgradeUnavailable(
                    "Selected Core Source lacks its retained completed migration"
                )
        self.images = frozenset(images)
        self.candidate_names = {
            f"{settings.upgrade_project}-upgrade-{plan.operation.job_id.lower()}-{service.service}": service
            for service in plan.target.manifest.services
            if service.service in plan.operation.cohort
        }
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            timeout=5, trust_env=False, follow_redirects=False
        )
        if self._client.follow_redirects or self._client._trust_env:
            raise UpgradeForbidden("Docker transport cannot use redirects or ambient proxies")

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    @tracer.start_as_current_span(
        "coire.upgrade_executor.docker_read", record_exception=False, set_status_on_exception=False
    )
    async def _read(self, method: str, path: str, *, absent_candidate: bool = False) -> DockerRead:
        image = IMAGE_PATH.fullmatch(path)
        container = re.fullmatch(r"/containers/([a-z][a-z0-9_-]{1,255})/json", path)
        allowed_image = image is not None and image.group(1) in self.images
        candidate = container is not None and (
            container.group(1) in self.candidate_names
            or (
                container.group(1) == self.migration_candidate_name
                and container.group(1) in self.migration_roles
            )
        )
        allowed_container = container is not None and (
            container.group(1) in self.source_names
            or container.group(1) in self.migration_roles
            or candidate
            or (container.group(1) == self.database_name and self.database_role is not None)
        )
        if absent_candidate and not candidate:
            raise UpgradeForbidden("Absent-resource reconciliation requires an accepted candidate")
        if method != "GET" or not (allowed_image or allowed_container):
            raise UpgradeForbidden("Docker operation is outside accepted resource inspection")
        return await read_docker_object(
            self._client, self.proxy, path, allow_absent=absent_candidate
        )

    async def inspect_image(self, image_id: str) -> UpgradeDockerImage:
        if image_id not in self.images:
            raise UpgradeForbidden("Image is outside the accepted release")
        body = await self._read("GET", f"/images/{image_id}/json")
        try:
            observed = UpgradeDockerImage.model_validate(
                {
                    "image_id": body.get("Id"),
                    "os": body.get("Os"),
                    "architecture": body.get("Architecture"),
                }
            )
        except ValueError:
            raise UpgradeConflict("Accepted image platform or identity differs") from None
        if observed.image_id != image_id:
            raise UpgradeConflict("Docker returned another image identity")
        return observed

    async def inspect_image_defaults(self, image_id: str) -> UpgradeDockerImageDefaults:
        """Obtain defaults only from a present, exact accepted Linux ARM64 image."""
        if image_id not in self.images:
            raise UpgradeForbidden("Image defaults are outside the accepted release")
        body = await self._read("GET", f"/images/{image_id}/json")
        if (body.get("Id"), body.get("Os"), body.get("Architecture")) != (
            image_id,
            "linux",
            "arm64",
        ):
            raise UpgradeConflict("Image defaults platform or identity differs")
        configuration = body.get("Config")
        if not isinstance(configuration, dict):
            raise UpgradeConflict("Immutable image execution defaults are absent")
        exposed = configuration.get("ExposedPorts") or {}
        if not isinstance(exposed, dict):
            raise UpgradeConflict("Immutable image port defaults are malformed")
        try:
            return UpgradeDockerImageDefaults.model_validate(
                {
                    "image_id": image_id,
                    "environment": configuration.get("Env") or [],
                    "entrypoint": configuration.get("Entrypoint"),
                    "command": configuration.get("Cmd"),
                    "working_dir": configuration.get("WorkingDir") or "",
                    "stop_signal": configuration.get("StopSignal") or None,
                    "exposed_ports": tuple(exposed),
                }
            )
        except ValueError:
            raise UpgradeConflict("Immutable image execution defaults are malformed") from None

    async def inspect_database_dependency(self, prepared_root: Path) -> CoreDependencyObservation:
        """Inspect only the frozen existing PostgreSQL role; no mutation path accepts it."""
        role = self.database_role
        if role is None:
            raise UpgradeUnavailable("Frozen database dependency is unavailable")
        document = read_prepared_compose(prepared_root, self.plan.source)
        project = self.plan.source.manifest.project
        if project is None:
            raise UpgradeConflict("Database dependency has no fixed project")
        resources = frozen_resources(document, project)
        body = await self._read("GET", f"/containers/{self.database_name}/json")
        observed_at = body.started_at
        config, state, networks = body.get("Config"), body.get("State"), body.get("NetworkSettings")
        labels = config.get("Labels") if isinstance(config, dict) else None
        attachments = networks.get("Networks") if isinstance(networks, dict) else None
        if (
            not isinstance(labels, dict)
            or not isinstance(state, dict)
            or not isinstance(attachments, dict)
            or body.get("Name") != "/" + self.database_name
            or body.get("Image") != role.image
            or labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.service") != "postgres"
            or state.get("Running") is not True
            or not isinstance(state.get("Health"), dict)
        ):
            raise UpgradeConflict("Database dependency identity or readiness differs")
        health = state["Health"]
        assert isinstance(health, dict)
        if health.get("Status") != "healthy":
            raise UpgradeUnavailable("Database dependency is not healthy")
        physical: dict[str, str] = {}
        for name, attachment in attachments.items():
            if not isinstance(attachment, dict) or not isinstance(attachment.get("NetworkID"), str):
                raise UpgradeConflict("Database network identity is unavailable")
            physical[name] = str(attachment["NetworkID"])
        network_ids = {
            logical: physical[physical_name]
            for logical, physical_name in self._network_names(document).items()
            if logical in role.networks and physical_name in physical
        }
        mounts = reconcile_execution_mounts(role, body.get("Mounts"), resources)
        defaults = await self.inspect_image_defaults(role.image)
        verify_database_configuration(
            body, defaults, role, project=project, network_ids=network_ids, mounts=mounts
        )
        try:
            return CoreDependencyObservation(
                role="postgres",
                container_id=cast(str, body.get("Id")),
                name=self.database_name,
                project=project,
                image_id=role.image,
                observed_at=observed_at,
                state="healthy",
                configuration_sha256=hashlib.sha256(role.model_dump_json().encode()).hexdigest(),
            )
        except ValueError:
            raise UpgradeConflict("Database dependency observation is malformed") from None

    async def fresh_dependencies(
        self,
        role: str,
        configuration: dict[str, JsonValue],
        prepared_root: Path,
        *,
        target: bool = False,
    ) -> dict[str, str]:
        """Resolve only the existing acyclic Core dependency graph from current reality."""
        dependencies = configuration.get("depends_on", {})
        permitted = {
            "coire-file-worker": {"postgres"},
            "coire-scheduler": {"postgres"},
            "coire-api": {"postgres", "coire-migrate"},
            "coire-mcp": {"postgres", "coire-migrate"},
            "coire-ops": {"coire-api"},
            "coire-web": {"coire-api"},
            "coire-migrate": {"postgres"},
            "postgres": set(),
        }
        if not isinstance(dependencies, dict) or set(dependencies) - permitted.get(role, set()):
            raise UpgradeForbidden("Frozen role requests an unsupported or cyclic dependency")
        states: dict[str, str] = {}
        observations: list[CoreDependencyObservation] = []
        for name in sorted(dependencies):
            if name == "postgres":
                observed = await self.inspect_database_dependency(prepared_root)
            elif name == "coire-migrate":
                observed = await self.inspect_migration_dependency(
                    prepared_root, candidate=target and self.plan.needs_migration
                )
            else:
                observed = await self.inspect_api_dependency(prepared_root)
            states[name] = observed.state
            observations.append(observed)
        now = datetime.now(UTC)
        if any(not 0 <= (now - item.observed_at).total_seconds() <= 5 for item in observations):
            raise UpgradeUnavailable("Core dependency observations are stale or from the future")
        return states

    async def inspect_migration_dependency(
        self, prepared_root: Path, *, candidate: bool
    ) -> CoreDependencyObservation:
        selected = await self.inspect_selected_migration(prepared_root, candidate=candidate)
        return CoreDependencyObservation(
            role="coire-migrate",
            container_id=selected.container.container_id,
            name=selected.container.name,
            project=selected.container.project,
            image_id=selected.container.image_id,
            observed_at=selected.container.observed_at,
            state="completed",
            configuration_sha256=hashlib.sha256(
                selected.configuration.model_dump_json().encode()
            ).hexdigest(),
        )

    async def inspect_selected_migration(
        self, prepared_root: Path, *, candidate: bool
    ) -> CoreSelectedMigration:
        release = self.plan.target if candidate else self.source_migration_release
        name = self.migration_candidate_name if candidate else self.migration_source_name
        role = self._migration_role(candidate=candidate)
        document = read_prepared_compose(prepared_root, release)
        project = self.plan.source.manifest.project or ""
        body = await self._read("GET", f"/containers/{name}/json")
        observed = self._migration_observation(
            body, frozen_resources(document, project), candidate=candidate
        )
        if observed.running or observed.status != "exited" or observed.exit_code != 0:
            raise UpgradeUnavailable("Migration dependency lacks terminal zero exit")
        network_ids = {
            logical: observed.network_ids[physical]
            for logical, physical in self._network_names(document).items()
            if logical in role.networks and physical in observed.network_ids
        }
        states = await self.fresh_dependencies(
            "coire-migrate", role.model_dump(mode="json"), prepared_root, target=candidate
        )
        expected = translate_migration(
            role.model_dump(mode="json"),
            role.model_dump(mode="json"),
            project=project,
            network_ids=network_ids,
            mount_bindings=observed.mounts,
            dependency_states=states,
        )
        verify_execution_configuration(
            body, await self.inspect_image_defaults(role.image), expected, network_ids=network_ids
        )
        origin = self.source_migration_selection
        return CoreSelectedMigration(
            container=observed,
            release=release,
            configuration=role,
            origin_job_id=self.plan.operation.job_id
            if candidate
            else origin.origin_job_id
            if origin
            else None,
            origin_generation=self.plan.operation.generation
            if candidate
            else origin.origin_generation
            if origin
            else None,
            origin_plan_sha256=self.plan.authority_sha256()
            if candidate
            else origin.origin_plan_sha256
            if origin
            else None,
        )

    async def inspect_api_dependency(self, prepared_root: Path) -> CoreDependencyObservation:
        source_name = next(
            (name for name, item in self.source_names.items() if item.service == "coire-api"), None
        )
        if source_name is None:
            raise UpgradeUnavailable("API dependency is absent from the accepted Source")
        body = await self._read("GET", f"/containers/{source_name}/json")
        state = body.get("State")
        health = state.get("Health") if isinstance(state, dict) else None
        if (
            isinstance(state, dict)
            and state.get("Running") is True
            and isinstance(health, dict)
            and health.get("Status") == "healthy"
        ):
            role = self.source_names[source_name]
            observed = await self.inspect_verified_source(
                source_name, prepared_root, dependency_states={}
            )
        else:
            name = next(
                (
                    name
                    for name, item in self.candidate_names.items()
                    if item.service == "coire-api"
                ),
                None,
            )
            if name is None:
                raise UpgradeUnavailable("API dependency has no healthy accepted owner")
            role = self.candidate_names[name]
            observed = await self.inspect_verified_candidate(
                name, prepared_root, dependency_states={}
            )
        if not observed.running or observed.health != "healthy":
            raise UpgradeUnavailable("API dependency is not healthy")
        return CoreDependencyObservation(
            role="coire-api",
            container_id=observed.container_id,
            name=observed.name,
            project=observed.project,
            image_id=observed.image_id,
            observed_at=observed.observed_at,
            state="healthy",
            configuration_sha256=role.configuration_sha256,
        )

    async def inspect_source(
        self, name: str, resources: UpgradeComposeResources | None = None
    ) -> UpgradeDockerContainer:
        """Observe a fixed Compose source role without granting authority over unrelated containers."""
        service = self.source_names.get(name)
        if service is None:
            raise UpgradeForbidden("Container is outside the accepted source cohort")
        body = await self._read("GET", f"/containers/{name}/json")
        return self._source_observation(name, body, resources)

    async def preflight_cohort(
        self,
        journal: ExecutorJournal,
        authority: ExecutorAuthority,
        *,
        prepared_root: Path,
        dependency_states: dict[str, str],
    ) -> CoreSourceSnapshot:
        """Validate the whole candidate cohort without stopping or creating any container."""
        authority.verify()
        if journal.plan.authority_sha256() != self.plan.authority_sha256():
            raise UpgradeConflict("Core preflight journal authority differs")
        source_document = read_prepared_compose(prepared_root, self.plan.source)
        target_document = read_prepared_compose(prepared_root, self.plan.target)
        project = authority.claim.project
        source_resources = frozen_resources(source_document, project)
        target_resources = frozen_resources(target_document, project)
        if source_resources != target_resources or self._network_names(
            source_document
        ) != self._network_names(target_document):
            raise UpgradeConflict("Core preflight cannot change physical resources or networks")
        targets = {item.service: item for item in self.plan.target.manifest.services}
        observations: list[UpgradeDockerContainer] = []
        for name, service in self.source_names.items():
            observed = await self.inspect_verified_source(
                name, prepared_root, dependency_states=dependency_states
            )
            observations.append(observed)
            target = targets.get(service.service)
            if target is None:
                raise UpgradeConflict("Core preflight cannot remove a Source role")
            dependency_states = await self.fresh_dependencies(
                target.service, target.configuration, prepared_root
            )
            await self.inspect_image_defaults(target.image_id)
            networks = service.configuration.get("networks")
            if not isinstance(networks, dict):
                raise UpgradeConflict("Frozen Source network configuration is malformed")
            translate_service(
                service,
                target,
                project=project,
                network_ids={
                    logical: observed.network_ids[physical]
                    for logical, physical in self._network_names(source_document).items()
                    if logical in networks and physical in observed.network_ids
                },
                mount_bindings=observed.mounts,
                dependency_states=dependency_states,
            )
        if set(targets) != {item.service for item in observations}:
            raise UpgradeConflict("Core preflight cannot add an unobserved role")
        authority.verify()
        # Target/default validation may take longer than the observation window.
        # Recheck every physical Source concurrently at the snapshot boundary;
        # the journal still enforces its unchanged five-second freshness bound.
        observations = list(
            await asyncio.gather(
                *(
                    self.inspect_verified_source(name, prepared_root, dependency_states={})
                    for name in self.source_names
                )
            )
        )
        authority.verify()
        return journal.freeze_source(tuple(observations))

    async def inspect_verified_source(
        self,
        name: str,
        prepared_root: Path,
        *,
        dependency_states: dict[str, str],
        require_dependency_readiness: bool = True,
    ) -> UpgradeDockerContainer:
        """Verify the complete frozen execution configuration before retaining recovery identity."""
        service = self.source_names.get(name)
        if service is None:
            raise UpgradeForbidden("Container is outside the accepted source cohort")
        dependency_states = (
            await self.fresh_dependencies(service.service, service.configuration, prepared_root)
            if require_dependency_readiness
            else {}
        )
        document = read_prepared_compose(prepared_root, self.plan.source)
        project = self.plan.source.manifest.project
        if project is None:
            raise UpgradeConflict("Core Source has no fixed Compose project")
        resources = frozen_resources(document, project)
        body = await self._read("GET", f"/containers/{name}/json")
        observed = self._source_observation(name, body, resources)
        defaults = await self.inspect_image_defaults(service.image_id)
        networks = service.configuration.get("networks")
        if not isinstance(networks, dict):
            raise UpgradeConflict("Frozen Source network configuration is malformed")
        from coire_upgrade_executor.telemetry import source_configuration

        try:
            verify_source_configuration(
                service,
                body,
                defaults,
                project=project,
                network_ids={
                    logical: observed.network_ids[physical]
                    for logical, physical in self._network_names(document).items()
                    if logical in networks and physical in observed.network_ids
                },
                mounts=observed.mounts,
                dependency_states=dependency_states,
                enforce_dependency_readiness=require_dependency_readiness,
            )
        except UpgradeConflict:
            source_configuration.add(1, {"service": service.service, "outcome": "drift"})
            raise
        source_configuration.add(1, {"service": service.service, "outcome": "matched"})
        return observed

    async def inspect_verified_candidate(
        self, name: str, prepared_root: Path, *, dependency_states: dict[str, str]
    ) -> UpgradeDockerContainer:
        service = self.candidate_names.get(name)
        if service is None:
            raise UpgradeForbidden("Container is outside the accepted candidate cohort")
        dependency_states = await self.fresh_dependencies(
            service.service, service.configuration, prepared_root, target=True
        )
        document = read_prepared_compose(prepared_root, self.plan.target)
        project = self.plan.source.manifest.project
        if project is None:
            raise UpgradeConflict("Core candidate has no fixed Compose project")
        resources = frozen_resources(document, project)
        body = await self._read("GET", f"/containers/{name}/json")
        observed = self._source_observation(name, body, resources, candidate=True)
        networks = service.configuration.get("networks")
        if not isinstance(networks, dict):
            raise UpgradeConflict("Frozen candidate network configuration is malformed")
        verify_source_configuration(
            service,
            body,
            await self.inspect_image_defaults(service.image_id),
            project=project,
            network_ids={
                logical: observed.network_ids[physical]
                for logical, physical in self._network_names(document).items()
                if logical in networks and physical in observed.network_ids
            },
            mounts=observed.mounts,
            dependency_states=dependency_states,
        )
        return observed

    def _source_observation(
        self,
        name: str,
        body: DockerRead,
        resources: UpgradeComposeResources | None,
        *,
        candidate: bool = False,
    ) -> UpgradeDockerContainer:
        service = (self.candidate_names if candidate else self.source_names).get(name)
        if service is None:
            raise UpgradeForbidden("Container is outside the accepted source cohort")
        config, state = body.get("Config"), body.get("State")
        network_settings = body.get("NetworkSettings")
        if (
            not isinstance(config, dict)
            or not isinstance(state, dict)
            or not isinstance(network_settings, dict)
        ):
            raise UpgradeConflict("Source container metadata is incomplete")
        labels, networks = config.get("Labels"), network_settings.get("Networks")
        if not isinstance(labels, dict) or not isinstance(networks, dict):
            raise UpgradeConflict("Source container ownership is incomplete")
        if (
            labels.get("com.docker.compose.project") != self.plan.source.manifest.project
            or labels.get("com.docker.compose.service") != service.service
            or labels.get("com.docker.compose.oneoff", "False") not in {"False", "false"}
            or body.get("Name") != "/" + name
            or body.get("Image") != service.image_id
        ):
            raise UpgradeConflict("Source container ownership or image differs")
        if candidate and any(
            labels.get(key) != value for key, value in self._candidate_labels().items()
        ):
            raise UpgradeConflict("Candidate container belongs to another accepted operation")
        if not candidate and self.source_selection is not None:
            selected = next(
                item for item in self.source_selection.services if item.container.name == name
            )
            if body.get("Id") != selected.container.container_id:
                raise UpgradeConflict("Selected Core Source container identity differs")
            origin = {
                "coire.upgrade.job_id": selected.origin_job_id,
                "coire.upgrade.generation": str(selected.origin_generation)
                if selected.origin_generation is not None
                else None,
                "coire.upgrade.plan_sha256": selected.origin_plan_sha256,
            }
            if any(labels.get(key) != value for key, value in origin.items()):
                raise UpgradeConflict("Selected Core Source origin labels differ")
        elif not candidate and any(
            labels.get(key) is not None
            for key in (
                "coire.upgrade.job_id",
                "coire.upgrade.generation",
                "coire.upgrade.plan_sha256",
            )
        ):
            raise UpgradeConflict("Initial Core Source has an unrecorded managed origin")
        health = state.get("Health")
        observed_health = health.get("Status") if isinstance(health, dict) else "none"
        try:
            observed = UpgradeDockerContainer.model_validate(
                {
                    "container_id": body.get("Id"),
                    "name": name,
                    "service": service.service,
                    "project": self.plan.source.manifest.project,
                    "image_id": body.get("Image"),
                    "running": state.get("Running"),
                    "observed_at": body.started_at,
                    "health": observed_health,
                    "exit_code": state.get("ExitCode"),
                    "mounts": reconcile_mounts(service, body.get("Mounts"), resources)
                    if resources is not None
                    else [],
                    "network_ids": {
                        key: value.get("NetworkID")
                        for key, value in networks.items()
                        if isinstance(value, dict)
                    },
                }
            )
        except ValueError:
            raise UpgradeConflict("Source container observation is malformed") from None
        if len(observed.network_ids) != len(networks):
            raise UpgradeConflict("Source container network identity is incomplete")
        if resources is None and (
            body.get("Mounts") not in ([], None)
            or service.configuration.get("volumes")
            or service.configuration.get("secrets")
        ):
            raise UpgradeConflict("Source mounts require hash-verified immutable resource bindings")
        return observed

    def _candidate_labels(self) -> dict[str, str]:
        return {
            "coire.upgrade.job_id": self.plan.operation.job_id,
            "coire.upgrade.generation": str(self.plan.operation.generation),
            "coire.upgrade.plan_sha256": self.plan.authority_sha256(),
        }

    def _migration_role(self, *, candidate: bool) -> UpgradeComposeMigration:
        name = self.migration_candidate_name if candidate else self.migration_source_name
        role = self.migration_roles.get(name)
        if role is None:
            raise UpgradeUnavailable("Migration requires verified Source and Target frozen roles")
        if not candidate:
            return role
        control = {
            "UPGRADES_ENABLED": "true",
            "UPGRADE_PROJECT": self.plan.source.manifest.project or "",
            "UPGRADE_EXECUTOR_ID": self.executor_id,
            "UPGRADE_MIGRATION_JOB_ID": self.plan.operation.job_id,
            "UPGRADE_MIGRATION_GENERATION": str(self.plan.operation.generation),
        }
        if any(
            key in role.environment and role.environment[key] != value
            for key, value in control.items()
        ):
            raise UpgradeConflict("Frozen migration overrides its accepted control identity")
        return UpgradeComposeMigration.model_validate(
            role.model_dump()
            | {
                "environment": role.environment | control,
            }
        )

    def _migration_observation(
        self,
        body: DockerRead,
        resources: UpgradeComposeResources,
        *,
        candidate: bool,
    ) -> UpgradeDockerMigration:
        name = self.migration_candidate_name if candidate else self.migration_source_name
        role = self._migration_role(candidate=candidate)
        config, state, attachments = (
            body.get("Config"),
            body.get("State"),
            body.get("NetworkSettings"),
        )
        labels = config.get("Labels") if isinstance(config, dict) else None
        networks = attachments.get("Networks") if isinstance(attachments, dict) else None
        if (
            not isinstance(labels, dict)
            or not isinstance(state, dict)
            or not isinstance(networks, dict)
        ):
            raise UpgradeConflict("Migration container ownership is incomplete")
        if (
            body.get("Name") != "/" + name
            or body.get("Image") != role.image
            or labels.get("com.docker.compose.project") != self.plan.source.manifest.project
            or labels.get("com.docker.compose.service") != "coire-migrate"
            or labels.get("com.docker.compose.oneoff", "False") not in {"False", "false"}
            or (
                candidate
                and any(labels.get(key) != value for key, value in self._candidate_labels().items())
            )
        ):
            raise UpgradeConflict("Migration container identity differs from its accepted role")
        if not candidate:
            selected = self.source_migration_selection
            origin = {
                "coire.upgrade.job_id": selected.origin_job_id if selected else None,
                "coire.upgrade.generation": str(selected.origin_generation)
                if selected and selected.origin_generation is not None
                else None,
                "coire.upgrade.plan_sha256": selected.origin_plan_sha256 if selected else None,
            }
            if (selected is not None and body.get("Id") != selected.container.container_id) or any(
                labels.get(key) != value for key, value in origin.items()
            ):
                raise UpgradeConflict(
                    "Retained migration container identity or historical origin differs"
                )
        try:
            observed = UpgradeDockerMigration.model_validate(
                {
                    "container_id": body.get("Id"),
                    "name": name,
                    "project": self.plan.source.manifest.project,
                    "image_id": body.get("Image"),
                    "observed_at": body.started_at,
                    "running": state.get("Running"),
                    "status": state.get("Status"),
                    "exit_code": state.get("ExitCode"),
                    "mounts": reconcile_execution_mounts(role, body.get("Mounts"), resources),
                    "network_ids": {
                        key: value.get("NetworkID")
                        for key, value in networks.items()
                        if isinstance(value, dict)
                    },
                }
            )
        except ValueError:
            raise UpgradeConflict("Migration container observation is malformed") from None
        if len(observed.network_ids) != len(networks):
            raise UpgradeConflict("Migration physical network identities are incomplete")
        return observed

    async def create_migration(
        self,
        effect: CoreExecutorEffect,
        journal: ExecutorJournal,
        authority: ExecutorAuthority,
        *,
        prepared_root: Path,
        dependency_states: dict[str, str],
    ) -> UpgradeDockerMigration:
        authority.verify()
        if (
            not self.plan.needs_migration
            or effect.action != "create_migration"
            or (journal.plan.authority_sha256() != self.plan.authority_sha256())
        ):
            raise UpgradeForbidden("Migration creation requires its accepted schema plan")
        existing = journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
        if existing is None or existing.model_dump(
            exclude={"outcome", "observed_container_id"}
        ) != effect.model_dump(exclude={"outcome", "observed_container_id"}):
            raise UpgradeConflict("Migration creation has no matching durable intent")
        source, target = self._migration_role(candidate=False), self._migration_role(candidate=True)
        dependency_states = await self.fresh_dependencies(
            "coire-migrate", target.model_dump(mode="json"), prepared_root, target=True
        )
        if effect.image_id != target.image:
            raise UpgradeConflict("Migration creation image differs")
        source_doc = read_prepared_compose(prepared_root, self.source_migration_release)
        target_doc = read_prepared_compose(prepared_root, self.plan.target)
        resources = frozen_resources(source_doc, authority.claim.project)
        if resources != frozen_resources(
            target_doc, authority.claim.project
        ) or self._network_names(source_doc) != self._network_names(target_doc):
            raise UpgradeConflict("Migration physical resources differ from retained Source")
        source_body = await self._read("GET", f"/containers/{self.migration_source_name}/json")
        observed_source = self._migration_observation(source_body, resources, candidate=False)
        if (
            observed_source.running
            or observed_source.status != "exited"
            or observed_source.exit_code != 0
        ):
            raise UpgradeConflict(
                "Migration Source one-shot container has no terminal zero-exit proof"
            )
        network_ids = {
            logical: observed_source.network_ids[physical]
            for logical, physical in self._network_names(source_doc).items()
            if logical in source.networks and physical in observed_source.network_ids
        }
        source_payload = translate_migration(
            source.model_dump(mode="json"),
            source.model_dump(mode="json"),
            project=authority.claim.project,
            network_ids=network_ids,
            mount_bindings=observed_source.mounts,
            dependency_states=dependency_states,
        )
        verify_execution_configuration(
            source_body,
            await self.inspect_image_defaults(source.image),
            source_payload,
            network_ids=network_ids,
        )
        target_defaults = await self.inspect_image_defaults(target.image)
        payload = translate_migration(
            source.model_dump(mode="json"),
            target.model_dump(mode="json"),
            project=authority.claim.project,
            network_ids=network_ids,
            mount_bindings=observed_source.mounts,
            dependency_states=dependency_states,
        )
        bind_physical_network_names(payload, self._network_names(source_doc))
        payload["Labels"].update(self._candidate_labels())
        body = await self._read(
            "GET", f"/containers/{self.migration_candidate_name}/json", absent_candidate=True
        )
        if not body:
            if existing.outcome != "intent":
                raise UpgradeConflict("Migration creation uncertainty forbids blind recreation")
            remaining = (effect.deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise UpgradeForbidden("Migration creation deadline has elapsed")
            journal.dispatch_intent(effect.effect_id, authority)
            authority.verify()
            timeout = min(5, remaining)
            try:
                async with (
                    asyncio.timeout(timeout),
                    self._client.stream(
                        "POST",
                        self.proxy.copy_with(path="/v1.46/containers/create"),
                        params={"name": self.migration_candidate_name},
                        json=payload,
                        timeout=timeout,
                    ) as response,
                ):
                    if response.status_code != 201:
                        raise UpgradeUnavailable("Migration creation remains uncertain")
            except (httpx.HTTPError, TimeoutError):
                raise UpgradeUnavailable("Migration creation remains uncertain") from None
            authority.verify()
            body = await self._read("GET", f"/containers/{self.migration_candidate_name}/json")
        observed = self._migration_observation(body, resources, candidate=True)
        verify_execution_configuration(body, target_defaults, payload, network_ids=network_ids)
        journal.observe_migration_container(effect.effect_id, observed)
        return observed

    async def start_migration(
        self,
        effect: CoreExecutorEffect,
        journal: ExecutorJournal,
        authority: ExecutorAuthority,
        *,
        prepared_root: Path,
        dependency_states: dict[str, str],
    ) -> UpgradeDockerMigration:
        authority.verify()
        if (
            effect.action != "start_migration"
            or not self.plan.needs_migration
            or (journal.plan.authority_sha256() != self.plan.authority_sha256())
        ):
            raise UpgradeForbidden("Migration start requires its accepted durable schema effect")
        existing = journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
        if existing is None or existing.model_dump(
            exclude={"outcome", "observed_container_id"}
        ) != effect.model_dump(exclude={"outcome", "observed_container_id"}):
            raise UpgradeConflict("Migration start has no matching durable intent")
        created = [
            item
            for path in journal.effects.root.glob("*.json")
            if (item := journal.effects.read(path.name, CoreExecutorEffect)) is not None
            and item.action == "create_migration"
            and item.outcome == "complete"
            and item.observed_container_id == effect.container_id
            and item.image_id == effect.image_id
        ]
        if len(created) != 1:
            raise UpgradeConflict("Migration start lacks exact container creation proof")
        document = read_prepared_compose(prepared_root, self.plan.target)
        resources = frozen_resources(document, authority.claim.project)
        body = await self._read("GET", f"/containers/{self.migration_candidate_name}/json")
        observed = self._migration_observation(body, resources, candidate=True)
        if (observed.container_id, observed.image_id) != (effect.container_id, effect.image_id):
            raise UpgradeConflict("Migration start container identity differs")
        role = self._migration_role(candidate=True)
        networks = {
            logical: observed.network_ids[physical]
            for logical, physical in self._network_names(document).items()
            if logical in role.networks and physical in observed.network_ids
        }
        dependency_states = await self.fresh_dependencies(
            "coire-migrate", role.model_dump(mode="json"), prepared_root, target=True
        )
        expected = translate_migration(
            role.model_dump(mode="json"),
            role.model_dump(mode="json"),
            project=authority.claim.project,
            network_ids=networks,
            mount_bindings=observed.mounts,
            dependency_states=dependency_states,
        )
        defaults = await self.inspect_image_defaults(effect.image_id)
        verify_execution_configuration(body, defaults, expected, network_ids=networks)
        if observed.status in {"running", "exited"}:
            journal.observe_migration_container(effect.effect_id, observed)
            return observed
        if observed.status != "created" or existing.outcome != "intent":
            raise UpgradeConflict("Migration start uncertainty forbids automatic retry")
        remaining = (effect.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise UpgradeForbidden("Migration start deadline has elapsed")
        journal.dispatch_intent(effect.effect_id, authority)
        authority.verify()
        timeout = min(5, remaining)
        try:
            async with (
                asyncio.timeout(timeout),
                self._client.stream(
                    "POST",
                    self.proxy.copy_with(path=f"/v1.46/containers/{observed.container_id}/start"),
                    timeout=timeout,
                ) as response,
            ):
                if response.status_code not in {204, 304}:
                    raise UpgradeUnavailable("Migration start remains uncertain")
        except (httpx.HTTPError, TimeoutError):
            raise UpgradeUnavailable("Migration start remains uncertain") from None
        authority.verify()
        body = await self._read("GET", f"/containers/{self.migration_candidate_name}/json")
        result = self._migration_observation(body, resources, candidate=True)
        verify_execution_configuration(body, defaults, expected, network_ids=networks)
        journal.observe_migration_container(effect.effect_id, result)
        return result

    async def verify_migration_candidate_absent(self) -> None:
        """Only an exact accepted candidate's explicit 404 proves physical absence."""
        body = await self._read(
            "GET", f"/containers/{self.migration_candidate_name}/json", absent_candidate=True
        )
        if body:
            raise UpgradeConflict("Unstarted migration candidate is physically present")

    async def inspect_migration(
        self,
        *,
        prepared_root: Path,
        dependency_states: dict[str, str],
    ) -> UpgradeDockerMigration:
        document = read_prepared_compose(prepared_root, self.plan.target)
        resources = frozen_resources(document, self.plan.source.manifest.project or "")
        body = await self._read("GET", f"/containers/{self.migration_candidate_name}/json")
        observed = self._migration_observation(body, resources, candidate=True)
        role = self._migration_role(candidate=True)
        dependency_states = await self.fresh_dependencies(
            "coire-migrate", role.model_dump(mode="json"), prepared_root, target=True
        )
        networks = {
            logical: observed.network_ids[physical]
            for logical, physical in self._network_names(document).items()
            if logical in role.networks and physical in observed.network_ids
        }
        expected = translate_migration(
            role.model_dump(mode="json"),
            role.model_dump(mode="json"),
            project=observed.project,
            network_ids=networks,
            mount_bindings=observed.mounts,
            dependency_states=dependency_states,
        )
        verify_execution_configuration(
            body,
            await self.inspect_image_defaults(observed.image_id),
            expected,
            network_ids=networks,
        )
        return observed

    async def create_candidate(
        self,
        effect: CoreExecutorEffect,
        journal: ExecutorJournal,
        authority: ExecutorAuthority,
        *,
        prepared_root: Path,
        dependency_states: dict[str, str],
    ) -> UpgradeDockerContainer:
        """Create one fixed candidate only after exact Source stop and configuration proof."""
        authority.verify()
        if effect.action != "create_candidate" or (
            journal.plan.authority_sha256() != self.plan.authority_sha256()
        ):
            raise UpgradeForbidden("Candidate creation requires its accepted durable effect")
        existing = journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
        if existing is None or existing.model_dump(
            exclude={"outcome", "observed_container_id"}
        ) != effect.model_dump(exclude={"outcome", "observed_container_id"}):
            raise UpgradeConflict("Candidate creation has no matching durable intent")
        candidate_name = next(
            (name for name, item in self.candidate_names.items() if item.service == effect.service),
            None,
        )
        source_name = next(
            (name for name, item in self.source_names.items() if item.service == effect.service),
            None,
        )
        if candidate_name is None or source_name is None:
            raise UpgradeForbidden("Candidate creation is outside the accepted cohort")
        source = self.source_names[source_name]
        target = self.candidate_names[candidate_name]
        dependency_states = await self.fresh_dependencies(
            target.service, target.configuration, prepared_root, target=True
        )
        if effect.image_id != target.image_id:
            raise UpgradeConflict("Candidate creation image differs")
        source_document = read_prepared_compose(prepared_root, self.plan.source)
        target_document = read_prepared_compose(prepared_root, self.plan.target)
        resources = frozen_resources(source_document, authority.claim.project)
        if resources != frozen_resources(target_document, authority.claim.project) or (
            self._network_names(source_document) != self._network_names(target_document)
        ):
            raise UpgradeConflict("Candidate physical resources differ from retained Source")
        source_body = await self._read("GET", f"/containers/{source_name}/json")
        source_observed = self._source_observation(source_name, source_body, resources)
        stopped = [
            item
            for path in journal.effects.root.glob("*.json")
            if (item := journal.effects.read(path.name, CoreExecutorEffect)) is not None
            and item.action == "stop_source"
            and item.service == effect.service
            and item.outcome == "complete"
            and item.container_id == source_observed.container_id
            and item.observed_container_id == source_observed.container_id
        ]
        if source_observed.running or len(stopped) != 1:
            raise UpgradeConflict("Candidate creation lacks exact retained Source stop evidence")
        source_networks = source.configuration.get("networks")
        if not isinstance(source_networks, dict):
            raise UpgradeConflict("Source network configuration is malformed")
        network_ids = {
            logical: source_observed.network_ids[physical]
            for logical, physical in self._network_names(source_document).items()
            if logical in source_networks and physical in source_observed.network_ids
        }
        verify_source_configuration(
            source,
            source_body,
            await self.inspect_image_defaults(source.image_id),
            project=authority.claim.project,
            network_ids=network_ids,
            mounts=source_observed.mounts,
            dependency_states=dependency_states,
        )
        defaults = await self.inspect_image_defaults(target.image_id)
        payload = translate_service(
            source,
            target,
            project=authority.claim.project,
            network_ids=network_ids,
            mount_bindings=source_observed.mounts,
            dependency_states=dependency_states,
        )
        bind_physical_network_names(payload, self._network_names(source_document))
        payload["Labels"].update(self._candidate_labels())
        body = await self._read("GET", f"/containers/{candidate_name}/json", absent_candidate=True)
        if not body:
            if existing.outcome != "intent":
                raise UpgradeConflict("Candidate creation uncertainty forbids blind recreation")
            remaining = (effect.deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise UpgradeForbidden("Candidate creation deadline has elapsed")
            journal.dispatch_intent(effect.effect_id, authority)
            authority.verify()
            timeout = min(5, remaining)
            try:
                async with (
                    asyncio.timeout(timeout),
                    self._client.stream(
                        "POST",
                        self.proxy.copy_with(path="/v1.46/containers/create"),
                        params={"name": candidate_name},
                        json=payload,
                        timeout=timeout,
                    ) as response,
                ):
                    if response.status_code != 201:
                        raise UpgradeUnavailable("Candidate creation remains uncertain")
            except (httpx.HTTPError, TimeoutError):
                raise UpgradeUnavailable("Candidate creation remains uncertain") from None
            authority.verify()
            body = await self._read("GET", f"/containers/{candidate_name}/json")
        observed = self._source_observation(candidate_name, body, resources, candidate=True)
        verify_source_configuration(
            target,
            body,
            defaults,
            project=authority.claim.project,
            network_ids=network_ids,
            mounts=observed.mounts,
            dependency_states=dependency_states,
        )
        journal.observe(effect.effect_id, observed)
        return observed

    async def stop_source(
        self,
        effect: CoreExecutorEffect,
        journal: ExecutorJournal,
        authority: ExecutorAuthority,
        *,
        prepared_root: Path,
        dependency_states: dict[str, str],
    ) -> UpgradeDockerContainer:
        """Stop only the exact drained Source role, retaining its container and volume references."""
        authority.verify()
        if (
            self.plan.authority_sha256() != journal.plan.authority_sha256()
            or effect.action != "stop_source"
        ):
            raise UpgradeForbidden("Docker stop requires the accepted Source effect")
        existing = journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
        if existing is None or existing.model_dump(
            exclude={"outcome", "observed_container_id"}
        ) != effect.model_dump(exclude={"outcome", "observed_container_id"}):
            raise UpgradeConflict("Docker stop has no matching durable intent")
        name = next(
            (
                name
                for name, service in self.source_names.items()
                if service.service == effect.service
            ),
            None,
        )
        if name is None:
            raise UpgradeForbidden("Docker stop is outside the fixed Source roles")
        document = read_prepared_compose(prepared_root, self.plan.source)
        resources = frozen_resources(document, authority.claim.project)
        body = await self._read("GET", f"/containers/{name}/json")
        observed = self._source_observation(name, body, resources)
        if observed.container_id != effect.container_id or observed.image_id != effect.image_id:
            raise UpgradeConflict("Docker stop Source identity changed")
        defaults = await self.inspect_image_defaults(effect.image_id)
        dependency_states = await self.fresh_dependencies(
            self.source_names[name].service, self.source_names[name].configuration, prepared_root
        )
        source_networks = self.source_names[name].configuration.get("networks")
        if not isinstance(source_networks, dict):
            raise UpgradeConflict("Frozen Source network configuration is malformed")
        configuration = verify_source_configuration(
            self.source_names[name],
            body,
            defaults,
            project=authority.claim.project,
            network_ids={
                logical: observed.network_ids[physical]
                for logical, physical in self._network_names(document).items()
                if logical in source_networks and physical in observed.network_ids
            },
            mounts=observed.mounts,
            dependency_states=dependency_states,
        )
        if not observed.running:
            journal.observe(effect.effect_id, observed)
            return observed
        if existing.outcome != "intent":
            raise UpgradeConflict(
                "Docker stop uncertainty requires actual stopped-state reconciliation"
            )
        now = datetime.now(UTC)
        state = journal.state()
        deadline = (
            min(effect.deadline, state.outage_deadline)
            if state.outage_deadline is not None
            else effect.deadline
        )
        remaining = (deadline - now).total_seconds()
        graceful = configuration["StopTimeout"]
        if remaining <= graceful:
            raise UpgradeForbidden("Docker stop cannot fit its captured graceful deadline")
        journal.dispatch_intent(effect.effect_id, authority, now=now)
        authority.verify()
        timeout = min(remaining, graceful + 1)
        url = self.proxy.copy_with(path=f"/v1.46/containers/{observed.container_id}/stop")
        try:
            async with (
                asyncio.timeout(timeout),
                self._client.stream(
                    "POST",
                    url,
                    params={"t": str(graceful)},
                    timeout=timeout,
                ) as response,
            ):
                if response.status_code not in {204, 304}:
                    raise UpgradeUnavailable("Source container stop remains uncertain")
        except (httpx.HTTPError, TimeoutError):
            raise UpgradeUnavailable("Source container stop remains uncertain") from None
        # An HTTP success is not positive process termination evidence.
        authority.verify()
        stopped = await self.inspect_source(name, resources)
        journal.observe(effect.effect_id, stopped)
        return stopped

    async def candidate_lifecycle(
        self,
        effect: CoreExecutorEffect,
        journal: ExecutorJournal,
        authority: ExecutorAuthority,
        *,
        prepared_root: Path,
        dependency_states: dict[str, str],
    ) -> UpgradeDockerContainer:
        """Start or recover-stop the exact journal-created candidate, with no blind retry."""
        authority.verify()
        if effect.action not in {"start_candidate", "stop_candidate"} or (
            journal.plan.authority_sha256() != self.plan.authority_sha256()
        ):
            raise UpgradeForbidden("Candidate lifecycle requires its accepted durable effect")
        saved = journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
        if saved is None or saved.model_dump(
            exclude={"outcome", "observed_container_id"}
        ) != effect.model_dump(exclude={"outcome", "observed_container_id"}):
            raise UpgradeConflict("Candidate lifecycle has no matching durable intent")
        name = next(
            (name for name, item in self.candidate_names.items() if item.service == effect.service),
            None,
        )
        if name is None:
            raise UpgradeForbidden("Candidate lifecycle is outside the accepted cohort")
        created = [
            item
            for path in journal.effects.root.glob("*.json")
            if (item := journal.effects.read(path.name, CoreExecutorEffect)) is not None
            and item.action == "create_candidate"
            and item.service == effect.service
            and item.outcome == "complete"
            and item.observed_container_id == effect.container_id
        ]
        if len(created) != 1:
            raise UpgradeConflict("Candidate lifecycle lacks exact creation evidence")
        document = read_prepared_compose(prepared_root, self.plan.target)
        resources = frozen_resources(document, authority.claim.project)
        body = await self._read("GET", f"/containers/{name}/json")
        observed = self._source_observation(name, body, resources, candidate=True)
        if (observed.container_id, observed.image_id) != (effect.container_id, effect.image_id):
            raise UpgradeConflict("Candidate lifecycle identity differs")
        service = self.candidate_names[name]
        if effect.action == "start_candidate":
            dependency_states = await self.fresh_dependencies(
                service.service, service.configuration, prepared_root, target=True
            )
        logical_networks = service.configuration.get("networks")
        if not isinstance(logical_networks, dict):
            raise UpgradeConflict("Candidate network configuration is malformed")
        configuration = verify_source_configuration(
            service,
            body,
            await self.inspect_image_defaults(effect.image_id),
            project=authority.claim.project,
            network_ids={
                logical: observed.network_ids[physical]
                for logical, physical in self._network_names(document).items()
                if logical in logical_networks and physical in observed.network_ids
            },
            mounts=observed.mounts,
            dependency_states=dependency_states,
            enforce_dependency_readiness=effect.action == "start_candidate",
        )
        starting = effect.action == "start_candidate"
        if observed.running == starting:
            journal.observe(effect.effect_id, observed)
            return observed
        if saved.outcome != "intent":
            raise UpgradeConflict(
                "Candidate lifecycle uncertainty requires actual-state reconciliation"
            )
        state = journal.state()
        deadline = (
            min(effect.deadline, state.outage_deadline)
            if starting and state.outage_deadline
            else effect.deadline
        )
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        grace = configuration["StopTimeout"]
        if remaining <= (0 if starting else grace):
            raise UpgradeForbidden("Candidate lifecycle cannot fit its captured deadline")
        journal.dispatch_intent(effect.effect_id, authority)
        authority.verify()
        timeout = min(remaining, 5 if starting else grace + 1)
        action = "start" if starting else "stop"
        try:
            async with (
                asyncio.timeout(timeout),
                self._client.stream(
                    "POST",
                    self.proxy.copy_with(
                        path=f"/v1.46/containers/{observed.container_id}/{action}"
                    ),
                    params=None if starting else {"t": str(grace)},
                    timeout=timeout,
                ) as response,
            ):
                if response.status_code not in {204, 304}:
                    raise UpgradeUnavailable("Candidate lifecycle remains uncertain")
        except (httpx.HTTPError, TimeoutError):
            raise UpgradeUnavailable("Candidate lifecycle remains uncertain") from None
        authority.verify()
        body = await self._read("GET", f"/containers/{name}/json")
        result = self._source_observation(name, body, resources, candidate=True)
        verify_source_configuration(
            service,
            body,
            await self.inspect_image_defaults(effect.image_id),
            project=authority.claim.project,
            network_ids={
                logical: result.network_ids[physical]
                for logical, physical in self._network_names(document).items()
                if logical in logical_networks and physical in result.network_ids
            },
            mounts=result.mounts,
            dependency_states=dependency_states,
            enforce_dependency_readiness=starting,
        )
        journal.observe(effect.effect_id, result)
        return result

    @staticmethod
    def _network_names(document: dict[str, JsonValue]) -> dict[str, str]:
        networks = document.get("networks")
        if not isinstance(networks, dict):
            raise UpgradeConflict("Frozen physical network bindings are absent")
        result = {}
        for logical, entry in networks.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise UpgradeConflict("Frozen physical network name is unavailable")
            result[logical] = str(entry["name"])
        return result

    async def remove_failed_migration(
        self,
        effect: CoreExecutorEffect,
        journal: ExecutorJournal,
        authority: ExecutorAuthority,
        *,
        prepared_root: Path,
    ) -> CoreExecutorEffect:
        """Retire an exact failed unchanged attempt, preserving successful migration images."""
        authority.verify()
        saved = journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
        terminal = journal.effects.read("core-migration.json", CoreExecutorEffect)
        proof = journal.store.read("migration-proof.json", UpgradeMigrationExecution)
        if (
            effect.action != "remove_migration"
            or effect.container_id is None
            or saved is None
            or saved != effect
            or journal.plan.authority_sha256() != self.plan.authority_sha256()
            or journal.state().recovery_captured_at is None
            or terminal is None
            or terminal.outcome != "complete"
            or terminal.observed_container_id != effect.container_id
            or terminal.image_id != effect.image_id
            or proof is None
            or not proof.failed_unchanged
            or effect.image_id != self._migration_role(candidate=True).image
        ):
            raise UpgradeForbidden("Migration removal lacks exact unchanged recovery authority")

        async def absent_id() -> bool:
            authority.verify()
            try:
                async with (
                    asyncio.timeout(5),
                    self._client.stream(
                        "GET",
                        self.proxy.copy_with(path=f"/v1.46/containers/{effect.container_id}/json"),
                        timeout=5,
                    ) as response,
                ):
                    if response.status_code == 404:
                        return True
                    if response.status_code == 200:
                        return False
                    raise UpgradeUnavailable("Migration absence remains uncertain")
            except (httpx.HTTPError, TimeoutError):
                raise UpgradeUnavailable("Migration absence remains uncertain") from None

        body = await self._read(
            "GET", f"/containers/{self.migration_candidate_name}/json", absent_candidate=True
        )
        if not body:
            if not await absent_id():
                raise UpgradeConflict("Migration name is absent but retained container remains")
            authority.verify()
            return journal.observe_migration_removed(
                effect.effect_id, effect.container_id, body.started_at
            )
        if saved.outcome != "intent":
            raise UpgradeConflict("Migration removal uncertainty forbids blind deletion retry")
        observed = await self.inspect_migration(prepared_root=prepared_root, dependency_states={})
        journal.complete_migration(terminal.effect_id, observed, proof)
        if observed.container_id != effect.container_id:
            raise UpgradeConflict("Migration removal terminal identity differs")
        remaining = (effect.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise UpgradeForbidden("Migration removal recovery deadline has elapsed")
        journal.dispatch_intent(effect.effect_id, authority)
        authority.verify()
        timeout = min(5, remaining)
        try:
            async with (
                asyncio.timeout(timeout),
                self._client.stream(
                    "DELETE",
                    self.proxy.copy_with(path=f"/v1.46/containers/{effect.container_id}"),
                    params={"force": "false", "v": "false"},
                    timeout=timeout,
                ) as response,
            ):
                if response.status_code not in {204, 404}:
                    raise UpgradeUnavailable("Migration removal remains uncertain")
        except (httpx.HTTPError, TimeoutError):
            raise UpgradeUnavailable("Migration removal remains uncertain") from None
        absence_at = datetime.now(UTC)
        if not await absent_id() or await self._read(
            "GET", f"/containers/{self.migration_candidate_name}/json", absent_candidate=True
        ):
            raise UpgradeConflict("Migration removal lacks positive ID and name absence")
        authority.verify()
        return journal.observe_migration_removed(effect.effect_id, effect.container_id, absence_at)

    async def remove_candidate(
        self,
        effect: CoreExecutorEffect,
        journal: ExecutorJournal,
        authority: ExecutorAuthority,
        *,
        prepared_root: Path,
    ) -> CoreExecutorEffect:
        """Delete only a stopped journal-created candidate; never force or delete volumes."""
        authority.verify()
        if (
            effect.action != "remove_candidate"
            or effect.container_id is None
            or journal.plan.authority_sha256() != self.plan.authority_sha256()
            or journal.state().recovery_captured_at is None
        ):
            raise UpgradeForbidden("Candidate removal requires its accepted recovery authority")
        saved = journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
        created = journal.effects.read(
            "core-create-candidate-" + str(effect.service) + ".json", CoreExecutorEffect
        )
        stopped = journal.effects.read(
            "core-stop-candidate-" + str(effect.service) + ".json", CoreExecutorEffect
        )
        if (
            saved is None
            or saved.model_dump(exclude={"outcome", "observed_container_id"})
            != effect.model_dump(exclude={"outcome", "observed_container_id"})
            or created is None
            or created.action != "create_candidate"
            or created.service != effect.service
            or created.outcome != "complete"
            or created.observed_container_id != effect.container_id
            or created.image_id != effect.image_id
            or stopped is None
            or stopped.action != "stop_candidate"
            or stopped.service != effect.service
            or stopped.outcome != "complete"
            or stopped.observed_container_id != effect.container_id
            or stopped.image_id != effect.image_id
        ):
            raise UpgradeConflict("Candidate removal lacks exact creation and stop proofs")
        for proof in (created, stopped):
            journal._validate(proof)
        name = next(
            (name for name, role in self.candidate_names.items() if role.service == effect.service),
            None,
        )
        if name is None:
            raise UpgradeForbidden("Candidate removal is outside the accepted cohort")
        if self.candidate_names[name].image_id != effect.image_id:
            raise UpgradeConflict("Candidate removal image differs from accepted Target")

        async def absent_id() -> bool:
            authority.verify()
            try:
                async with (
                    asyncio.timeout(5),
                    self._client.stream(
                        "GET",
                        self.proxy.copy_with(path=f"/v1.46/containers/{effect.container_id}/json"),
                        timeout=5,
                    ) as response,
                ):
                    if response.status_code == 404:
                        return True
                    if response.status_code == 200:
                        return False
                    raise UpgradeUnavailable("Candidate removal absence remains uncertain")
            except (httpx.HTTPError, TimeoutError):
                raise UpgradeUnavailable("Candidate removal absence remains uncertain") from None

        body = await self._read("GET", f"/containers/{name}/json", absent_candidate=True)
        if not body:
            if not await absent_id():
                raise UpgradeConflict("Candidate name is absent but its retained container remains")
            authority.verify()
            return journal.observe_removed(effect.effect_id, effect.container_id, body.started_at)
        if saved.outcome != "intent":
            raise UpgradeConflict("Candidate removal uncertainty forbids blind deletion retry")
        document = read_prepared_compose(prepared_root, self.plan.target)
        resources = frozen_resources(document, authority.claim.project)
        observed = self._source_observation(name, body, resources, candidate=True)
        if (
            observed.container_id != effect.container_id
            or observed.image_id != effect.image_id
            or observed.running
        ):
            raise UpgradeConflict("Removal candidate is running or differs from retained identity")
        role = self.candidate_names[name]
        logical_networks = role.configuration.get("networks")
        if not isinstance(logical_networks, dict):
            raise UpgradeConflict("Removal candidate network configuration is malformed")
        verify_source_configuration(
            role,
            body,
            await self.inspect_image_defaults(effect.image_id),
            project=authority.claim.project,
            network_ids={
                logical: observed.network_ids[physical]
                for logical, physical in self._network_names(document).items()
                if logical in logical_networks and physical in observed.network_ids
            },
            mounts=observed.mounts,
            dependency_states={},
            enforce_dependency_readiness=False,
        )
        remaining = (effect.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise UpgradeForbidden("Candidate removal recovery deadline has elapsed")
        journal.dispatch_intent(effect.effect_id, authority)
        authority.verify()
        timeout = min(5, remaining)
        try:
            async with (
                asyncio.timeout(timeout),
                self._client.stream(
                    "DELETE",
                    self.proxy.copy_with(path=f"/v1.46/containers/{effect.container_id}"),
                    params={"force": "false", "v": "false"},
                    timeout=timeout,
                ) as response,
            ):
                if response.status_code not in {204, 404}:
                    raise UpgradeUnavailable("Candidate removal remains uncertain")
        except (httpx.HTTPError, TimeoutError):
            raise UpgradeUnavailable("Candidate removal remains uncertain") from None
        absence_at = datetime.now(UTC)
        if not await absent_id() or await self._read(
            "GET", f"/containers/{name}/json", absent_candidate=True
        ):
            raise UpgradeConflict("Candidate removal has no positive identity and name absence")
        authority.verify()
        return journal.observe_removed(effect.effect_id, effect.container_id, absence_at)

    async def start_source(
        self,
        effect: CoreExecutorEffect,
        journal: ExecutorJournal,
        authority: ExecutorAuthority,
        *,
        prepared_root: Path,
        dependency_states: dict[str, str],
    ) -> UpgradeDockerContainer:
        """Restore an exact retained Source without recreating its container or mounts."""
        authority.verify()
        if journal.state().recovery_captured_at is None:
            raise UpgradeForbidden("Source start requires the retained recovery epoch")
        if effect.action != "start_source" or (
            self.plan.authority_sha256() != journal.plan.authority_sha256()
        ):
            raise UpgradeForbidden("Docker restoration requires the accepted Source effect")
        existing = journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
        if existing is None or existing.model_dump(
            exclude={"outcome", "observed_container_id"}
        ) != effect.model_dump(exclude={"outcome", "observed_container_id"}):
            raise UpgradeConflict("Docker restoration has no matching durable intent")
        name = next(
            (
                name
                for name, service in self.source_names.items()
                if service.service == effect.service
            ),
            None,
        )
        if name is None:
            raise UpgradeForbidden("Docker restoration is outside the fixed Source roles")
        document = read_prepared_compose(prepared_root, self.plan.source)
        resources = frozen_resources(document, authority.claim.project)
        body = await self._read("GET", f"/containers/{name}/json")
        observed = self._source_observation(name, body, resources)
        if observed.container_id != effect.container_id or observed.image_id != effect.image_id:
            raise UpgradeConflict("Docker restoration Source identity changed")
        networks = self.source_names[name].configuration.get("networks")
        if not isinstance(networks, dict):
            raise UpgradeConflict("Frozen Source network configuration is malformed")
        verify_source_configuration(
            self.source_names[name],
            body,
            await self.inspect_image_defaults(effect.image_id),
            project=authority.claim.project,
            network_ids={
                logical: observed.network_ids[physical]
                for logical, physical in self._network_names(document).items()
                if logical in networks and physical in observed.network_ids
            },
            mounts=observed.mounts,
            dependency_states=dependency_states,
            enforce_dependency_readiness=False,
        )
        if observed.running:
            journal.observe(effect.effect_id, observed)
            return observed
        if existing.outcome != "intent":
            raise UpgradeConflict(
                "Docker restoration uncertainty requires running-state reconciliation"
            )
        state = journal.state()
        if state.recovery_deadline is None:
            raise UpgradeForbidden("Docker restoration has no captured recovery deadline")
        remaining = (
            min(effect.deadline, state.recovery_deadline) - datetime.now(UTC)
        ).total_seconds()
        if remaining <= 0:
            raise UpgradeForbidden("Docker restoration deadline has elapsed")
        journal.dispatch_intent(effect.effect_id, authority)
        authority.verify()
        timeout = min(5, remaining)
        url = self.proxy.copy_with(path=f"/v1.46/containers/{observed.container_id}/start")
        try:
            async with (
                asyncio.timeout(timeout),
                self._client.stream("POST", url, timeout=timeout) as response,
            ):
                if response.status_code not in {204, 304}:
                    raise UpgradeUnavailable("Source container restoration remains uncertain")
        except (httpx.HTTPError, TimeoutError):
            raise UpgradeUnavailable("Source container restoration remains uncertain") from None
        authority.verify()
        restored = await self.inspect_source(name, resources)
        journal.observe(effect.effect_id, restored)
        return restored
