"""Upgrade authority must be immutable, bounded and content-addressed."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from coire_core.models.upgrades import (
    CoreBootstrapHeartbeat,
    UpgradeCompatibility,
    UpgradeKind,
    UpgradeReleaseManifest,
    UpgradeStartRequest,
    UpgradeTiming,
)


@pytest.mark.parametrize(
    "fault",
    [
        "command",
        "weak_writer",
        "source",
        "container",
        "boot",
        "job",
        "committed",
        "mutating",
        "image",
        "executor",
    ],
)
def test_bootstrap_heartbeat_binds_complete_source_and_effect_free_owner(fault: str) -> None:
    from upgrade_bootstrap_fixtures import bootstrap_heartbeat

    value = bootstrap_heartbeat()
    data = value.model_dump(mode="json")
    if fault == "command":
        data["command"] = "/bin/sh"
    elif fault == "weak_writer":
        data["exclusive_writer_verified"] = False
    elif fault == "source":
        data["physical"]["source"]["source_sha256"] = "f" * 64
    elif fault == "container":
        data["ownership"]["executor_container_id"] = "f" * 64
    elif fault == "boot":
        data["lease"]["kernel_boot_identity"] = "foreign-boot"
    elif fault == "job":
        data["ownership"]["job_id"] = "01M6MHS0000000000000000003"
        data["ownership"]["operation_generation"] = 3
    elif fault == "committed":
        data["ownership"]["state"] = "committed"
    elif fault == "mutating":
        data["ownership"]["stage"] = "mutating"
    elif fault == "image":
        data["physical"]["executor_image_id"] = "sha256:" + "f" * 64
    elif fault == "executor":
        data["executor_id"] = "another-owner"
    with pytest.raises(ValidationError):
        CoreBootstrapHeartbeat.model_validate(data)


def test_start_cannot_supply_paths_commands_or_unacknowledged_maintenance() -> None:
    values = {
        "release_id": uuid4(),
        "expected_source_sha256": "a" * 64,
        "idempotency_key": uuid4(),
        "acknowledge_maintenance": True,
    }
    UpgradeStartRequest.model_validate(values)
    for key, value in (
        ("path", "/tmp/release"),
        ("command", "touch /tmp/x"),
        ("model", "untrusted"),
    ):
        with pytest.raises(ValidationError):
            UpgradeStartRequest.model_validate(values | {key: value})
    with pytest.raises(ValidationError):
        UpgradeStartRequest.model_validate(values | {"acknowledge_maintenance": False})


def test_compatibility_requires_explicit_source_and_schema() -> None:
    with pytest.raises(ValidationError):
        UpgradeCompatibility.model_validate({"source_sha256s": [], "readable_schema_heads": []})
    with pytest.raises(ValidationError):
        UpgradeCompatibility.model_validate(
            {"source_sha256s": ["latest"], "readable_schema_heads": ["0034_upgrades"]}
        )


def test_timing_cannot_remove_deadlines() -> None:
    for field in ("drain_s", "readiness_s", "recovery_s", "gateway_outage_s"):
        with pytest.raises(ValidationError):
            UpgradeTiming.model_validate({field: 0})


def test_not_started_fence_cannot_invent_execution_or_weaken_ownership() -> None:
    from coire_core.models import UpgradeMigrationNotStarted

    values = {
        "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "generation": 1,
        "plan_sha256": "a" * 64,
        "grant_id": uuid4(),
        "schema_head": "0034_upgrades",
        "schema_sha256": "b" * 64,
        "observed_at": datetime.now(UTC),
    }
    proof = UpgradeMigrationNotStarted.model_validate(values)
    assert UpgradeMigrationNotStarted.model_validate_json(proof.model_dump_json()) == proof
    for mutation in (
        {"state": "complete"},
        {"execution_fenced": False},
        {"generation": 0},
        {"exit_code": 0},
        {"command": "alembic downgrade base"},
        {"plan_sha256": "latest"},
        {"schema_sha256": "unknown"},
        {"observed_at": datetime(2026, 1, 1)},
    ):
        with pytest.raises(ValidationError):
            UpgradeMigrationNotStarted.model_validate(values | mutation)


def test_initial_core_bootstrap_requires_independent_complete_physical_identities() -> None:
    from upgrade_fixtures import core_plan

    from coire_core.models import CoreBootstrapSeed

    source = core_plan().source
    values = {
        "source": source.model_dump(mode="json"),
        "source_container_ids": {source.manifest.services[0].service: "a" * 64},
        "executor_runtime_id": "initial-executor",
        "executor_container_id": "b" * 64,
        "executor_image_id": "sha256:" + "c" * 64,
        "generation": 1,
        "prepared_at": datetime.now(UTC),
    }
    seed = CoreBootstrapSeed.model_validate(values)
    assert CoreBootstrapSeed.model_validate_json(seed.model_dump_json()) == seed
    for mutation in (
        {"source_container_ids": {}},
        {"executor_container_id": "a" * 64},
        {"executor_image_id": "coire-upgrade-executor:latest"},
        {"generation": 0},
        {"command": "docker exec"},
        {"bootstrap_ready": True},
    ):
        with pytest.raises(ValidationError):
            CoreBootstrapSeed.model_validate(values | mutation)


def test_backup_restore_and_publication_wire_cannot_authorize_arbitrary_restore_commands() -> None:
    from coire_core.models import UpgradeBackupPublication, UpgradeBackupValidation

    now = datetime.now(UTC)
    values = {
        "backup_id": "backup-fixture",
        "project": "coire",
        "schema_head": "0034_upgrades",
        "schema_sha256": "a" * 64,
        "dump_sha256": "b" * 64,
        "size_bytes": 128,
        "restore_container_id": "c" * 64,
        "restore_container_name": "coire-upgrade-backup-" + "d" * 32,
        "postgres_image_id": "sha256:" + "e" * 64,
        "restored_at": now,
    }
    value = UpgradeBackupValidation.model_validate(values)
    assert UpgradeBackupValidation.model_validate_json(value.model_dump_json()) == value
    for mutation in (
        {"command": "pg_restore --clean production"},
        {"path": "/private/dump"},
        {"restore_container_name": "coire-postgres-1"},
        {"restore_exit_code": 1},
        {"schema_verified": False},
        {"backup_id": "../escape"},
        {"size_bytes": 0},
        {"postgres_image_id": "postgres:latest"},
    ):
        with pytest.raises(ValidationError):
            UpgradeBackupValidation.model_validate(values | mutation)
    publication = {
        "evidence": {
            "backup_id": value.backup_id,
            "project": value.project,
            "schema_head": value.schema_head,
            "created_at": now,
            "sha256_digest": value.dump_sha256,
            "size_bytes": value.size_bytes,
            "validated_at": now,
            "validation_sha256": "f" * 64,
        },
        "validation": value.model_dump(),
    }
    UpgradeBackupPublication.model_validate(publication)
    with pytest.raises(ValidationError):
        UpgradeBackupPublication.model_validate(publication | {"destination": "/foreign/volume"})


def test_migration_uses_terminal_exit_without_weakening_service_health() -> None:
    from coire_core.models import UpgradeComposeMigration, UpgradeComposeService

    values = {
        "image": "sha256:" + "a" * 64,
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
    assert UpgradeComposeMigration.model_validate(values).restart == "no"
    with pytest.raises(ValidationError):
        UpgradeComposeService.model_validate(values)
    for mutation in (
        {"restart": "always"},
        {"command": ["sh", "-c", "anything"]},
        {"entrypoint": ["/another-program"]},
        {"ports": [{"target": 8000}]},
        {"expose": ["8000"]},
        {"healthcheck": {"test": ["CMD", "/healthcheck"]}},
        {"privileged": True},
        {"networks": {"coire-edge": {}}},
    ):
        with pytest.raises(ValidationError):
            UpgradeComposeMigration.model_validate(values | mutation)


def test_migration_receipt_requires_timely_zero_exit_and_expected_schema() -> None:
    from datetime import timedelta

    from coire_core.models import UpgradeMigrationExecution

    now = datetime.now(UTC)
    value = {
        "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "generation": 1,
        "plan_sha256": "a" * 64,
        "grant_id": uuid4(),
        "state": "intent",
        "captured_at": now,
        "deadline_at": now + timedelta(seconds=120),
        "schema_before_head": "0034_upgrades",
        "schema_before_sha256": "b" * 64,
        "expected_schema_head": "0035_candidate",
    }
    UpgradeMigrationExecution.model_validate(value)
    complete = value | {
        "state": "complete",
        "schema_after_head": "0035_candidate",
        "schema_after_sha256": "c" * 64,
        "exit_code": 0,
        "completed_at": now + timedelta(seconds=10),
    }
    UpgradeMigrationExecution.model_validate(complete)
    for mutation in (
        {"exit_code": 1},
        {"schema_after_head": "0034_upgrades"},
        {"schema_after_sha256": None},
        {"completed_at": now + timedelta(seconds=121)},
        {"deadline_at": now + timedelta(seconds=121)},
        {"state": "intent"},
    ):
        with pytest.raises(ValidationError):
            UpgradeMigrationExecution.model_validate(complete | mutation)


def test_smoke_selector_uses_registry_slug_bounds_instead_of_artifact_key_bounds() -> None:
    from upgrade_fixtures import plan

    from coire_core.models.upgrades import UpgradeExecutionPlan

    value = plan().model_dump(mode="json")
    registry_slug = "team--" + "long-model-name-" * 11
    assert (
        UpgradeExecutionPlan.model_validate(value | {"model_slug": registry_slug}).model_slug
        == registry_slug
    )
    for invalid in ("arbitrary-model", "../models", "team--" + "x" * 250):
        with pytest.raises(ValidationError):
            UpgradeExecutionPlan.model_validate(value | {"model_slug": invalid})


def test_native_selection_binds_bootstrap_or_exact_operation_epoch() -> None:
    from coire_core.models import NativeRuntimeSelection

    values = {
        "node": "coire-edge-a",
        "release_sha256": "a" * 64,
        "runtime": {
            "environment_id": "known-source",
            "environment_sha256": "b" * 64,
            "interpreter_id": "retained",
            "interpreter_sha256": "c" * 64,
        },
        "generation": 0,
        "selected_at": datetime.now(UTC),
    }
    assert NativeRuntimeSelection.model_validate(values).job_id is None
    operation = values | {"generation": 1, "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}
    assert NativeRuntimeSelection.model_validate(operation).generation == 1
    for changed in ({"generation": 1}, {"job_id": operation["job_id"]}, {"generation": -1}):
        with pytest.raises(ValidationError):
            NativeRuntimeSelection.model_validate(values | changed)


def test_node_activation_requires_matching_consumed_core_intent() -> None:
    from datetime import timedelta

    from upgrade_fixtures import plan

    from coire_core.models.upgrades import (
        NodeUpgradeCommand,
        UpgradeActivationGrant,
        UpgradePhase,
        UpgradeReceipt,
    )

    value = plan()
    now = datetime.now(UTC)
    grant = UpgradeActivationGrant(
        job_id=value.operation.job_id,
        generation=1,
        boundary="runtime",
        grant_id=uuid4(),
        expires_at=now + timedelta(seconds=30),
    )
    receipt = UpgradeReceipt(
        job_id=grant.job_id,
        generation=1,
        sequence=1,
        phase=UpgradePhase.ACTIVATING,
        effect_id="activation",
        outcome="intent",
        occurred_at=now,
        observed_sha256=value.operation.source_sha256,
    )
    fields = value.model_dump(mode="json") | {
        "authorization_expires_at": grant.expires_at,
        "activation_grant": grant,
        "activation_receipt": receipt,
    }
    command = NodeUpgradeCommand.model_validate(fields)
    assert command.authority_sha256() == value.authority_sha256()
    for changed in (
        {"activation_receipt": None},
        {"activation_grant": None},
        {"activation_receipt": receipt.model_copy(update={"phase": UpgradePhase.MIGRATING})},
        {"activation_receipt": receipt.model_copy(update={"observed_sha256": "f" * 64})},
        {"activation_receipt": receipt.model_copy(update={"outcome": "complete"})},
    ):
        with pytest.raises(ValidationError):
            NodeUpgradeCommand.model_validate(fields | changed)


def test_cancellation_capability_does_not_change_accepted_authority() -> None:
    from upgrade_fixtures import core_plan

    value = core_plan()
    deciding = value.operation.model_copy(update={"cancellation_allowed": False})
    assert deciding.authority_sha256() == value.operation.authority_sha256()
    assert (
        value.model_copy(update={"operation": deciding}).authority_sha256()
        == value.authority_sha256()
    )


def test_manual_rollback_uses_the_tested_forward_pair_without_circular_manifest_hashes() -> None:
    from upgrade_fixtures import plan

    from coire_core.models.upgrades import UpgradeExecutionPlan

    value = plan()
    operation = value.operation.model_dump(mode="json") | {
        "action": "rollback",
        "parent_job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAY",
        "source_release_id": value.target.release_id,
        "target_release_id": value.source.release_id,
        "source_sha256": value.target.manifest_sha256,
        "target_sha256": value.source.manifest_sha256,
    }
    payload = value.model_dump(mode="json") | {
        "operation": operation,
        "source": value.target,
        "target": value.source,
    }
    rollback = UpgradeExecutionPlan.model_validate(payload)
    assert rollback.target.manifest_sha256 == value.source.manifest_sha256
    with pytest.raises(ValidationError):
        UpgradeExecutionPlan.model_validate(
            payload | {"operation": operation | {"parent_job_id": None}}
        )


def test_manifest_digest_changes_for_same_version_artifact_change() -> None:
    common = {
        "kind": UpgradeKind.STUDIO,
        "version": "0.2.0",
        "source_revision": "a" * 40,
        "platform": "macos-arm64",
        "prepared_at": datetime.now(UTC),
        "compatibility": UpgradeCompatibility(
            source_sha256s=("b" * 64,), readable_schema_heads=("0034_upgrades",)
        ),
        "environment_id": "0.2.0-aaaaaaaaaaaa",
        "environment_sha256": "a" * 64,
        "artifact_sha256s": {"node-wheel": "a" * 64},
        "staged_bytes": 1024,
    }
    first = UpgradeReleaseManifest.model_validate(common)
    second = UpgradeReleaseManifest.model_validate(
        common | {"artifact_sha256s": {"node-wheel": "c" * 64}}
    )
    assert first.sha256() != second.sha256()
    with pytest.raises(ValidationError):
        first.version = "changed"


def test_every_upgrade_wire_shape_rejects_unknown_fields() -> None:
    import inspect

    from coire_core.models import upgrades

    models = [
        value
        for _, value in inspect.getmembers(upgrades, inspect.isclass)
        if issubclass(value, upgrades.UpgradeWire)
    ]
    assert len(models) >= 35
    for model in models:
        assert model.model_config["extra"] == "forbid", model.__name__
        assert model.model_config["frozen"], model.__name__
        assert model.model_json_schema()["additionalProperties"] is False, model.__name__


def test_inventory_cannot_claim_another_owners_work() -> None:
    from coire_core.models.upgrades import UpgradeInventoryEntry, UpgradeOwnerInventory

    entry = UpgradeInventoryEntry(owner="training", kind="attempt", work_id="one", state="active")
    with pytest.raises(ValidationError):
        UpgradeOwnerInventory(owner="images", status="complete", entries=(entry,))
    with pytest.raises(ValidationError):
        UpgradeOwnerInventory(owner="training", status="complete", entries=(entry, entry))


def test_receipt_evidence_rejects_raw_logs_secrets_and_input() -> None:
    from coire_core.models.upgrades import UpgradeEvidence, UpgradeReceipt

    fields = {
        "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "generation": 1,
        "sequence": 1,
        "effect_id": "source-check",
        "phase": "verifying_source",
        "occurred_at": datetime.now(UTC),
        "outcome": "complete",
    }
    safe = UpgradeEvidence(runtime_sha256="a" * 64, source_verified=True)
    assert UpgradeReceipt.model_validate(fields | {"evidence": safe.model_dump_json()}).evidence
    for evidence in ('{"environment":"secret"}', '{"prompt":"private"}', "engine stack trace"):
        with pytest.raises(ValidationError):
            UpgradeReceipt.model_validate(fields | {"evidence": evidence})
    with pytest.raises(ValidationError):
        UpgradeReceipt.model_validate(fields | {"reason": "failed at /private/secret"})


def test_persistence_numbers_and_process_identity_are_bounded() -> None:
    from coire_core.models.upgrades import NodeAgentProcessIdentity, UpgradeReceiptAck

    with pytest.raises(ValidationError):
        UpgradeReceiptAck(job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV", generation=2**63, sequence=0)
    with pytest.raises(ValidationError):
        NodeAgentProcessIdentity(pid=123, create_time_s=float("inf"), environment_sha256="a" * 64)


def test_qualification_requires_exact_runtime_and_peer_identity() -> None:
    from coire_core.models.upgrades import UpgradeQualification

    with pytest.raises(ValidationError):
        UpgradeQualification(capability="paired", state="qualified", runtime_sha256="a" * 64)
    value = UpgradeQualification(
        capability="paired",
        state="qualified",
        runtime_sha256="a" * 64,
        peer_runtime_sha256="b" * 64,
    )
    assert value.peer_runtime_sha256 == "b" * 64
    with pytest.raises(ValidationError):
        UpgradeQualification(capability="serving", state="qualified")


def test_target_readiness_cannot_claim_missing_bootstrap_or_wrong_identity() -> None:
    from coire_core.models.upgrades import UpgradeTargetStatus

    fields = {
        "kind": "studio",
        "node": "coire-edge-a",
        "readiness": "ready",
        "observed_at": datetime.now(UTC),
        "release_id": uuid4(),
        "desired_sha256": "a" * 64,
        "observed_sha256": "a" * 64,
    }
    with pytest.raises(ValidationError):
        UpgradeTargetStatus.model_validate(fields)
    assert (
        UpgradeTargetStatus.model_validate(fields | {"bootstrap_ready": True}).readiness == "ready"
    )
    with pytest.raises(ValidationError):
        UpgradeTargetStatus.model_validate(
            fields | {"bootstrap_ready": True, "observed_sha256": "b" * 64}
        )
    with pytest.raises(ValidationError):
        UpgradeTargetStatus.model_validate(fields | {"bootstrap_ready": True, "node": None})


def test_every_wire_model_rejects_unknown_fields_at_validation() -> None:
    import inspect

    from coire_core.models import upgrades

    for name, model in inspect.getmembers(upgrades, inspect.isclass):
        if not issubclass(model, upgrades.UpgradeWire):
            continue
        with pytest.raises(ValidationError) as caught:
            model.model_validate({"caller_shell_command": "forbidden"})
        assert any(
            error["type"] == "extra_forbidden" and error["loc"] == ("caller_shell_command",)
            for error in caught.value.errors()
        ), name


def test_receipt_replay_and_event_cursors_round_trip_without_identity_coercion() -> None:
    from upgrade_fixtures import plan

    from coire_core.models.upgrades import UpgradeEventPage, UpgradeReceiptAck

    receipt = UpgradeReceiptAck(job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV", generation=7, sequence=32)
    assert UpgradeReceiptAck.model_validate_json(receipt.model_dump_json()) == receipt
    for fields in ({"job_id": str(uuid4())}, {"generation": 0}, {"sequence": -1}):
        with pytest.raises(ValidationError):
            UpgradeReceiptAck.model_validate(receipt.model_dump(mode="json") | fields)
    page = UpgradeEventPage(operation=plan().operation, events=(), cursor=0, reset=True)
    assert UpgradeEventPage.model_validate_json(page.model_dump_json()) == page
    with pytest.raises(ValidationError):
        UpgradeEventPage.model_validate(page.model_dump(mode="json") | {"cursor": 2**63})


def test_compatibility_and_inventory_collection_bounds_are_enforced() -> None:
    from coire_core.models.upgrades import UpgradeInventoryEntry

    with pytest.raises(ValidationError):
        UpgradeCompatibility(
            source_sha256s=tuple(f"{number:064x}" for number in range(33)),
            readable_schema_heads=("0034_upgrades",),
        )
    with pytest.raises(ValidationError):
        UpgradeCompatibility(
            source_sha256s=("a" * 64, "a" * 64), readable_schema_heads=("0034_upgrades",)
        )
    with pytest.raises(ValidationError):
        UpgradeInventoryEntry(
            owner="training",
            kind="attempt",
            work_id="one",
            state="active",
            holds=tuple(f"hold-{number}" for number in range(257)),
        )


@pytest.mark.parametrize("identity", ["owner with spaces", "../owner", "x" * 129])
def test_deployment_owner_identity_matches_shared_claim_bounds(identity: str) -> None:
    from coire_core.models.upgrades import DeploymentOwnership

    with pytest.raises(ValidationError):
        DeploymentOwnership(
            project="coire",
            owner_kind="host",
            owner_identity=identity,
            boot_identity="boot-one",
            generation=1,
        )


def test_activation_peer_snapshot_is_bound_without_changing_plan_authority() -> None:
    from datetime import timedelta

    from upgrade_fixtures import plan

    from coire_core.models.upgrades import (
        NodeUpgradeCommand,
        NodeUpgradeInventory,
        UpgradeActivationGrant,
        UpgradeExecutionPlan,
        UpgradePhase,
        UpgradeReceipt,
    )

    original = plan()
    manifest = original.target.manifest.model_copy(update={"paired_required": True})
    value = UpgradeExecutionPlan.model_validate(
        original.model_dump(mode="json")
        | {
            "target": original.target.model_copy(
                update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
            ),
            "operation": original.operation.model_copy(update={"target_sha256": manifest.sha256()}),
            "maintenance": original.maintenance.model_copy(
                update={"targets": ("coire-edge-a", "coire-edge-b")}
            ),
            "peer_runtime_sha256": "c" * 64,
        }
    )
    now = datetime.now(UTC)
    grant = UpgradeActivationGrant(
        job_id=value.operation.job_id,
        generation=1,
        boundary="runtime",
        grant_id=uuid4(),
        expires_at=now + timedelta(seconds=30),
    )
    receipt = UpgradeReceipt(
        job_id=grant.job_id,
        generation=1,
        sequence=1,
        phase=UpgradePhase.ACTIVATING,
        effect_id="activation",
        outcome="intent",
        occurred_at=now,
        observed_sha256=value.operation.source_sha256,
    )
    peer = NodeUpgradeInventory(
        job_id=grant.job_id,
        generation=1,
        node="coire-edge-b",
        runtime_sha256="c" * 64,
        observed_at=now,
        owners=(),
    )
    fields = value.model_dump(mode="json") | {
        "authorization_expires_at": grant.expires_at,
        "activation_grant": grant,
        "activation_receipt": receipt,
        "peer_inventory": peer,
    }
    accepted = NodeUpgradeCommand.model_validate(fields)
    assert accepted.authority_sha256() == value.authority_sha256()
    for invalid in (
        None,
        peer.model_copy(update={"node": "coire-edge-a"}),
        peer.model_copy(update={"generation": 2}),
        peer.model_copy(update={"runtime_sha256": "d" * 64}),
    ):
        with pytest.raises(ValidationError):
            NodeUpgradeCommand.model_validate(fields | {"peer_inventory": invalid})
    with pytest.raises(ValidationError):
        NodeUpgradeCommand.model_validate(value.model_dump(mode="json") | {"peer_inventory": peer})


def test_owned_smoke_engine_journal_cannot_describe_adapters_or_unbounded_processes() -> None:
    from coire_core.models import NativeSmokeEngineRecord

    values = {
        "engine_id": uuid4(),
        "slug": "owner--tiny",
        "port": 9500,
        "pid": 123456,
        "create_time": 123.0,
        "estimate_bytes": 1024,
        "started_at": datetime.now(UTC),
    }
    NativeSmokeEngineRecord.model_validate(values)
    for patch in (
        {"target": {"adapter_id": str(uuid4())}},
        {"backend": "mlx_vlm"},
        {"pid": 0},
        {"create_time": None},
        {"create_time": float("nan")},
        {"estimate_bytes": 3 * 1024**3},
        {"chat_template_sha256": "a" * 64},
        {"stderr_file": "../foreign.log"},
        {"command": "arbitrary"},
    ):
        with pytest.raises(ValidationError):
            NativeSmokeEngineRecord.model_validate(values | patch)


def test_unchanged_release_is_scoped_to_a_verified_terminal_source_runtime() -> None:
    from upgrade_fixtures import plan

    from coire_core.models.upgrades import NodeMaintenanceCommand

    values = {
        "maintenance": plan().maintenance.model_dump(mode="json"),
        "action": "release",
        "terminal_verified": True,
        "source_unchanged": True,
        "source_runtime_sha256": "a" * 64,
    }
    NodeMaintenanceCommand.model_validate(values)
    for patch in (
        {"action": "apply"},
        {"terminal_verified": False},
        {"source_runtime_sha256": None},
        {"source_runtime_sha256": "unknown"},
        {"source_unchanged": False},
    ):
        with pytest.raises(ValidationError):
            NodeMaintenanceCommand.model_validate(values | patch)
