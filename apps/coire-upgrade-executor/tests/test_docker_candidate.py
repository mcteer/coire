"""Candidate creation reconciles exact owned reality rather than retrying uncertainty."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_docker_stop import prepared_plan
from test_executor_journal import activation

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreExecutorEffect,
    DeploymentOwnership,
    UpgradeDockerContainer,
    UpgradeExecutionPlan,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import publish_prepared_release, read_prepared_compose
from coire_upgrade_executor.compose import translate_service
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership


def candidate_plan(root: Path, *, migration: bool = False) -> UpgradeExecutionPlan:
    previous = prepared_plan(root)
    document = read_prepared_compose(root / "prepared", previous.source)
    document["services"] = {"coire-api": previous.target.manifest.services[0].configuration}
    if migration:
        services = document["services"]
        assert isinstance(services, dict)
        services["coire-migrate"] = {
            "image": "sha256:" + "e" * 64,
            "user": "65532:65532",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "restart": "no",
            "pids_limit": 128,
            "mem_limit": "512m",
            "networks": {"coire-db": {}, "coire-telemetry": {}},
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        }
    content = json.dumps(document).encode()
    digest = hashlib.sha256(content).hexdigest()
    manifest = previous.target.manifest.model_copy(
        update={
            "compose_sha256": digest,
            "artifact_sha256s": {"compose.json": digest},
            "staged_bytes": len(content),
        }
    )
    target = previous.target.model_copy(
        update={
            "manifest": manifest,
            "manifest_sha256": manifest.sha256(),
        }
    )
    result = UpgradeExecutionPlan.model_validate(
        previous.model_dump()
        | {
            "target": target,
            "operation": previous.operation.model_dump()
            | {"target_sha256": target.manifest_sha256},
        }
    )
    artifact = root / "target-compose.json"
    artifact.write_bytes(content)
    publish_prepared_release(root / "prepared", target, {"compose.json": artifact})
    return result


async def test_migration_image_authority_requires_verified_prepared_compose(tmp_path: Path) -> None:
    from coire_core.errors import UpgradeForbidden

    plan = candidate_plan(tmp_path, migration=True)
    image = "sha256:" + "e" * 64
    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET" and request.url.path == f"/v1.46/images/{image}/json"
        return httpx.Response(200, json={"Id": image, "Os": "linux", "Architecture": "arm64"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), trust_env=False
    ) as client:
        unverified = UpgradeDocker(Settings(), plan, client=client)
        with pytest.raises(UpgradeForbidden):
            await unverified.inspect_image(image)
        assert calls == []
        verified = UpgradeDocker(
            Settings(), plan, client=client, prepared_root=tmp_path / "prepared"
        )
        assert (await verified.inspect_image(image)).image_id == image
        assert len(calls) == 1


@pytest.mark.parametrize("fault", [None, "lost", "blind_retry", "foreign", "source_running"])
@pytest.mark.parametrize(
    "lifecycle",
    [
        None,
        "start_candidate",
        "stop_candidate",
        "remove_candidate",
        "remove_lost",
        "remove_retained_id",
        "remove_uncertain",
        "remove_running",
        "remove_foreign",
    ],
)
async def test_candidate_create_has_one_fixed_effect_and_positive_reconciliation(
    tmp_path: Path,
    fault: str | None,
    lifecycle: str | None,
) -> None:
    plan = candidate_plan(tmp_path)
    journal = ExecutorJournal(tmp_path / "journal", plan)
    now = datetime.now(UTC)
    state = journal.activation(activation(journal, now), now=now)
    assert state.readiness_deadline is not None
    source, target = plan.source.manifest.services[0], plan.target.manifest.services[0]
    posts: list[httpx.Request] = []
    deletes: list[httpx.Request] = []
    candidate: dict[str, object] | None = None
    removal = lifecycle is not None and lifecycle.startswith("remove_")
    lifecycle_action = "stop_candidate" if removal else lifecycle
    lifecycle_key = "core-" + str(lifecycle_action).replace("_", "-") + "-coire-api"
    retained_id = False

    def body(name: str, image: str, identifier: str) -> dict[str, object]:
        service = source if image == source.image_id else target
        payload = translate_service(
            service, service, project="coire", network_ids={"coire-internal": "b" * 64}
        )
        return {
            "Id": identifier,
            "Name": "/" + name,
            "Image": image,
            "Config": {
                "User": payload["User"],
                "Env": payload["Env"],
                "Entrypoint": None,
                "Cmd": ["python", "-m", "coire_api"],
                "WorkingDir": "",
                "StopTimeout": 5,
                "Healthcheck": payload["Healthcheck"],
                "ExposedPorts": payload["ExposedPorts"],
                "Labels": payload["Labels"],
            },
            "HostConfig": payload["HostConfig"] | {"NetworkMode": "coire_coire-internal"},
            "NetworkSettings": {
                "Networks": {
                    "coire_coire-internal": {
                        "NetworkID": "b" * 64,
                        "Aliases": [payload["Labels"]["com.docker.compose.service"]],
                    }
                }
            },
            "Mounts": [],
            "State": {
                "Running": fault == "source_running" and image == source.image_id,
                "ExitCode": 0,
                "Health": {"Status": "healthy"},
            },
        }

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal candidate, retained_id
        path = request.url.path
        if request.method == "DELETE":
            deletes.append(request)
            assert path == "/v1.46/containers/" + "d" * 64
            assert dict(request.url.params) == {"force": "false", "v": "false"}
            saved = journal.effects.read("core-remove-candidate-coire-api.json", CoreExecutorEffect)
            assert saved is not None and saved.outcome == "unknown"
            if lifecycle != "remove_uncertain":
                candidate = None
            retained_id = lifecycle == "remove_retained_id"
            if lifecycle == "remove_lost":
                raise httpx.ReadError("lost removal response", request=request)
            return httpx.Response(204)
        if path == "/v1.46/containers/" + "d" * 64 + "/json":
            assert request.method == "GET"
            return httpx.Response(200 if candidate is not None or retained_id else 404, json={})
        if request.method == "POST":
            posts.append(request)
            if path != "/v1.46/containers/create":
                assert lifecycle is not None and candidate is not None
                action = "start" if lifecycle == "start_candidate" else "stop"
                assert path == f"/v1.46/containers/{'d' * 64}/{action}"
                assert dict(request.url.params) == ({} if action == "start" else {"t": "5"})
                saved = journal.effects.read(lifecycle_key + ".json", CoreExecutorEffect)
                assert saved is not None and saved.outcome == "unknown"
                candidate_state = candidate["State"]
                assert isinstance(candidate_state, dict)
                candidate_state["Running"] = action == "start"
                return httpx.Response(204)
            assert path == "/v1.46/containers/create"
            saved = journal.effects.read("core-create-candidate-coire-api.json", CoreExecutorEffect)
            assert saved is not None and saved.outcome == "unknown"
            payload = json.loads(request.content)
            assert payload["Image"] == target.image_id
            assert dict(request.url.params) == {"name": next(iter(docker.candidate_names))}
            candidate = body(request.url.params["name"], target.image_id, "d" * 64)
            configuration = candidate["Config"]
            assert isinstance(configuration, dict)
            configuration["Labels"] = payload["Labels"]
            if fault == "lost":
                raise httpx.ReadError("lost response", request=request)
            return httpx.Response(201, json={"Id": "d" * 64})
        if "/images/" in path:
            image = path.split("/images/", 1)[1].removesuffix("/json")
            return httpx.Response(
                200,
                json={
                    "Id": image,
                    "Os": "linux",
                    "Architecture": "arm64",
                    "Config": {"Env": [], "Cmd": ["python", "-m", "coire_api"]},
                },
            )
        if path == "/v1.46/containers/coire-coire-api-1/json":
            return httpx.Response(200, json=body("coire-coire-api-1", source.image_id, "c" * 64))
        assert path == "/v1.46/containers/" + next(iter(docker.candidate_names)) + "/json"
        return httpx.Response(404) if candidate is None else httpx.Response(200, json=candidate)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
    docker = UpgradeDocker(Settings(), plan, client=client)
    stop = CoreExecutorEffect(
        job_id=state.job_id,
        generation=state.generation,
        plan_sha256=state.plan_sha256,
        effect_id="stop-api.json-intent",
        action="stop_source",
        service="coire-api",
        container_id="c" * 64,
        image_id=source.image_id,
        captured_at=now,
        deadline=state.readiness_deadline,
    )
    journal.intent(stop)

    journal.observe(
        stop.effect_id,
        UpgradeDockerContainer.model_validate(
            {
                "container_id": "c" * 64,
                "name": "coire-coire-api-1",
                "service": "coire-api",
                "project": "coire",
                "image_id": source.image_id,
                "observed_at": now,
                "running": False,
                "health": "none",
                "exit_code": 0,
                "network_ids": {"coire_coire-internal": "b" * 64},
            }
        ),
    )
    effect = CoreExecutorEffect(
        job_id=state.job_id,
        generation=state.generation,
        plan_sha256=state.plan_sha256,
        effect_id="core-create-candidate-coire-api",
        action="create_candidate",
        service="coire-api",
        image_id=target.image_id,
        captured_at=now,
        deadline=state.readiness_deadline,
    )
    journal.intent(effect)
    if fault == "blind_retry":
        journal.uncertain(effect.effect_id)
    if fault == "foreign":
        candidate = body(next(iter(docker.candidate_names)), target.image_id, "d" * 64)
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=state.job_id,
        operation_generation=state.generation,
        source_sha256=plan.operation.source_sha256,
    )
    try:
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:

            async def create() -> UpgradeDockerContainer:
                return await docker.create_candidate(
                    effect,
                    journal,
                    authority,
                    prepared_root=tmp_path / "prepared",
                    dependency_states={},
                )

            if fault in {"blind_retry", "foreign", "source_running"}:
                with pytest.raises(UpgradeConflict):
                    await create()
                assert posts == []
            else:
                if fault == "lost":
                    with pytest.raises(UpgradeUnavailable):
                        await create()
                observed = await create()
                assert observed.container_id == "d" * 64
                assert len(posts) == 1
                assert (await create()).container_id == observed.container_id
                assert len(posts) == 1
                if lifecycle is not None:
                    assert candidate is not None
                    candidate_state = candidate["State"]
                    assert isinstance(candidate_state, dict)
                    candidate_state["Running"] = lifecycle_action == "stop_candidate"
                    deadline: datetime | None = state.readiness_deadline
                    if lifecycle_action == "stop_candidate":
                        deadline = journal.capture_recovery().recovery_deadline
                    assert deadline is not None
                    lifecycle_effect = CoreExecutorEffect.model_validate(
                        effect.model_dump()
                        | {
                            "effect_id": lifecycle_key,
                            "action": lifecycle_action,
                            "container_id": "d" * 64,
                            "deadline": deadline,
                        }
                    )
                    journal.intent(lifecycle_effect)
                    for _ in range(2):
                        outcome = await docker.candidate_lifecycle(
                            lifecycle_effect,
                            journal,
                            authority,
                            prepared_root=tmp_path / "prepared",
                            dependency_states={},
                        )
                        assert outcome.running == (lifecycle_action == "start_candidate")
                    assert len(posts) == 2
                    if removal:
                        if lifecycle == "remove_running":
                            candidate_state["Running"] = True
                        elif lifecycle == "remove_foreign":
                            candidate["Id"] = "e" * 64
                        remove = CoreExecutorEffect.model_validate(
                            lifecycle_effect.model_dump()
                            | {
                                "effect_id": "core-remove-candidate-coire-api",
                                "action": "remove_candidate",
                            }
                        )
                        journal.intent(remove)

                        async def remove_owned() -> CoreExecutorEffect:
                            return await docker.remove_candidate(
                                remove, journal, authority, prepared_root=tmp_path / "prepared"
                            )

                        if lifecycle == "remove_lost":
                            with pytest.raises(UpgradeUnavailable):
                                await remove_owned()
                            assert (await remove_owned()).outcome == "complete"
                            assert len(deletes) == 1
                        elif lifecycle in {
                            "remove_retained_id",
                            "remove_uncertain",
                            "remove_running",
                            "remove_foreign",
                        }:
                            with pytest.raises(UpgradeConflict):
                                await remove_owned()
                            with pytest.raises(UpgradeConflict):
                                await remove_owned()
                            assert len(deletes) == (
                                0 if lifecycle in {"remove_running", "remove_foreign"} else 1
                            )
                        else:
                            assert (await remove_owned()).outcome == "complete"
                            assert (await remove_owned()).outcome == "complete"
                            assert len(deletes) == 1
    finally:
        await client.aclose()
