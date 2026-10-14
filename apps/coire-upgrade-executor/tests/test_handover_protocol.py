"""Passive HTTP protocol must bind the exact observed candidate, not a shared service alias."""

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr
from test_handover_creation_journal import preparation

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import ExecutorPassiveReadiness
from coire_core.settings import Settings
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_protocol import observe_passive_protocol


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "public_ip",
        "loopback",
        "network",
        "seed",
        "runtime",
        "future",
        "duplicate",
        "oversize",
        "health_log",
        "after_restart",
        "after_network",
        "after_health",
    ],
)
async def test_passive_protocol_has_only_exact_candidate_authority(
    tmp_path: Path,
    fault: str | None,
) -> None:
    value = preparation()
    journal = HandoverCreationJournal(tmp_path / "journal", value)
    created = journal.begin(now=value.prepared_at)
    journal.observe(created, "a" * 64, observed_at=value.prepared_at, now=value.prepared_at)
    seed = journal.candidate_seed()
    intent = journal.begin_start(now=value.prepared_at)
    journal.dispatch_start(intent, now=value.prepared_at)
    journal.observe_start(
        started_at=value.prepared_at, observed_at=value.prepared_at, now=value.prepared_at
    )
    settings = Settings(
        upgrades_enabled=True, upgrade_executor_token=SecretStr("coire_upgrade_private-test")
    )
    ip = (
        "8.8.8.8" if fault == "public_ip" else "127.0.0.1" if fault == "loopback" else "172.30.0.12"
    )
    body: dict[str, Any] = {
        "Id": seed.executor_container_id,
        "Image": seed.executor_image_id,
        "State": {
            "Running": True,
            "Health": {"Status": "healthy"},
            "StartedAt": value.prepared_at.isoformat(),
        },
        "NetworkSettings": {
            "Networks": {
                "coire-internal": {
                    "NetworkID": "b" * 64 if fault == "network" else "c" * 64,
                    "IPAddress": ip,
                }
            }
        },
    }
    docker = Mock()
    docker.seed = value.incumbent
    docker.preparation, docker.journal, docker.settings = value, journal, settings
    docker.candidate_configuration = ({}, None, {"coire-internal": "c" * 64}, ())
    after = copy.deepcopy(body)
    if fault == "health_log":
        after["State"]["Health"]["Log"] = [{"ExitCode": 0}]
    elif fault == "after_restart":
        after["State"]["StartedAt"] = datetime.now(UTC).isoformat()
    elif fault == "after_network":
        after["NetworkSettings"]["Networks"]["coire-internal"]["IPAddress"] = "172.30.0.13"
    elif fault == "after_health":
        after["State"]["Health"]["Status"] = "unhealthy"
    docker.read = AsyncMock(side_effect=[body, after])
    docker.verified_candidate_state = Mock(side_effect=lambda physical: physical["State"])
    docker.verify_owner = Mock()
    docker.target_document = {"networks": {"coire-internal": {"name": "coire-internal"}}}
    calls = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == "http://172.30.0.12:8011/passive"
        assert request.headers["authorization"] == "Bearer coire_upgrade_private-test"
        proof = ExecutorPassiveReadiness(
            preparation_sha256=value.sha256(),
            candidate_seed_sha256="f" * 64 if fault == "seed" else seed.sha256(),
            generation=value.generation,
            candidate_runtime_id="wrong" if fault == "runtime" else seed.executor_runtime_id,
            candidate_container_id=seed.executor_container_id,
            candidate_image_id=seed.executor_image_id,
            incumbent_runtime_id=value.incumbent.executor_runtime_id,
            incumbent_container_id=value.incumbent.executor_container_id,
            observed_at=datetime.now(UTC),
        )
        payload = proof.model_dump_json().encode()
        if fault == "future":
            from datetime import timedelta

            payload = (
                proof.model_copy(update={"observed_at": datetime.now(UTC) + timedelta(seconds=1)})
                .model_dump_json()
                .encode()
            )
        elif fault == "duplicate":
            payload = b'{"generation":999,' + payload[1:]
        elif fault == "oversize":
            payload = b"x" * 16385
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        docker.client = http
        if fault and fault != "health_log":
            with pytest.raises((UpgradeConflict, UpgradeForbidden, UpgradeUnavailable)):
                await observe_passive_protocol(docker)
            if fault in {"public_ip", "loopback", "network"}:
                assert not calls
        else:
            proof = await observe_passive_protocol(docker)
            assert proof.candidate_seed_sha256 == seed.sha256()
            assert not proof.recovery_ready and not proof.exclusive_writer_held
            assert len(calls) == 1
            assert docker.read.await_count == 2
