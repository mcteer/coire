"""Fixed lifecycle dispatch retains Source references and reconciles lost responses."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
import pytest
from test_compose_translation import service
from test_executor_journal import activation
from upgrade_fixtures import core_plan

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import CoreExecutorEffect, DeploymentOwnership, UpgradeExecutionPlan
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import publish_prepared_release
from coire_upgrade_executor.compose import translate_service
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership


def prepared_plan(root: Path) -> UpgradeExecutionPlan:
    original = core_plan()
    source_service = service()
    document = {
        "name": "coire",
        "services": {"coire-api": source_service.configuration},
        "volumes": {},
        "secrets": {},
        "networks": {"coire-internal": {"name": "coire_coire-internal"}},
    }
    payload = json.dumps(document).encode()
    digest = hashlib.sha256(payload).hexdigest()
    source_manifest = original.source.manifest.model_copy(
        update={
            "services": (source_service,),
            "compose_sha256": digest,
            "artifact_sha256s": {"compose.json": digest},
            "staged_bytes": len(payload),
        }
    )
    source = original.source.model_copy(
        update={
            "manifest": source_manifest,
            "manifest_sha256": source_manifest.sha256(),
            "prepared_artifact_id": "source",
        }
    )
    target_manifest = original.target.manifest.model_copy(
        update={
            "services": (service(image="sha256:" + "b" * 64),),
            "compatibility": original.target.manifest.compatibility.model_copy(
                update={"source_sha256s": (source.manifest_sha256,)}
            ),
        }
    )
    target = original.target.model_copy(
        update={"manifest": target_manifest, "manifest_sha256": target_manifest.sha256()}
    )
    plan = UpgradeExecutionPlan.model_validate(
        original.model_dump()
        | {
            "source": source,
            "target": target,
            "operation": original.operation.model_dump()
            | {"source_sha256": source.manifest_sha256, "target_sha256": target.manifest_sha256},
        }
    )
    artifact = root / "input-compose.json"
    artifact.write_bytes(payload)
    publish_prepared_release(root / "prepared", source, {"compose.json": artifact})
    return plan


@pytest.mark.parametrize(
    "fault", [None, "identity", "config", "lost", "blind_retry", "deadline", "outage"]
)
@pytest.mark.parametrize("action", ["stop_source", "start_source"])
async def test_source_stop_is_fixed_fsynced_and_observation_closed(
    tmp_path: Path, fault: str | None, action: Literal["stop_source", "start_source"]
) -> None:
    plan = prepared_plan(tmp_path)
    expected = translate_service(
        plan.source.manifest.services[0],
        plan.source.manifest.services[0],
        project="coire",
        network_ids={"coire-internal": "b" * 64},
    )
    body = {
        "Id": "d" * 64 if fault == "identity" else "c" * 64,
        "Name": "/coire-coire-api-1",
        "Image": expected["Image"],
        "Config": {
            "User": expected["User"],
            "Env": expected["Env"],
            "Entrypoint": None,
            "Cmd": ["python", "-m", "coire_api"],
            "WorkingDir": "",
            "StopTimeout": 5,
            "Healthcheck": expected["Healthcheck"],
            "ExposedPorts": expected["ExposedPorts"],
            "Labels": expected["Labels"],
        },
        "HostConfig": expected["HostConfig"] | {"NetworkMode": "coire_coire-internal"},
        "NetworkSettings": {
            "Networks": {
                "coire_coire-internal": {
                    "NetworkID": "b" * 64,
                    "Aliases": [expected["Labels"]["com.docker.compose.service"]],
                }
            }
        },
        "Mounts": [],
        "State": {"Running": True, "ExitCode": 0, "Health": {"Status": "healthy"}},
    }
    if fault == "config":
        body["HostConfig"]["Memory"] = 0
    if action == "start_source":
        body["State"]["Running"] = False
    journal = ExecutorJournal(tmp_path / "journal", plan)
    now = datetime.now(UTC)
    state = journal.activation(activation(journal, now), now=now)
    assert state.readiness_deadline is not None
    journal.capture_outage(now=now)
    if action == "start_source":
        state = journal.capture_recovery(now=now)
    deadline = state.recovery_deadline if action == "start_source" else state.readiness_deadline
    assert deadline is not None
    effect = CoreExecutorEffect(
        job_id=state.job_id,
        generation=state.generation,
        plan_sha256=state.plan_sha256,
        effect_id=action.replace("_", "-") + "-api",
        action=action,
        service="coire-api",
        container_id="c" * 64,
        image_id=expected["Image"],
        captured_at=now,
        deadline=deadline,
    )
    journal.intent(effect, now=now)
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="executor-source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.operation.source_sha256,
    )
    if fault in {"deadline", "outage"}:
        epoch = journal.state()
        journal.store.write(
            "epoch.json",
            epoch.model_copy(
                update={
                    "outage_captured_at": now - timedelta(seconds=301),
                    "outage_deadline": now - timedelta(seconds=1),
                    **(
                        {
                            "recovery_captured_at": now - timedelta(seconds=301),
                            "recovery_deadline": now - timedelta(seconds=1),
                        }
                        if fault == "deadline" and action == "start_source"
                        else {}
                    ),
                }
            ),
        )

    def current_effect() -> CoreExecutorEffect:
        recorded = journal.effects.read(effect.effect_id + ".json", CoreExecutorEffect)
        assert recorded is not None
        return recorded

    posts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            verb = "start" if action == "start_source" else "stop"
            assert request.url.path == "/v1.46/containers/" + "c" * 64 + "/" + verb
            assert dict(request.url.params) == ({} if verb == "start" else {"t": "5"})
            assert current_effect().outcome == "unknown"
            posts.append(request)
            if fault != "blind_retry":
                body["State"]["Running"] = action == "start_source"
            if fault in {"lost", "blind_retry"}:
                raise httpx.ReadError("lost private engine response", request=request)
            return httpx.Response(204)
        assert request.method == "GET"
        if request.url.path.startswith("/v1.46/images/"):
            return httpx.Response(
                200,
                json={
                    "Id": expected["Image"],
                    "Os": "linux",
                    "Architecture": "arm64",
                    "Config": {"Env": [], "Cmd": ["python", "-m", "coire_api"], "Entrypoint": None},
                },
            )
        assert request.url.path == "/v1.46/containers/coire-coire-api-1/json"
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
    ) as http:
        docker = UpgradeDocker(Settings(), plan, client=http)
        if fault == "config":
            with pytest.raises(UpgradeConflict):
                await docker.inspect_verified_source(
                    "coire-coire-api-1", tmp_path / "prepared", dependency_states={}
                )
            assert posts == []
        elif fault is None and action == "stop_source":
            source = await docker.inspect_verified_source(
                "coire-coire-api-1", tmp_path / "prepared", dependency_states={}
            )
            assert journal.freeze_source((source,)).services[0].container_id == "c" * 64
            assert posts == []
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:

            async def stop() -> None:
                await getattr(docker, action)(
                    effect,
                    journal,
                    authority,
                    prepared_root=tmp_path / "prepared",
                    dependency_states={},
                )

            if fault in {"identity", "config", "deadline"} or (
                fault == "outage" and action == "stop_source"
            ):
                with pytest.raises((UpgradeConflict, UpgradeForbidden)):
                    await stop()
                assert posts == []
            elif fault in {"lost", "blind_retry"}:
                with pytest.raises(UpgradeUnavailable):
                    await stop()
                assert len(posts) == 1
                if fault == "lost":
                    await stop()
                    assert current_effect().outcome == "complete"
                else:
                    with pytest.raises(UpgradeConflict):
                        await stop()
                assert len(posts) == 1
            else:
                await stop()
                assert len(posts) == 1
                assert current_effect().outcome == "complete"
                await stop()
                assert len(posts) == 1
    assert (tmp_path / "prepared/source/compose.json").exists()
