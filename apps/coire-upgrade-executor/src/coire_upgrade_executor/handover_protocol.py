"""Read passive protocol only at an exact Docker-attested candidate network address."""

import asyncio
import ipaddress
import json
from datetime import UTC, datetime

import httpx
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import ExecutorPassiveReadiness
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.handover_docker import HandoverDocker

tracer = trace.get_tracer("coire.upgrade_executor.handover")
MAX_PASSIVE_BYTES = 16 * 1024


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_passive_protocol",
    record_exception=False,
    set_status_on_exception=False,
)
async def observe_passive_protocol(docker: HandoverDocker) -> ExecutorPassiveReadiness:
    docker.verify_owner()
    seed = docker.journal.retained_candidate_seed()
    started = docker.journal.current_start()
    if started is None or started.outcome != "observed" or started.started_at is None:
        raise UpgradeConflict("Passive protocol has no observed candidate start")
    remaining = (docker.preparation.deadline_at - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise UpgradeUnavailable("Passive protocol original deadline elapsed")
    body = await docker.read(f"/containers/{seed.executor_container_id}/json")
    state = docker.verified_candidate_state(body)
    health = state.get("Health")
    if (
        state.get("Running") is not True
        or not isinstance(health, dict)
        or health.get("Status") != "healthy"
    ):
        raise UpgradeUnavailable("Passive candidate process is not positively healthy")
    try:
        born = datetime.fromisoformat(str(state.get("StartedAt")).replace("Z", "+00:00"))
    except ValueError:
        raise UpgradeConflict("Passive candidate process birth is malformed") from None
    if born != started.started_at:
        raise UpgradeConflict("Passive candidate process changed after observed start")
    names = UpgradeDocker._network_names(docker.target_document)
    name = names.get("coire-internal")
    network_settings = body.get("NetworkSettings")
    attachments = network_settings.get("Networks") if isinstance(network_settings, dict) else None
    attachment = (
        attachments.get(name) if isinstance(attachments, dict) and name is not None else None
    )
    configuration = docker.candidate_configuration
    if (
        configuration is None
        or not isinstance(attachment, dict)
        or attachment.get("NetworkID") != configuration[2].get("coire-internal")
    ):
        raise UpgradeForbidden("Passive protocol candidate network differs")
    try:
        address = ipaddress.IPv4Address(str(attachment.get("IPAddress")))
    except ipaddress.AddressValueError:
        raise UpgradeForbidden("Passive protocol candidate address is malformed") from None
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        raise UpgradeForbidden("Passive protocol candidate address is outside its private network")
    token = docker.settings.upgrade_executor_token.get_secret_value()
    if not token.startswith("coire_upgrade_") or len(token) > 4096:
        raise UpgradeUnavailable("Passive protocol restricted credential is unavailable")
    remaining = (docker.preparation.deadline_at - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise UpgradeUnavailable("Passive protocol original deadline elapsed")
    payload = bytearray()
    try:
        async with asyncio.timeout(min(5, remaining)):
            async with docker.client.stream(
                "GET",
                f"http://{address}:8011/passive",
                headers={"Authorization": "Bearer " + token},
                timeout=min(5, remaining),
            ) as response:
                if response.status_code != 200:
                    raise UpgradeUnavailable("Passive candidate protocol is unavailable")
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_PASSIVE_BYTES:
                        raise UpgradeUnavailable("Passive candidate protocol exceeds its bound")
    except (httpx.HTTPError, TimeoutError):
        raise UpgradeUnavailable("Passive candidate protocol transport is unavailable") from None

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate protocol field")
            result[key] = value
        return result

    try:
        json.loads(payload, object_pairs_hook=unique)
        proof = ExecutorPassiveReadiness.model_validate_json(payload)
    except ValueError:
        raise UpgradeConflict("Passive candidate protocol is malformed") from None
    if (
        proof.preparation_sha256 != docker.preparation.sha256()
        or proof.candidate_seed_sha256 != seed.sha256()
        or proof.generation != seed.generation
        or proof.candidate_runtime_id != seed.executor_runtime_id
        or proof.candidate_container_id != seed.executor_container_id
        or proof.candidate_image_id != seed.executor_image_id
        or proof.incumbent_runtime_id != docker.seed.executor_runtime_id
        or proof.incumbent_container_id != docker.seed.executor_container_id
        or not 0 <= (datetime.now(UTC) - proof.observed_at).total_seconds() <= 5
    ):
        raise UpgradeConflict("Passive protocol differs from its exact retained candidate")
    after = await docker.read(f"/containers/{seed.executor_container_id}/json")
    after_state = docker.verified_candidate_state(after)
    after_health = after_state.get("Health")
    after_network_settings = after.get("NetworkSettings")
    after_attachments = (
        after_network_settings.get("Networks") if isinstance(after_network_settings, dict) else None
    )
    after_attachment = (
        after_attachments.get(name)
        if isinstance(after_attachments, dict) and name is not None
        else None
    )
    if (
        after_state.get("Running") is not True
        or after_state.get("StartedAt") != state.get("StartedAt")
        or not isinstance(after_health, dict)
        or after_health.get("Status") != "healthy"
        or not isinstance(after_attachment, dict)
        or after_attachment.get("NetworkID") != attachment.get("NetworkID")
        or after_attachment.get("IPAddress") != attachment.get("IPAddress")
        or docker.journal.retained_candidate_seed() != seed
    ):
        raise UpgradeConflict("Passive candidate process changed during protocol observation")
    docker.verify_owner()
    if (
        datetime.now(UTC) >= docker.preparation.deadline_at
        or not 0 <= (datetime.now(UTC) - proof.observed_at).total_seconds() <= 5
    ):
        raise UpgradeUnavailable("Passive candidate protocol exhausted its original deadline")
    return proof
