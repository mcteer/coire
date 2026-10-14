"""Core migration and runtime grants require different, exact durable evidence."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from upgrade_fixtures import core_plan, plan

from coire_api.upgrades.grants import verify_activation_ready
from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    UpgradeEvidence,
    UpgradeMigrationExecution,
    UpgradePhase,
    UpgradeReceipt,
)


async def test_studio_pre_switch_smoke_must_execute_the_candidate_runtime() -> None:
    value = plan()
    operation = value.operation.model_copy(update={"phase": UpgradePhase.SMOKE_BEFORE})
    evidence = UpgradeEvidence(
        check="node",
        runtime_sha256=value.target.manifest.environment_sha256,
        model_id=value.model_id,
        model_variant_id=value.model_variant_id,
        model_manifest_sha256=value.model_manifest_sha256,
        output_sha256="e" * 64,
        generated_tokens=10,
        cleanup_verified=True,
    )
    receipt = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=1,
        phase=UpgradePhase.SMOKE_BEFORE,
        effect_id="candidate-smoke",
        occurred_at=datetime.now(UTC),
        outcome="complete",
        observed_sha256=operation.source_sha256,
        evidence=evidence.model_dump_json(),
    )
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = SimpleNamespace(payload=receipt.model_dump(mode="json"))
    await verify_activation_ready(session, operation, "runtime", value)
    for cleanup in (None, False):
        session.scalar.return_value.payload = receipt.model_copy(
            update={
                "evidence": evidence.model_copy(
                    update={"cleanup_verified": cleanup}
                ).model_dump_json(),
            }
        ).model_dump(mode="json")
        with pytest.raises(UpgradeConflict):
            await verify_activation_ready(session, operation, "runtime", value)
    source_smoke = evidence.model_copy(
        update={"runtime_sha256": value.source.manifest.environment_sha256}
    )
    session.scalar.return_value.payload = receipt.model_copy(
        update={"evidence": source_smoke.model_dump_json()}
    ).model_dump(mode="json")
    with pytest.raises(UpgradeConflict):
        await verify_activation_ready(session, operation, "runtime", value)


@pytest.mark.parametrize("schema_changed", (False, True))
async def test_core_staging_grant_binds_artifacts_configuration_and_backup(
    schema_changed: bool,
) -> None:
    value = core_plan(schema_changed=schema_changed)
    operation = value.operation.model_copy(update={"phase": UpgradePhase.STAGED})
    evidence = UpgradeEvidence(
        artifacts_verified=True,
        configuration_verified=True,
        release_sha256=value.target.manifest_sha256,
        service_image_ids={item.service: item.image_id for item in value.target.manifest.services},
        backup_validation_sha256=value.backup.validation_sha256 if value.backup else None,
    )
    receipt = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=1,
        phase=UpgradePhase.STAGED,
        effect_id="candidate-verified",
        occurred_at=datetime.now(UTC),
        outcome="complete",
        observed_sha256=operation.source_sha256,
        evidence=evidence.model_dump_json(),
    )
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = SimpleNamespace(payload=receipt.model_dump(mode="json"))
    boundary = "migration" if schema_changed else "runtime"
    await verify_activation_ready(session, operation, boundary, value)
    with pytest.raises(UpgradeConflict):
        await verify_activation_ready(
            session, operation, "runtime" if schema_changed else "migration", value
        )
    for changed in (
        evidence.model_copy(update={"configuration_verified": False}),
        evidence.model_copy(update={"artifacts_verified": False}),
        evidence.model_copy(update={"release_sha256": operation.source_sha256}),
        evidence.model_copy(update={"service_image_ids": {}}),
    ):
        session.scalar.return_value.payload = receipt.model_copy(
            update={"evidence": changed.model_dump_json()}
        ).model_dump(mode="json")
        with pytest.raises(UpgradeConflict):
            await verify_activation_ready(session, operation, boundary, value)


async def test_core_runtime_requires_zero_migration_exit_and_expected_schema() -> None:
    value = core_plan(schema_changed=True)
    operation = value.operation.model_copy(update={"phase": UpgradePhase.MIGRATING})
    evidence = UpgradeEvidence(schema_head=value.target.manifest.schema_head, migration_exit_code=0)
    receipt = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=1,
        phase=UpgradePhase.MIGRATING,
        effect_id="migration",
        occurred_at=datetime.now(UTC),
        outcome="complete",
        observed_sha256=operation.source_sha256,
        evidence=evidence.model_dump_json(),
    )
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = SimpleNamespace(payload=receipt.model_dump(mode="json"))
    await verify_activation_ready(session, operation, "runtime", value)
    expired = value.model_copy(
        update={
            "maintenance": value.maintenance.model_copy(
                update={"drain_deadline_at": datetime.now(UTC) - timedelta(seconds=1)}
            )
        }
    )
    # Runtime continuation after a completed migration preserves logical activation.
    await verify_activation_ready(session, operation, "runtime", expired)
    for changed in (
        evidence.model_copy(update={"migration_exit_code": None}),
        evidence.model_copy(update={"migration_exit_code": 1}),
        evidence.model_copy(update={"schema_head": value.source.manifest.schema_head}),
    ):
        session.scalar.return_value.payload = receipt.model_copy(
            update={"evidence": changed.model_dump_json()}
        ).model_dump(mode="json")
        with pytest.raises(UpgradeConflict):
            await verify_activation_ready(session, operation, "runtime", value)


async def test_core_grant_rechecks_actual_schema_at_each_activation_boundary() -> None:
    from unittest.mock import patch

    from coire_api.upgrades.grants import verify_current_schema

    value = core_plan(schema_changed=True)
    session = AsyncMock(spec=AsyncSession)
    staged = value.operation.model_copy(update={"phase": UpgradePhase.STAGED})
    with patch("coire_api.upgrades.grants.inspect_schema", new_callable=AsyncMock) as inspect:
        inspect.return_value = (value.initial_schema_head, value.initial_schema_sha256)
        await verify_current_schema(session, staged, value)
        inspect.return_value = (value.initial_schema_head, "f" * 64)
        with pytest.raises(UpgradeConflict):
            await verify_current_schema(session, staged, value)
        migrated = staged.model_copy(update={"phase": UpgradePhase.MIGRATING})
        now = datetime.now(UTC)
        proof = UpgradeMigrationExecution(
            job_id=migrated.job_id,
            generation=migrated.generation,
            plan_sha256=value.authority_sha256(),
            grant_id=uuid4(),
            state="complete",
            captured_at=now,
            deadline_at=now + timedelta(seconds=120),
            schema_before_head=value.initial_schema_head,
            schema_before_sha256=value.initial_schema_sha256,
            expected_schema_head=value.schema_head_after,
            exit_code=0,
            schema_after_head=value.schema_head_after,
            schema_after_sha256="f" * 64,
            completed_at=now,
        )
        session.get.return_value = SimpleNamespace(
            migration_execution=proof.model_dump(mode="json")
        )
        inspect.return_value = (value.schema_head_after, "f" * 64)
        await verify_current_schema(session, migrated, value)
        inspect.return_value = (value.schema_head_after, "e" * 64)
        with pytest.raises(UpgradeConflict):
            await verify_current_schema(session, migrated, value)
        inspect.return_value = (value.initial_schema_head, value.initial_schema_sha256)
        with pytest.raises(UpgradeConflict):
            await verify_current_schema(session, migrated, value)


@pytest.mark.parametrize("kind", ["studio", "core"])
async def test_first_activation_rejects_elapsed_captured_drain(kind: str) -> None:
    value = plan() if kind == "studio" else core_plan(schema_changed=True)
    value = value.model_copy(
        update={
            "maintenance": value.maintenance.model_copy(
                update={"drain_deadline_at": datetime.now(UTC) - timedelta(seconds=1)}
            )
        }
    )
    operation = value.operation.model_copy(
        update={"phase": UpgradePhase.SMOKE_BEFORE if kind == "studio" else UpgradePhase.STAGED}
    )
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(UpgradeConflict, match="drain deadline elapsed"):
        await verify_activation_ready(
            session, operation, "runtime" if kind == "studio" else "migration", value
        )
    session.scalar.assert_not_awaited()


async def test_migration_boundary_requires_fresh_protected_backup_bytes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from coire_api.upgrades import grants
    from coire_core.errors import UpgradeUnavailable
    from coire_core.settings import Settings

    value = core_plan(schema_changed=True)
    settings = Settings(upgrade_prepared_dir=str(tmp_path))
    verifier = Mock(side_effect=UpgradeUnavailable("backup dump missing"))
    monkeypatch.setattr(grants, "verify_prepared_backup", verifier)
    # Runtime continuation and unchanged-schema changes do not reauthorize DDL.
    await grants.verify_backup_boundary(value, "runtime", settings)
    verifier.assert_not_called()
    with pytest.raises(UpgradeUnavailable, match="backup dump missing"):
        await grants.verify_backup_boundary(value, "migration", settings)
    verifier.assert_called_once_with(
        tmp_path / "backups",
        value.backup,
        project="coire",
        schema_head=value.initial_schema_head,
        schema_sha256=value.initial_schema_sha256,
    )
