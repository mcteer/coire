"""Stop only the exact predecessor after acknowledged, held candidate readiness."""

from __future__ import annotations

import asyncio
import ipaddress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from opentelemetry import trace

from coire_core.deploy import idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.health import ReadyResponse
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapSeed,
    CoreHandoverPreparation,
    CoreHandoverRetirementIntent,
    CoreHandoverSummary,
    DeploymentHandoverHold,
    DeploymentOwnership,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_recovery import RecoveryObserver
from coire_upgrade_executor.namespace import verify_executor_namespace
from coire_upgrade_executor.ownership import BootstrapAuthority

tracer = trace.get_tracer("coire.upgrade_executor.handover_retirement")


def retained_retirement_owner(
    settings: Settings,
    creation: HandoverCreationJournal,
    *,
    observer_role: Literal["incumbent", "candidate"] = "incumbent",
) -> tuple[BootstrapHandover, DeploymentOwnership]:
    prep, seed = creation.preparation, creation.retained_candidate_seed()
    verify_executor_namespace(prep.incumbent if observer_role == "incumbent" else seed)
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "bootstrap")
    coordination = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    current = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
    selected = bootstrap.read("selected-seed.json", CoreBootstrapSeed)
    ack = creation.store.read("commit-ack.json", CoreHandoverSummary)
    hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
    owner = idle_executor_owner(
        coordination, settings.upgrade_project, settings.upgrade_executor_id
    )
    if (
        not settings.upgrades_enabled
        or current is None
        or selected is None
        or ack is None
        or hold is None
        or owner is None
        or bootstrap.read("handover-preparation.json", CoreHandoverPreparation) != prep
        or current.phase != "committed"
        or current.generation != prep.generation
        or (
            current.incumbent_id,
            current.incumbent_container_id,
            current.incumbent_image_id,
            current.candidate_id,
            current.candidate_container_id,
            current.candidate_image_id,
        )
        != (
            prep.incumbent.executor_runtime_id,
            prep.incumbent.executor_container_id,
            prep.incumbent.executor_image_id,
            seed.executor_runtime_id,
            seed.executor_container_id,
            seed.executor_image_id,
        )
        or ack.phase != "committed"
        or ack.preparation_sha256 != prep.sha256()
        or ack.generation != prep.generation
        or ack.project != settings.upgrade_project
        or ack.candidate_release_id != prep.candidate_release.release_id
        or ack.incumbent_image_id != prep.incumbent.executor_image_id
        or ack.candidate_image_id != prep.candidate_image_id
        or ack.prepared_at != prep.prepared_at
        or ack.deadline_at != prep.deadline_at
        or not hold.released
        or (
            hold.project,
            hold.preparation_sha256,
            hold.generation,
            hold.incumbent_runtime_id,
            hold.incumbent_container_id,
            hold.candidate_runtime_id,
        )
        != (
            settings.upgrade_project,
            prep.sha256(),
            prep.generation,
            prep.incumbent.executor_runtime_id,
            prep.incumbent.executor_container_id,
            prep.candidate_runtime_id,
        )
        or (
            selected.executor_runtime_id,
            selected.executor_container_id,
            selected.executor_image_id,
            selected.executor_source,
        )
        != (
            seed.executor_runtime_id,
            seed.executor_container_id,
            seed.executor_image_id,
            seed.executor_source,
        )
        or (owner.executor_runtime_id, owner.executor_container_id, owner.source_sha256)
        != (seed.executor_runtime_id, seed.executor_container_id, selected.source.manifest_sha256)
        or owner.generation < prep.generation
    ):
        raise UpgradeConflict("Executor predecessor retirement lacks acknowledged held selection")
    return current, owner


async def observe_candidate_ready(observer: RetirementObserver) -> None:
    """Read only the attested private address and reverify it after the response."""
    await observer.verify_container(observer.candidate)
    path = f"/containers/{observer.candidate}/json"
    before = observer.verified_reads[path]
    document = observer.roles[observer.candidate][1]
    name = UpgradeDocker._network_names(document).get("coire-internal")
    network = before.get("NetworkSettings")
    attachments = network.get("Networks") if isinstance(network, dict) else None
    attachment = attachments.get(name) if isinstance(attachments, dict) and name else None
    if not isinstance(attachment, dict) or not isinstance(attachment.get("NetworkID"), str):
        raise UpgradeConflict("Executor readiness network attestation is absent")
    try:
        address = ipaddress.IPv4Address(str(attachment.get("IPAddress")))
    except ipaddress.AddressValueError:
        raise UpgradeConflict("Executor readiness address is malformed") from None
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        raise UpgradeConflict("Executor readiness address is outside its private network")
    token = observer.settings.upgrade_executor_token.get_secret_value()
    if not token.startswith("coire_upgrade_") or len(token) > 4096:
        raise UpgradeUnavailable("Executor readiness restricted credential is absent")
    payload = bytearray()
    try:
        async with asyncio.timeout(5):
            async with observer.client.stream(
                "GET",
                f"http://{address}:8011/ready",
                headers={"Authorization": "Bearer " + token},
                timeout=5,
            ) as response:
                if response.status_code != 200:
                    raise UpgradeUnavailable("Selected executor is not positively ready")
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 4096:
                        raise UpgradeUnavailable("Selected executor readiness exceeds its bound")
        proof = ReadyResponse.model_validate_json(payload)
        if proof.service != "coire-upgrade-executor":
            raise ValueError("foreign readiness service")
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise UpgradeUnavailable("Selected executor readiness response is unverified") from None
    await observer.verify_container(observer.candidate)
    after = observer.verified_reads[path]
    before_state, after_state = before.get("State"), after.get("State")
    if (
        not isinstance(before_state, dict)
        or not isinstance(after_state, dict)
        or (after_state.get("Running"), after_state.get("StartedAt"))
        != (before_state.get("Running"), before_state.get("StartedAt"))
        or after.get("NetworkSettings") != before.get("NetworkSettings")
    ):
        raise UpgradeConflict("Selected executor process or address changed during readiness")


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_retire", record_exception=False, set_status_on_exception=False
)
async def retire_incumbent(
    settings: Settings,
    creation: HandoverCreationJournal,
    api: ExecutorClient,
    http: httpx.AsyncClient,
) -> None:
    """A successful stop preserves the predecessor container and image for rollback."""
    current, owner = retained_retirement_owner(settings, creation)
    prep, seed = creation.preparation, creation.retained_candidate_seed()
    ack = creation.store.read("commit-ack.json", CoreHandoverSummary)
    if await api.handover_status(prep) != ack:
        raise UpgradeConflict("Executor retirement SQL acknowledgment changed")
    observer = RetirementObserver(
        settings,
        Path(settings.upgrade_prepared_dir),
        prep.incumbent.executor_source,
        prep.candidate_release,
        current,
        prep.incumbent.executor_container_id,
        seed.executor_container_id,
        http,
    )
    observer.verified_reads, observer.preparation, observer.settings = {}, prep, settings
    await observe_candidate_ready(observer)
    await observer.verify_container(observer.incumbent, require_healthy=False)
    if retained_retirement_owner(settings, creation) != (current, owner):
        raise UpgradeConflict("Executor writer changed before predecessor retirement")
    with creation.store.ownership("retirement-dispatch.lock"):
        intent = creation.store.read("retirement-intent.json", CoreHandoverRetirementIntent)
        if intent is None:
            intent = CoreHandoverRetirementIntent(
                preparation_sha256=prep.sha256(),
                candidate_seed_sha256=seed.sha256(),
                generation=prep.generation,
                incumbent_container_id=prep.incumbent.executor_container_id,
                candidate_container_id=seed.executor_container_id,
                intent_at=datetime.now(UTC),
            )
            creation.store.write("retirement-intent.json", intent, replace=False)
        if (
            intent.preparation_sha256 != prep.sha256()
            or intent.candidate_seed_sha256 != seed.sha256()
            or intent.generation != prep.generation
            or intent.incumbent_container_id != prep.incumbent.executor_container_id
            or intent.candidate_container_id != seed.executor_container_id
        ):
            raise UpgradeConflict("Executor retirement belongs to another predecessor")
        if intent.outcome != "intent":
            raise UpgradeUnavailable("Uncertain predecessor stop cannot be blindly redispatched")
        if retained_retirement_owner(settings, creation) != (current, owner):
            raise UpgradeConflict("Executor writer changed before predecessor stop dispatch")
        creation.store.write(
            "retirement-intent.json", intent.model_copy(update={"outcome": "dispatched"})
        )
        try:
            async with asyncio.timeout(10):
                async with http.stream(
                    "POST",
                    observer.proxy.copy_with(
                        path=f"/v1.46/containers/{prep.incumbent.executor_container_id}/stop",
                        query=b"t=5",
                    ),
                    timeout=10,
                ) as response:
                    if response.status_code not in {204, 304}:
                        raise UpgradeUnavailable("Executor predecessor stop outcome is uncertain")
        except (httpx.HTTPError, TimeoutError):
            raise UpgradeUnavailable("Executor predecessor stop response is uncertain") from None


class RetirementObserver(RecoveryObserver):
    settings: Settings


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_retirement_observe",
    record_exception=False,
    set_status_on_exception=False,
)
async def observe_retired_incumbent(
    settings: Settings,
    creation: HandoverCreationJournal,
    authority: BootstrapAuthority,
    http: httpx.AsyncClient,
) -> None:
    """A live selected candidate records positive stop, never absence or an HTTP acknowledgment."""
    intent = creation.store.read("retirement-intent.json", CoreHandoverRetirementIntent)
    if intent is None or intent.outcome != "dispatched":
        return
    authority.verify()
    current, owner = retained_retirement_owner(settings, creation, observer_role="candidate")
    if owner != authority.claim:
        raise UpgradeConflict("Retirement observer does not hold the selected writer")
    prep, seed = creation.preparation, creation.retained_candidate_seed()
    if (
        intent.preparation_sha256 != prep.sha256()
        or intent.candidate_seed_sha256 != seed.sha256()
        or intent.generation != prep.generation
        or intent.incumbent_container_id != prep.incumbent.executor_container_id
        or intent.candidate_container_id != seed.executor_container_id
    ):
        raise UpgradeConflict("Retirement observation belongs to another predecessor")
    observer = RetirementObserver(
        settings,
        Path(settings.upgrade_prepared_dir),
        prep.incumbent.executor_source,
        prep.candidate_release,
        current,
        prep.incumbent.executor_container_id,
        seed.executor_container_id,
        http,
    )
    observer.verified_reads, observer.preparation, observer.settings = {}, prep, settings
    await observer.verify_container(
        observer.incumbent, require_healthy=False, require_running=False
    )
    physical = observer.verified_reads[f"/containers/{observer.incumbent}/json"]
    state = physical.get("State")
    if (
        not isinstance(state, dict)
        or state.get("Running") is not False
        or not 0 <= (datetime.now(UTC) - physical.started_at).total_seconds() <= 5
        or physical.started_at < intent.intent_at
    ):
        raise UpgradeUnavailable("Predecessor has no positive fresh stopped observation")
    with creation.store.ownership("retirement-dispatch.lock"):
        authority.verify()
        if creation.store.read(
            "retirement-intent.json", CoreHandoverRetirementIntent
        ) != intent or retained_retirement_owner(settings, creation, observer_role="candidate") != (
            current,
            owner,
        ):
            raise UpgradeConflict("Retirement authority changed before positive stop publication")
        if not 0 <= (datetime.now(UTC) - physical.started_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Predecessor stop observation expired before publication")
        creation.store.write(
            "retirement-intent.json",
            intent.model_copy(
                update={
                    "outcome": "stopped",
                    "stopped_observed_at": physical.started_at,
                }
            ),
        )
        authority.verify()
