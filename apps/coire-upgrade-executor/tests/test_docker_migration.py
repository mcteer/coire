"""Fixed migration containers survive lost create/start replies without repeating effects."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_compose_translation import service
from test_executor_journal import activation
from upgrade_fixtures import core_plan

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreExecutorEffect,
    DeploymentOwnership,
    UpgradeActivationConsume,
    UpgradeExecutionPlan,
    UpgradePhase,
    UpgradeRelease,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import publish_prepared_release
from coire_upgrade_executor.compose import translate_migration
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership


def prepared_migration_plan(root: Path) -> UpgradeExecutionPlan:
    original = core_plan(schema_changed=True)

    def prepare(
        release: UpgradeRelease, image: str, migration_image: str, source_sha: str
    ) -> UpgradeRelease:
        cohort = service(image=image)
        configuration = {
            "image": migration_image,
            "user": "65532:65532",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "restart": "no",
            "pids_limit": 128,
            "mem_limit": "512m",
            "environment": {"POSTGRES_HOST": "postgres"},
            "networks": {"coire-db": {}, "coire-telemetry": {}},
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        }
        document = {
            "name": "coire",
            "services": {"coire-api": cohort.configuration, "coire-migrate": configuration},
            "volumes": {},
            "secrets": {},
            "networks": {
                name: {"name": "coire_" + name}
                for name in ("coire-internal", "coire-db", "coire-telemetry")
            },
        }
        payload = json.dumps(document).encode()
        digest = hashlib.sha256(payload).hexdigest()
        manifest = release.manifest.model_copy(
            update={
                "services": (cohort,),
                "compose_sha256": digest,
                "artifact_sha256s": {"compose.json": digest},
                "staged_bytes": len(payload),
                "compatibility": release.manifest.compatibility.model_copy(
                    update={"source_sha256s": (source_sha,)}
                ),
            }
        )
        result = release.model_copy(
            update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
        )
        artifact = root / (release.prepared_artifact_id + "-compose.json")
        artifact.write_bytes(payload)
        publish_prepared_release(root / "prepared", result, {"compose.json": artifact})
        return result

    source = prepare(original.source, "sha256:" + "a" * 64, "sha256:" + "e" * 64, "f" * 64)
    target = prepare(
        original.target, "sha256:" + "b" * 64, "sha256:" + "f" * 64, source.manifest_sha256
    )
    return UpgradeExecutionPlan.model_validate(
        original.model_dump()
        | {
            "source": source,
            "target": target,
            "operation": original.operation.model_dump()
            | {"source_sha256": source.manifest_sha256, "target_sha256": target.manifest_sha256},
        }
    )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "lost_create",
        "create_unknown",
        "lost_start",
        "start_unknown",
        "source_running",
        "foreign",
    ],
)
async def test_fixed_migration_transport_never_replays_unknown_effects(
    tmp_path: Path, fault: str | None
) -> None:
    plan = prepared_migration_plan(tmp_path)
    now = datetime.now(UTC)
    journal = ExecutorJournal(tmp_path / "journal", plan, prepared_root=tmp_path / "prepared")
    consumed = activation(journal, now)
    consumed = UpgradeActivationConsume.model_validate(
        consumed.model_dump()
        | {
            "grant": consumed.grant.model_dump() | {"boundary": "migration"},
            "intent": consumed.intent.model_dump() | {"phase": UpgradePhase.MIGRATING},
        }
    )
    journal.activation(consumed, now=now)
    posts: list[httpx.Request] = []
    candidate: dict[str, object] | None = None

    def body(candidate_role: bool) -> dict[str, object]:
        role = docker._migration_role(candidate=candidate_role)
        payload = translate_migration(
            role.model_dump(mode="json"),
            role.model_dump(mode="json"),
            project="coire",
            network_ids={"coire-db": "c" * 64, "coire-telemetry": "d" * 64},
        )
        name = docker.migration_candidate_name if candidate_role else docker.migration_source_name
        return {
            "Id": ("2" if candidate_role else "1") * 64,
            "Name": "/" + name,
            "Image": role.image,
            "Config": {
                "User": payload["User"],
                "Env": payload["Env"],
                "Entrypoint": None,
                "Cmd": ["-m", "coire_api.upgrades.migration_runner"],
                "WorkingDir": "",
                "StopTimeout": 5,
                "ExposedPorts": {},
                "Labels": payload["Labels"]
                | (docker._candidate_labels() if candidate_role else {}),
            },
            "HostConfig": payload["HostConfig"] | {"NetworkMode": "coire_coire-db"},
            "NetworkSettings": {
                "Networks": {
                    "coire_coire-db": {"NetworkID": "c" * 64, "Aliases": ["coire-migrate"]},
                    "coire_coire-telemetry": {"NetworkID": "d" * 64, "Aliases": ["coire-migrate"]},
                }
            },
            "Mounts": [],
            "State": {
                "Running": not candidate_role and fault == "source_running",
                "Status": "created"
                if candidate_role
                else "running"
                if fault == "source_running"
                else "exited",
                "ExitCode": 0,
            },
        }

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal candidate
        if request.method == "POST":
            posts.append(request)
            if request.url.path == "/v1.46/containers/create":
                assert dict(request.url.params) == {"name": docker.migration_candidate_name}
                saved = journal.effects.read("create-migration.json", CoreExecutorEffect)
                assert saved is not None and saved.outcome == "unknown"
                payload = json.loads(request.content)
                assert payload["Image"] == docker._migration_role(candidate=True).image
                assert f"UPGRADE_MIGRATION_JOB_ID={plan.operation.job_id}" in payload["Env"]
                assert "Cmd" not in payload and "Entrypoint" not in payload
                candidate = body(True)
                if fault == "lost_create":
                    raise httpx.ReadError("lost", request=request)
                return httpx.Response(201, json={"Id": "2" * 64})
            assert request.url.path == f"/v1.46/containers/{'2' * 64}/start"
            assert not request.url.params and candidate is not None
            state = candidate["State"]
            assert isinstance(state, dict)
            state.update({"Status": "exited", "Running": False})
            if fault == "lost_start":
                raise httpx.ReadError("lost", request=request)
            return httpx.Response(204)
        if "/images/" in request.url.path:
            image = request.url.path.split("/images/", 1)[1].removesuffix("/json")
            return httpx.Response(
                200,
                json={
                    "Id": image,
                    "Os": "linux",
                    "Architecture": "arm64",
                    "Config": {"Env": [], "Cmd": ["-m", "coire_api.upgrades.migration_runner"]},
                },
            )
        if request.url.path == f"/v1.46/containers/{docker.migration_source_name}/json":
            return httpx.Response(200, json=body(False))
        assert request.url.path == f"/v1.46/containers/{docker.migration_candidate_name}/json"
        return httpx.Response(404) if candidate is None else httpx.Response(200, json=candidate)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), trust_env=False
    ) as client:
        docker = UpgradeDocker(Settings(), plan, client=client, prepared_root=tmp_path / "prepared")
        effect = CoreExecutorEffect(
            job_id=plan.operation.job_id,
            generation=1,
            plan_sha256=plan.authority_sha256(),
            effect_id="create-migration",
            action="create_migration",
            image_id=docker._migration_role(candidate=True).image,
            captured_at=now,
            deadline=now + timedelta(seconds=120),
        )
        journal.intent(effect)
        if fault == "create_unknown":
            journal.uncertain(effect.effect_id)
        if fault == "foreign":
            candidate = body(True)
            config = candidate["Config"]
            assert isinstance(config, dict)
            config["Labels"] = {"com.docker.compose.project": "another-project"}
        owner = DeploymentOwnership(
            project="coire",
            owner_kind="executor",
            owner_identity="source",
            boot_identity=host_boot_identity(),
            generation=1,
            job_id=plan.operation.job_id,
            operation_generation=1,
            source_sha256=plan.operation.source_sha256,
        )
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
            if fault in {"create_unknown", "source_running", "foreign"}:
                with pytest.raises(UpgradeConflict):
                    await docker.create_migration(
                        effect,
                        journal,
                        authority,
                        prepared_root=tmp_path / "prepared",
                        dependency_states={},
                    )
                assert posts == []
                return
            if fault == "lost_create":
                with pytest.raises(UpgradeUnavailable):
                    await docker.create_migration(
                        effect,
                        journal,
                        authority,
                        prepared_root=tmp_path / "prepared",
                        dependency_states={},
                    )
            created = await docker.create_migration(
                effect,
                journal,
                authority,
                prepared_root=tmp_path / "prepared",
                dependency_states={},
            )
            assert len(posts) == 1
            start = CoreExecutorEffect.model_validate(
                effect.model_dump()
                | {
                    "effect_id": "start-migration",
                    "action": "start_migration",
                    "container_id": created.container_id,
                }
            )
            journal.intent(start)
            if fault == "start_unknown":
                journal.uncertain(start.effect_id)
                with pytest.raises(UpgradeConflict):
                    await docker.start_migration(
                        start,
                        journal,
                        authority,
                        prepared_root=tmp_path / "prepared",
                        dependency_states={},
                    )
                assert len(posts) == 1
                return
            if fault == "lost_start":
                with pytest.raises(UpgradeUnavailable):
                    await docker.start_migration(
                        start,
                        journal,
                        authority,
                        prepared_root=tmp_path / "prepared",
                        dependency_states={},
                    )
            for _ in range(2):
                completed = await docker.start_migration(
                    start,
                    journal,
                    authority,
                    prepared_root=tmp_path / "prepared",
                    dependency_states={},
                )
                assert completed.status == "exited" and completed.exit_code == 0
            assert len(posts) == 2
            assert (
                await docker.inspect_migration(
                    prepared_root=tmp_path / "prepared", dependency_states={}
                )
            ).container_id == created.container_id
