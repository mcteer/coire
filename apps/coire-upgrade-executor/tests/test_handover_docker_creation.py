"""Fixed passive candidate creation uses a real kernel owner and sealed role metadata.

Docker is an isolated fault transport; these tests do not claim composed acceptance.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from test_handover_observer import prepared_executor
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_api.upgrades.handover_host_hold import publish_handover_hold
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import CoreHandoverPreparation
from coire_core.settings import Settings
from coire_upgrade_executor.compose import _translate_execution
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_docker import HandoverDocker
from coire_upgrade_executor.ownership import bootstrap_ownership


@pytest.mark.parametrize(
    "fault,starting",
    [
        (None, False),
        ("lost_response", False),
        ("lost_absent", False),
        ("foreign", False),
        ("missing_hold", False),
        (None, True),
        ("lost_start_running", True),
        ("lost_start_stopped", True),
        ("unexpected_running", True),
        ("restarted", True),
    ],
)
async def test_passive_creation_reconciles_lost_response_without_second_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None, starting: bool
) -> None:
    import coire_upgrade_executor.handover_docker as module

    monkeypatch.setattr(module, "verify_executor_namespace", lambda seed: None)
    value = core_plan()
    source, role = prepared_executor(tmp_path, value.source, "sha256:" + "e" * 64)
    candidate, _ = prepared_executor(tmp_path, value.target, "sha256:" + "f" * 64)
    seed, proof = prepared_bootstrap(source)
    now = datetime.now(UTC)
    request = CoreHandoverPreparation(
        incumbent=seed,
        candidate_release=candidate,
        candidate_runtime_id="candidate-executor",
        candidate_image_id="sha256:" + "f" * 64,
        generation=2,
        initiator_user_id=candidate.prepared_by,
        idempotency_key=uuid4(),
        prepared_at=now,
        deadline_at=now + timedelta(seconds=120),
    )
    journal = HandoverCreationJournal(tmp_path / "creation", request)
    networks = {name: str(index + 1) * 64 for index, name in enumerate(role.networks)}
    source_payload = _translate_execution(
        role,
        role,
        role="coire-upgrade-executor",
        project="coire",
        network_ids=networks,
        mount_bindings=(),
        dependency_states={},
    )
    candidate_payload: dict[str, Any] | None = None
    posts: list[httpx.Request] = []
    start_posts: list[httpx.Request] = []
    candidate_running = fault == "unexpected_running"
    candidate_started = now

    def container(payload: dict[str, Any], identity: str, *, running: bool) -> dict[str, Any]:
        return {
            "Id": identity,
            "Name": "/" + request.candidate_name,
            "Image": payload["Image"],
            "Config": {
                "User": payload["User"],
                "Env": payload["Env"],
                "Entrypoint": None,
                "Cmd": ["python", "-m", "coire_upgrade_executor"],
                "WorkingDir": "",
                "StopTimeout": 5,
                "Healthcheck": payload["Healthcheck"],
                "ExposedPorts": payload["ExposedPorts"],
                "Labels": payload["Labels"],
            },
            "HostConfig": payload["HostConfig"],
            "Mounts": [],
            "NetworkSettings": {
                "Networks": {
                    name: {
                        "NetworkID": identity,
                        "Aliases": ["coire-upgrade-executor"],
                    }
                    for name, identity in networks.items()
                }
            },
            "State": {
                "Running": running,
                "Health": {"Status": "healthy"},
                "StartedAt": (
                    candidate_started
                    if identity == "a" * 64 and running
                    else now - timedelta(seconds=1)
                ).isoformat(),
            },
        }

    def transport(http_request: httpx.Request) -> httpx.Response:
        nonlocal candidate_payload, candidate_running, candidate_started
        path = http_request.url.path.removeprefix("/v1.46")
        if http_request.method == "POST" and path == "/containers/" + "a" * 64 + "/start":
            retained_start = journal.current_start()
            assert retained_start is not None and retained_start.outcome == "dispatched"
            start_posts.append(http_request)
            candidate_running = fault != "lost_start_stopped"
            candidate_started = datetime.now(UTC)
            if fault in {"lost_start_running", "lost_start_stopped"}:
                raise httpx.ReadTimeout("lost candidate start response", request=http_request)
            return httpx.Response(204)
        if http_request.method == "POST":
            assert path == "/containers/create"
            assert dict(http_request.url.params) == {"name": request.candidate_name}
            retained = journal.current()
            assert retained is not None and retained.outcome == "dispatched"
            posts.append(http_request)
            candidate_payload = json.loads(http_request.content)
            if fault == "lost_absent":
                candidate_payload = None
            if fault in {"lost_response", "lost_absent"}:
                raise httpx.ReadTimeout("lost create response", request=http_request)
            return httpx.Response(201, json={"Id": "a" * 64})
        assert http_request.method == "GET"
        if path.startswith("/images/"):
            image = path.removeprefix("/images/").removesuffix("/json")
            assert image in {seed.executor_image_id, request.candidate_image_id}
            return httpx.Response(
                200,
                json={
                    "Id": image,
                    "Os": "linux",
                    "Architecture": "arm64",
                    "Config": {"Cmd": ["python", "-m", "coire_upgrade_executor"]},
                },
            )
        if path == f"/containers/{seed.executor_container_id}/json":
            return httpx.Response(
                200, json=container(source_payload, seed.executor_container_id, running=True)
            )
        assert path in {
            f"/containers/{request.candidate_name}/json",
            "/containers/" + "a" * 64 + "/json",
        }
        if fault == "foreign":
            foreign = source_payload | {
                "Image": request.candidate_image_id,
                "Labels": {"com.docker.compose.project": "other"},
            }
            return httpx.Response(200, json=container(foreign, "a" * 64, running=False))
        if candidate_payload is None:
            return httpx.Response(404)
        return httpx.Response(
            200, json=container(candidate_payload, "a" * 64, running=candidate_running)
        )

    settings = Settings(
        upgrades_enabled=True,
        upgrade_prepared_dir=str(tmp_path / "prepared"),
        upgrade_coordination_dir=str(tmp_path / "coordination"),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), trust_env=False, follow_redirects=False
    ) as client:
        with bootstrap_ownership(
            tmp_path / "coordination", seed, proof, settings.upgrade_executor_id
        ) as authority:
            if fault == "missing_hold":
                with pytest.raises(UpgradeForbidden, match="handover hold"):
                    HandoverDocker(settings, journal, authority, client)
                assert not posts
                return
            publish_handover_hold(settings, request)
            docker = HandoverDocker(settings, journal, authority, client)
            if fault == "foreign":
                with pytest.raises(UpgradeConflict):
                    await docker.create(proof)
                assert not posts
                return
            if fault in {"lost_response", "lost_absent"}:
                with pytest.raises(UpgradeUnavailable):
                    await docker.create(proof)
            if fault == "lost_absent":
                with pytest.raises(UpgradeUnavailable):
                    await docker.create(proof)
                assert len(posts) == 1
                retained = journal.current()
                assert retained is not None and retained.candidate_container_id is None
            else:
                result = await docker.create(proof)
                assert result.candidate_container_id == "a" * 64
                assert len(posts) == 1
                assert await docker.create(proof) == result
            if starting:
                journal.candidate_seed()
                if fault in {"lost_start_running", "lost_start_stopped"}:
                    with pytest.raises(UpgradeUnavailable):
                        await docker.start_candidate(proof)
                if fault == "lost_start_stopped":
                    with pytest.raises(UpgradeUnavailable, match="redispatch"):
                        await docker.start_candidate(proof)
                    assert len(start_posts) == 1
                elif fault == "unexpected_running":
                    with pytest.raises(UpgradeConflict, match="dispatched"):
                        await docker.start_candidate(proof)
                    assert not start_posts
                else:
                    observed_start = await docker.start_candidate(proof)
                    assert observed_start.outcome == "observed"
                    assert len(start_posts) == 1
                    assert await docker.start_candidate(proof) == observed_start
                    if fault == "restarted":
                        candidate_started = datetime.now(UTC)
                        with pytest.raises(UpgradeConflict, match="restarted"):
                            await docker.start_candidate(proof)
                        assert len(start_posts) == 1
            authority.verify()
            assert authority.claim.executor_container_id == seed.executor_container_id


async def test_start_requires_a_published_candidate_seed_before_docker_effect(
    tmp_path: Path,
) -> None:
    # The journal cannot grant start merely because a container ID was observed.
    from test_handover_creation_journal import preparation

    from coire_core.errors import UpgradeConflict

    value = preparation()
    journal = HandoverCreationJournal(tmp_path, value)
    effect = journal.begin(now=value.prepared_at)
    journal.observe(effect, "a" * 64, observed_at=value.prepared_at, now=value.prepared_at)
    with pytest.raises(UpgradeUnavailable, match="seed"):
        journal.begin_start(now=value.prepared_at)
    assert journal.current_start() is None
    with pytest.raises(UpgradeConflict, match="dispatched"):
        journal.observe_start(
            started_at=value.prepared_at, observed_at=value.prepared_at, now=value.prepared_at
        )
