"""Migration orchestration needs matching terminal container and schema evidence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_docker_migration import prepared_migration_plan
from test_executor_journal import activation

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreExecutorEffect,
    DeploymentOwnership,
    UpgradeActivationConsume,
    UpgradeDockerMigration,
    UpgradeMigrationExecution,
    UpgradeMigrationNotStarted,
    UpgradePhase,
    UpgradeSchemaObservation,
)
from coire_core.settings import Settings
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.executor import CoreExecutor
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership


@pytest.mark.parametrize("fault", [None, "dispatch", "present", "grant", "schema", "expired"])
async def test_consumed_unstarted_migration_recovers_only_with_positive_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    plan = prepared_migration_plan(tmp_path)
    assert plan.initial_schema_head is not None and plan.initial_schema_sha256 is not None
    journal = ExecutorJournal(tmp_path / "journal", plan, prepared_root=tmp_path / "prepared")
    now = datetime.now(UTC)
    value = activation(journal, now)
    consumed = UpgradeActivationConsume.model_validate(
        value.model_dump()
        | {
            "grant": value.grant.model_dump() | {"boundary": "migration"},
            "intent": value.intent.model_dump() | {"phase": UpgradePhase.MIGRATING},
        }
    )
    journal.activation(consumed, now=now)
    settings = Settings(upgrade_executor_token=SecretStr("coire_upgrade_test"))
    docker = UpgradeDocker(settings, plan, prepared_root=tmp_path / "prepared")
    client = ExecutorClient(settings)
    fence = UpgradeMigrationNotStarted(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        plan_sha256=plan.authority_sha256(),
        grant_id=uuid4() if fault == "grant" else consumed.grant.grant_id,
        schema_head=plan.initial_schema_head,
        schema_sha256=plan.initial_schema_sha256,
        observed_at=now,
    )
    observation = UpgradeSchemaObservation(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        schema_head=plan.initial_schema_head,
        schema_sha256="f" * 64 if fault == "schema" else plan.initial_schema_sha256,
        observed_at=now,
    )
    fenced, absent = AsyncMock(return_value=fence), AsyncMock()
    if fault == "present":
        absent.side_effect = UpgradeConflict("candidate is physically present")
    monkeypatch.setattr(client, "schema", AsyncMock(return_value=observation))
    monkeypatch.setattr(client, "fence_migration_not_started", fenced)
    monkeypatch.setattr(docker, "verify_migration_candidate_absent", absent)
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.operation.source_sha256,
    )
    try:
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
            executor = CoreExecutor(journal, docker, client, authority, tmp_path / "prepared")
            if fault == "dispatch":
                executor.migration_effect("migration")
            journal.capture_recovery(
                now=now - timedelta(seconds=301) if fault == "expired" else now
            )
            if fault:
                with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                    await executor.verify_schema(restored=True)
                if fault in {"dispatch", "expired"}:
                    fenced.assert_not_awaited()
                    absent.assert_not_awaited()
            else:
                assert await executor.verify_schema(restored=True) == observation
                assert (
                    journal.store.read("migration-not-started.json", UpgradeMigrationNotStarted)
                    == fence
                )
                assert journal.store.read("migration-proof.json", UpgradeMigrationExecution) is None
                fenced.assert_awaited_once()
                absent.assert_awaited_once()
    finally:
        await client.close()
        await docker.close()


@pytest.mark.parametrize("fault", [None, "exit", "schema", "failed_unchanged", "partial"])
async def test_migration_requires_both_proofs_and_replays_without_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    plan = prepared_migration_plan(tmp_path)
    assert plan.initial_schema_head is not None
    assert plan.initial_schema_sha256 is not None
    assert plan.schema_head_after is not None
    now = datetime.now(UTC)
    journal = ExecutorJournal(tmp_path / "journal", plan, prepared_root=tmp_path / "prepared")
    value = activation(journal, now)
    consumed = UpgradeActivationConsume.model_validate(
        value.model_dump()
        | {
            "grant": value.grant.model_dump() | {"boundary": "migration"},
            "intent": value.intent.model_dump() | {"phase": UpgradePhase.MIGRATING},
        }
    )
    journal.activation(consumed, now=now)
    settings = Settings(upgrade_executor_token=SecretStr("coire_upgrade_test"))
    docker = UpgradeDocker(settings, plan, prepared_root=tmp_path / "prepared")
    client = ExecutorClient(settings)
    observed = UpgradeDockerMigration(
        container_id="c" * 64,
        name=docker.migration_candidate_name,
        project="coire",
        image_id=docker._migration_role(candidate=True).image,
        observed_at=now,
        running=False,
        exit_code=1 if fault in {"exit", "failed_unchanged", "partial"} else 0,
        status="exited",
        network_ids={"coire-db": "b" * 64},
    )
    proof = UpgradeMigrationExecution(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        plan_sha256=plan.authority_sha256(),
        grant_id=consumed.grant.grant_id,
        state="complete",
        captured_at=now,
        deadline_at=now + timedelta(seconds=120),
        schema_before_head=plan.initial_schema_head,
        schema_before_sha256=plan.initial_schema_sha256,
        expected_schema_head=plan.schema_head_after,
        schema_after_head=plan.schema_head_after,
        schema_after_sha256="d" * 64,
        exit_code=0,
        completed_at=now,
    )
    if fault == "schema":
        proof = proof.model_copy(update={"plan_sha256": "f" * 64})
    elif fault in {"failed_unchanged", "partial"}:
        proof = UpgradeMigrationExecution.model_validate(
            proof.model_dump()
            | {
                "state": "uncertain",
                "exit_code": 1,
                "schema_after_head": plan.initial_schema_head
                if fault == "failed_unchanged"
                else plan.schema_head_after,
                "schema_after_sha256": plan.initial_schema_sha256
                if fault == "failed_unchanged"
                else "f" * 64,
            }
        )
    create, start, inspect, schema = (
        AsyncMock(return_value=item) for item in (observed, observed, observed, proof)
    )
    monkeypatch.setattr(docker, "create_migration", create)
    monkeypatch.setattr(docker, "start_migration", start)
    monkeypatch.setattr(docker, "inspect_migration", inspect)
    monkeypatch.setattr(client, "migration", schema)
    owner = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.operation.source_sha256,
    )
    try:
        with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
            executor = CoreExecutor(journal, docker, client, authority, tmp_path / "prepared")
            if fault is not None:
                with pytest.raises(UpgradeConflict):
                    await executor.migrate(dependency_states={})
                if fault == "failed_unchanged":
                    assert (
                        journal.store.read("migration-proof.json", UpgradeMigrationExecution)
                        == proof
                    )
                    with pytest.raises(UpgradeConflict):
                        await executor.migrate(dependency_states={})
                    assert create.await_count == start.await_count == schema.await_count == 1
                    snapshot = UpgradeSchemaObservation(
                        job_id=plan.operation.job_id,
                        generation=plan.operation.generation,
                        schema_head=plan.initial_schema_head,
                        schema_sha256=plan.initial_schema_sha256,
                        observed_at=datetime.now(UTC),
                    )
                    monkeypatch.setattr(client, "schema", AsyncMock(return_value=snapshot))
                    assert await executor.verify_schema(restored=True) == snapshot
                    expired = snapshot.model_copy(
                        update={"observed_at": datetime.now(UTC) - timedelta(seconds=6)}
                    )
                    monkeypatch.setattr(client, "schema", AsyncMock(return_value=expired))
                    with pytest.raises(UpgradeConflict, match="expired"):
                        await executor.verify_schema(restored=True)
                    monkeypatch.setattr(client, "schema", AsyncMock(return_value=snapshot))
                    with pytest.raises(UpgradeUnavailable):
                        await executor.verify_schema(restored=False)
                    assert create.await_count == start.await_count == 1
                    journal.capture_recovery()
                    recovery = journal.state()
                    assert recovery.recovery_deadline is not None
                    removal = journal.intent(
                        CoreExecutorEffect(
                            job_id=plan.operation.job_id,
                            generation=plan.operation.generation,
                            plan_sha256=plan.authority_sha256(),
                            effect_id="core-remove-migration",
                            action="remove_migration",
                            container_id=observed.container_id,
                            image_id=observed.image_id,
                            captured_at=datetime.now(UTC),
                            deadline=recovery.recovery_deadline,
                        )
                    )
                    exists = True
                    deletes: list[httpx.Request] = []

                    def remove_transport(request: httpx.Request) -> httpx.Response:
                        nonlocal exists
                        if request.method == "DELETE":
                            assert request.url.path == f"/v1.46/containers/{observed.container_id}"
                            assert dict(request.url.params) == {"force": "false", "v": "false"}
                            retained = journal.effects.read(
                                "core-remove-migration.json", CoreExecutorEffect
                            )
                            assert retained is not None and retained.outcome == "unknown"
                            deletes.append(request)
                            raise httpx.ReadError("lost removal reply", request=request)
                        assert request.method == "GET"
                        assert request.url.path in {
                            f"/v1.46/containers/{observed.container_id}/json",
                            f"/v1.46/containers/{docker.migration_candidate_name}/json",
                        }
                        return (
                            httpx.Response(200, json={"present": True})
                            if exists
                            else httpx.Response(404)
                        )

                    async with httpx.AsyncClient(
                        transport=httpx.MockTransport(remove_transport), trust_env=False
                    ) as removal_client:
                        monkeypatch.setattr(docker, "_client", removal_client)
                        with pytest.raises(UpgradeUnavailable):
                            await docker.remove_failed_migration(
                                removal, journal, authority, prepared_root=tmp_path / "prepared"
                            )
                        uncertain = journal.effects.read(
                            "core-remove-migration.json", CoreExecutorEffect
                        )
                        assert uncertain is not None and uncertain.outcome == "unknown"
                        with pytest.raises(UpgradeConflict, match="blind deletion retry"):
                            await docker.remove_failed_migration(
                                uncertain, journal, authority, prepared_root=tmp_path / "prepared"
                            )
                        assert len(deletes) == 1
                        exists = False
                        removed = await docker.remove_failed_migration(
                            uncertain, journal, authority, prepared_root=tmp_path / "prepared"
                        )
                        assert removed.outcome == "complete" and len(deletes) == 1
                        assert await executor.verify_schema(restored=True) == snapshot
                        assert len(deletes) == 1
                else:
                    assert not journal.store.path("migration-proof.json").exists()
            else:
                assert await executor.migrate(dependency_states={}) == proof
                assert await executor.migrate(dependency_states={}) == proof
                assert create.await_count == start.await_count == schema.await_count == 1
                assert inspect.await_count == 2
                journal.capture_recovery()
                deadline = journal.state().recovery_deadline
                assert deadline is not None
                removal = journal.intent(
                    CoreExecutorEffect(
                        job_id=plan.operation.job_id,
                        generation=plan.operation.generation,
                        plan_sha256=plan.authority_sha256(),
                        effect_id="core-remove-migration",
                        action="remove_migration",
                        container_id=observed.container_id,
                        image_id=observed.image_id,
                        captured_at=datetime.now(UTC),
                        deadline=deadline,
                    )
                )
                with pytest.raises(UpgradeForbidden):
                    await docker.remove_failed_migration(
                        removal, journal, authority, prepared_root=tmp_path / "prepared"
                    )
                assert inspect.await_count == 2
    finally:
        await client.close()
        await docker.close()
