"""Binary-only changes retain the exact last completed migration and its frozen release."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue
from test_docker_migration import prepared_migration_plan

from coire_api.upgrades.host_hold import require_published_target
from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreRuntimeSelection,
    CoreSelectedMigration,
    CoreSelectedService,
    DeploymentOwnership,
    DeploymentSelectionReceipt,
    UpgradeDockerContainer,
    UpgradeDockerMigration,
    UpgradeExecutionPlan,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeReceiptAck,
    UpgradeRelease,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import publish_prepared_release, read_prepared_compose
from coire_upgrade_executor.compose import translate_migration
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership


@pytest.mark.parametrize("fault", [None, "origin", "container", "configuration", "missing_release"])
async def test_historical_migration_stays_bound_after_binary_only_changes(
    tmp_path: Path, fault: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = prepared_migration_plan(tmp_path)
    prepared = tmp_path / "prepared"
    document = read_prepared_compose(prepared, previous.target)
    services = document["services"]
    assert isinstance(services, dict)
    migration = services["coire-migrate"]
    assert isinstance(migration, dict)
    migration["image"] = "sha256:" + "9" * 64
    content = json.dumps(document).encode()
    digest = hashlib.sha256(content).hexdigest()
    manifest = previous.target.manifest.model_copy(
        update={
            "compose_sha256": digest,
            "artifact_sha256s": {"compose.json": digest},
            "host_release_key": "next-target",
            "staged_bytes": len(content),
        }
    )
    target = previous.target.model_copy(
        update={
            "release_id": uuid4(),
            "prepared_artifact_id": "next-target",
            "manifest": manifest,
            "manifest_sha256": manifest.sha256(),
        }
    )
    artifact = tmp_path / "next-compose.json"
    artifact.write_bytes(content)
    publish_prepared_release(prepared, target, {"compose.json": artifact})
    current = UpgradeExecutionPlan.model_validate(
        previous.model_dump()
        | {
            "target": target,
            "initial_schema_head": previous.schema_head_after,
            "maintenance": previous.maintenance.model_dump()
            | {
                "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAZ",
                "generation": 2,
            },
            "operation": previous.operation.model_dump()
            | {
                "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAZ",
                "generation": 2,
                "target_release_id": target.release_id,
                "target_sha256": target.manifest_sha256,
            },
        }
    )
    assert not current.needs_migration
    original = UpgradeDocker(Settings(), previous, prepared_root=prepared)
    try:
        role = original._migration_role(candidate=True)
        name = original.migration_candidate_name
        labels = original._candidate_labels()
    finally:
        await original.close()
    now = datetime.now(UTC)
    selected_migration = CoreSelectedMigration(
        container=UpgradeDockerMigration(
            container_id="d" * 64,
            name=name,
            project="coire",
            image_id=role.image,
            observed_at=now,
            running=False,
            exit_code=0,
            status="exited",
            network_ids={"coire_coire-db": "b" * 64, "coire_coire-telemetry": "c" * 64},
        ),
        release=previous.target,
        configuration=role,
        origin_job_id=previous.operation.job_id,
        origin_generation=previous.operation.generation,
        origin_plan_sha256=previous.authority_sha256(),
    )
    if fault == "configuration":
        selected_migration = selected_migration.model_copy(
            update={
                "configuration": role.model_copy(
                    update={"environment": role.environment | {"POSTGRES_HOST": "foreign"}}
                )
            }
        )
    source_role = current.source.manifest.services[0]
    assert current.source.manifest.compose_sha256 is not None
    assert current.source.manifest.host_release_key is not None
    selection = CoreRuntimeSelection(
        project="coire",
        release_id=current.source.release_id,
        manifest_sha256=current.source.manifest_sha256,
        compose_sha256=current.source.manifest.compose_sha256,
        host_release_key=current.source.manifest.host_release_key,
        owner_identity="executor-prior",
        generation=1,
        selected_at=now,
        services=(
            CoreSelectedService(
                container=UpgradeDockerContainer(
                    container_id="a" * 64,
                    name="coire-coire-api-1",
                    project="coire",
                    service="coire-api",
                    image_id=source_role.image_id,
                    observed_at=now,
                    running=True,
                    health="healthy",
                    exit_code=0,
                    network_ids={"coire_coire-internal": "b" * 64},
                )
            ),
        ),
        migration=selected_migration,
    )
    payload = translate_migration(
        role.model_dump(mode="json"),
        role.model_dump(mode="json"),
        project="coire",
        network_ids={"coire-db": "b" * 64, "coire-telemetry": "c" * 64},
    )
    body: dict[str, Any] = {
        "Id": "e" * 64 if fault == "container" else "d" * 64,
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
            | labels
            | ({"coire.upgrade.generation": "99"} if fault == "origin" else {}),
        },
        "HostConfig": payload["HostConfig"] | {"NetworkMode": "coire_coire-db"},
        "NetworkSettings": {
            "Networks": {
                "coire_coire-db": {"NetworkID": "b" * 64, "Aliases": ["coire-migrate"]},
                "coire_coire-telemetry": {"NetworkID": "c" * 64, "Aliases": ["coire-migrate"]},
            }
        },
        "Mounts": [],
        "State": {"Running": False, "Status": "exited", "ExitCode": 0},
    }
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        calls.append(request.url.path)
        if "/containers/" in request.url.path:
            assert request.url.path == f"/v1.46/containers/{name}/json"
            return httpx.Response(200, json=body)
        assert request.url.path == f"/v1.46/images/{role.image}/json"
        return httpx.Response(
            200,
            json={
                "Id": role.image,
                "Os": "linux",
                "Architecture": "arm64",
                "Config": {"Env": [], "Cmd": ["-m", "coire_api.upgrades.migration_runner"]},
            },
        )

    if fault == "missing_release":
        reader = read_prepared_compose

        def read(root: Path, release: UpgradeRelease) -> dict[str, JsonValue]:
            if release.release_id == previous.target.release_id:
                raise UpgradeUnavailable("Retained release is unavailable")
            return reader(root, release)

        monkeypatch.setattr("coire_upgrade_executor.docker.read_prepared_compose", read)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as http:
        if fault in {"configuration", "missing_release"}:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                UpgradeDocker(
                    Settings(),
                    current,
                    client=http,
                    prepared_root=prepared,
                    source_selection=selection,
                )
            assert not calls
            return
        docker = UpgradeDocker(
            Settings(), current, client=http, prepared_root=prepared, source_selection=selection
        )
        with pytest.raises(UpgradeForbidden):
            await docker._read("GET", "/containers/coire-coire-migrate-1/json")
        assert not calls
        if fault:
            with pytest.raises(UpgradeConflict):
                await docker.inspect_selected_migration(prepared, candidate=False)
        else:
            observed = await docker.inspect_selected_migration(prepared, candidate=False)
            assert observed.release == previous.target
            assert observed.container.container_id == "d" * 64
            assert observed.configuration.image == "sha256:" + "f" * 64
            assert observed.configuration.image != docker._migration_role(candidate=True).image
            journal = ExecutorJournal(tmp_path / "journal", current, prepared_root=prepared)
            owner = DeploymentOwnership(
                project="coire",
                owner_kind="executor",
                owner_identity=selection.owner_identity,
                boot_identity=host_boot_identity(),
                generation=selection.generation,
                job_id=current.operation.job_id,
                operation_generation=current.operation.generation,
                source_sha256=current.operation.source_sha256,
            )
            with executor_ownership(tmp_path / "coordination", owner, current) as authority:
                receipt = journal.host_selection_receipt(selection, authority)
                assert set(receipt.protected_release_keys) == {
                    current.source.manifest.host_release_key,
                    current.target.manifest.host_release_key,
                    previous.target.manifest.host_release_key,
                }
                assert authority.store.read("ownership.json", DeploymentOwnership) == owner
                assert authority.store.read("selection.json", DeploymentSelectionReceipt) is None
                proof_path = journal.store.path(
                    "runtime-selection-" + str(selection.release_id) + ".json"
                )
                retained_bytes = proof_path.read_bytes()
                later = now + timedelta(seconds=6)
                fresh = selection.model_copy(
                    update={
                        "selected_at": later,
                        "services": tuple(
                            item.model_copy(
                                update={
                                    "container": item.container.model_copy(
                                        update={"observed_at": later}
                                    ),
                                }
                            )
                            for item in selection.services
                        ),
                        "migration": selected_migration.model_copy(
                            update={
                                "container": selected_migration.container.model_copy(
                                    update={"observed_at": later}
                                )
                            }
                        ),
                    }
                )
                clock = Mock(wraps=datetime)
                clock.now.return_value = later
                with monkeypatch.context() as delayed:
                    delayed.setattr("coire_upgrade_executor.journal.datetime", clock)
                    assert journal.retain_runtime_selection(fresh, authority) == fresh
                    assert journal.host_selection_receipt(fresh, authority).selected_at == later
                assert proof_path.read_bytes() == retained_bytes
                key = previous.target.manifest.host_release_key
                assert key is not None
                authority.store.write(
                    "selection.json",
                    receipt.model_copy(
                        update={
                            "protected_compose_sha256s": receipt.protected_compose_sha256s
                            | {key: "f" * 64}
                        }
                    ),
                )
                with pytest.raises(UpgradeConflict):
                    journal.host_selection_receipt(selection, authority)
                authority.store.write("selection.json", receipt)
                with pytest.raises(UpgradeConflict, match="terminal"):
                    journal.commit_runtime_selection(selection, current.operation, authority)
                terminal = current.operation.model_copy(
                    update={
                        "phase": UpgradePhase.ROLLED_BACK,
                        "updated_at": datetime.now(UTC),
                        "finished_at": datetime.now(UTC),
                    }
                )
                with pytest.raises(UpgradeConflict, match="selected release"):
                    journal.commit_runtime_selection(
                        selection,
                        terminal.model_copy(update={"phase": UpgradePhase.SUCCEEDED}),
                        authority,
                    )
                write = authority.store.write

                def interrupted_write(key: str, value: Any, *, replace: bool = True) -> None:
                    if key == "selection.json":
                        raise OSError("simulated selection publication interruption")
                    write(key, value, replace=replace)

                with monkeypatch.context() as interrupted:
                    interrupted.setattr(authority.store, "write", interrupted_write)
                    with pytest.raises(OSError):
                        journal.commit_runtime_selection(selection, terminal, authority)
                assert authority.store.read("ownership.json", DeploymentOwnership) == owner
                assert (
                    authority.store.read("runtime-selection.json", CoreRuntimeSelection)
                    == selection
                )
                committed_receipt = journal.commit_runtime_selection(selection, terminal, authority)
                assert authority.claim.state == "committed"
                assert authority.claim.stage == "selected"
                assert authority.claim.committed_sha256 == selection.compose_sha256
                with pytest.raises(UpgradeForbidden, match="Committed"):
                    journal.dispatch_intent("nonexistent-effect", authority)
                assert (
                    authority.store.read("selection.json", DeploymentSelectionReceipt)
                    == committed_receipt
                )
                assert (
                    journal.commit_runtime_selection(selection, terminal, authority)
                    == committed_receipt
                )
            # A Target decision must be durably acknowledged before publishing
            # its matching physical cohort. Terminal success follows publication.
            if fault is None:
                target_journal = ExecutorJournal(
                    tmp_path / "target-journal", current, prepared_root=prepared
                )
                target_owner = owner.model_copy(update={"owner_identity": "executor-target"})
                target_selection = selection.model_copy(
                    update={
                        "release_id": current.target.release_id,
                        "manifest_sha256": current.target.manifest_sha256,
                        "compose_sha256": current.target.manifest.compose_sha256,
                        "host_release_key": current.target.manifest.host_release_key,
                        "owner_identity": target_owner.owner_identity,
                        "services": tuple(
                            item.model_copy(
                                update={
                                    "origin_job_id": current.operation.job_id,
                                    "origin_generation": current.operation.generation,
                                    "origin_plan_sha256": current.authority_sha256(),
                                    "container": item.container.model_copy(
                                        update={
                                            "image_id": current.target.manifest.services[
                                                0
                                            ].image_id,
                                            "name": "coire-upgrade-"
                                            + current.operation.job_id.lower()
                                            + "-coire-api",
                                        }
                                    ),
                                }
                            )
                            for item in selection.services
                        ),
                    }
                )
                decision = UpgradeReceipt(
                    job_id=current.operation.job_id,
                    generation=current.operation.generation,
                    sequence=1,
                    phase=UpgradePhase.COMMITTING,
                    effect_id="target-selection-decision",
                    outcome="complete",
                    occurred_at=datetime.now(UTC),
                    observed_sha256=current.target.manifest_sha256,
                )
                target_journal.receipts.append(decision)
                deciding = current.operation.model_copy(
                    update={
                        "phase": UpgradePhase.COMMITTING,
                        "receipt_sequence": 1,
                        "cancellation_allowed": False,
                    }
                )
                with executor_ownership(
                    tmp_path / "target-coordination", target_owner, current
                ) as authority:
                    with pytest.raises(UpgradeConflict, match="selection decision"):
                        target_journal.commit_runtime_selection(
                            target_selection, deciding, authority
                        )
                    assert (
                        authority.store.read("runtime-selection.json", CoreRuntimeSelection) is None
                    )
                    target_journal.receipts.acknowledge(
                        UpgradeReceiptAck(
                            job_id=current.operation.job_id,
                            generation=current.operation.generation,
                            sequence=1,
                        )
                    )
                    target_journal.commit_runtime_selection(target_selection, deciding, authority)
                    assert authority.claim.state == "committed"
                    assert (
                        authority.store.read("runtime-selection.json", CoreRuntimeSelection)
                        == target_selection
                    )
                    require_published_target(
                        Settings(
                            upgrade_coordination_dir=str(tmp_path / "target-coordination"),
                            upgrade_executor_id=target_owner.owner_identity,
                        ),
                        current,
                    )
