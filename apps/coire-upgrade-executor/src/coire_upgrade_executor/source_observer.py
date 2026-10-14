"""Verify a prepared whole Source without manufacturing an accepted upgrade plan."""

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapSourceObservation,
    CoreBootstrapPhysicalObservation,
    CoreBootstrapSeed,
    ServiceName,
    UpgradeComposeService,
    UpgradeKind,
    UpgradeRelease,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import read_prepared_compose

from .handover import PreparedRuntimeObserver

tracer = trace.get_tracer("coire.upgrade_executor.bootstrap_source")


class InitialBootstrapObserver:
    """First-install physical checks require no invented application operation.

    The executor's own readiness cannot be a prerequisite for producing its initial
    proof. Inspect its exact running container and full configuration independently;
    the whole application Source still must already be positively healthy.
    """

    def __init__(
        self,
        settings: Settings,
        prepared_root: Path,
        seed: CoreBootstrapSeed,
        client: httpx.AsyncClient,
    ) -> None:
        seed = CoreBootstrapSeed.model_validate(seed.model_dump(mode="json"))
        self.source = SourceObserver(
            settings, prepared_root, seed.source, seed.source_container_ids, client
        )
        document = read_prepared_compose(prepared_root, seed.executor_source)
        services = document.get("services")
        try:
            role = UpgradeComposeService.model_validate(
                services.get("coire-upgrade-executor") if isinstance(services, dict) else None
            )
        except ValueError:
            raise UpgradeConflict(
                "Initial executor has no supported prepared configuration"
            ) from None
        if (
            role.image != seed.executor_image_id
            or role.ports
            or set(role.networks) != {"coire-docker", "coire-internal", "coire-telemetry"}
        ):
            raise UpgradeConflict("Initial executor image or fixed topology differs")
        self.executor = PreparedRuntimeObserver()
        self.executor.proxy, self.executor.client = self.source.proxy, client
        self.executor.project = settings.upgrade_project
        self.executor.roles = {seed.executor_container_id: (role, document)}
        self.executor.service_names = {seed.executor_container_id: "coire-upgrade-executor"}
        self.seed = seed

    @tracer.start_as_current_span(
        "coire.upgrade_executor.initial_bootstrap_observe",
        record_exception=False,
        set_status_on_exception=False,
    )
    async def observe(self) -> CoreBootstrapPhysicalObservation:
        source, (executor_at, started) = await asyncio.gather(
            self.source.observe(),
            self.executor.verify_container(self.seed.executor_container_id, require_healthy=False),
        )
        observed_at = min(source.observed_at, executor_at)
        if not 0 <= (datetime.now(UTC) - observed_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Initial bootstrap physical observations expired")
        return CoreBootstrapPhysicalObservation(
            seed_sha256=self.seed.sha256(),
            source=source,
            executor_runtime_id=self.seed.executor_runtime_id,
            executor_container_id=self.seed.executor_container_id,
            executor_image_id=self.seed.executor_image_id,
            executor_started_at=started,
            observed_at=observed_at,
            configuration_verified=True,
        )


class SourceObserver(PreparedRuntimeObserver):
    """Only inspect the exact Source containers and images in a trusted preparation.

    An observation proves physical configuration and health. It does not grant
    ownership, prove a schema, publish readiness, or authorize a lifecycle effect.
    """

    def __init__(
        self,
        settings: Settings,
        prepared_root: Path,
        source: UpgradeRelease,
        container_ids: dict[ServiceName, str],
        client: httpx.AsyncClient,
    ) -> None:
        try:
            source = UpgradeRelease.model_validate(source.model_dump(mode="json"))
        except ValueError:
            raise UpgradeConflict(
                "Bootstrap Source manifest failed identity verification"
            ) from None
        self.proxy = httpx.URL(settings.upgrade_proxy_url)
        if (
            str(self.proxy) != "http://docker-socket-proxy:2375"
            or client.follow_redirects
            or client._trust_env
            or source.manifest.kind is not UpgradeKind.CONTROL_PLANE
            or source.manifest.project != settings.upgrade_project
        ):
            raise UpgradeForbidden("Bootstrap Source transport or project differs")
        services = source.manifest.services
        if (
            set(container_ids) != {item.service for item in services}
            or len(set(container_ids.values())) != len(container_ids)
            or any(re.fullmatch(r"[a-f0-9]{64}", value) is None for value in container_ids.values())
        ):
            raise UpgradeConflict("Bootstrap Source requires every exact independent container")
        document = read_prepared_compose(prepared_root, source)
        configurations = document.get("services")
        if not isinstance(configurations, dict):
            raise UpgradeConflict("Bootstrap Source preparation has no service configuration")
        self.roles, self.service_names = {}, {}
        for service in services:
            try:
                role = UpgradeComposeService.model_validate(configurations.get(service.service))
                declared = UpgradeComposeService.model_validate(service.configuration)
            except ValueError:
                raise UpgradeConflict("Bootstrap Source configuration is unsupported") from None
            if role.image != service.image_id or role != declared:
                raise UpgradeConflict("Bootstrap Source configuration differs from its manifest")
            container = container_ids[service.service]
            self.roles[container] = role, document
            self.service_names[container] = service.service
        self.client, self.project, self.source = client, settings.upgrade_project, source
        self.container_ids = dict(container_ids)

    @tracer.start_as_current_span(
        "coire.upgrade_executor.bootstrap_source_observe",
        record_exception=False,
        set_status_on_exception=False,
    )
    async def observe(self) -> BootstrapSourceObservation:
        observations = await asyncio.gather(
            *(self.verify_container(container) for container in self.roles)
        )
        observed_at = min(item[0] for item in observations)
        if not 0 <= (datetime.now(UTC) - observed_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Whole bootstrap Source observations expired")
        return BootstrapSourceObservation(
            release_id=self.source.release_id,
            source_sha256=self.source.manifest_sha256,
            project=self.project,
            container_ids=self.container_ids,
            image_ids={
                service.service: service.image_id for service in self.source.manifest.services
            },
            observed_at=observed_at,
            configuration_verified=True,
            source_healthy=True,
        )
