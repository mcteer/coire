"""Local effects stay immutable, ordered and recoverable across lost acknowledgments."""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from coire_core.models.upgrades import (
    BackupEvidence,
    UpgradeAction,
    UpgradeCompatibility,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMaintenance,
    UpgradeOperation,
    UpgradeRelease,
    UpgradeReleaseManifest,
    UpgradeReleaseRegister,
    UpgradeServiceSpec,
)
from coire_core.upgrade_journal import PrivateUpgradeStore


def plan(*, paired: bool = False) -> UpgradeExecutionPlan:
    now, owner = datetime.now(UTC), uuid4()
    source_manifest = UpgradeReleaseManifest(
        kind=UpgradeKind.STUDIO,
        version="source",
        source_revision="a" * 40,
        platform="macos-arm64",
        prepared_at=now,
        compatibility=UpgradeCompatibility(
            source_sha256s=("f" * 64,), readable_schema_heads=("0034_upgrades",)
        ),
        artifact_sha256s={"wheel": "a" * 64},
        staged_bytes=1,
        environment_id="source",
        environment_sha256="a" * 64,
        paired_required=paired,
    )

    target_manifest = source_manifest.model_copy(
        update={
            "version": "target",
            "environment_id": "target",
            "environment_sha256": "b" * 64,
            "compatibility": UpgradeCompatibility(
                source_sha256s=(source_manifest.sha256(),), readable_schema_heads=("0034_upgrades",)
            ),
        }
    )
    source = UpgradeRelease(
        release_id=uuid4(),
        manifest_sha256=source_manifest.sha256(),
        manifest=source_manifest,
        prepared_artifact_id="source",
        prepared_by=owner,
    )
    target = UpgradeRelease(
        release_id=uuid4(),
        manifest_sha256=target_manifest.sha256(),
        manifest=target_manifest,
        prepared_artifact_id="target",
        prepared_by=owner,
    )
    op = UpgradeOperation(
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        action=UpgradeAction.UPGRADE,
        kind=UpgradeKind.STUDIO,
        node="coire-edge-a",
        source_release_id=source.release_id,
        target_release_id=target.release_id,
        source_sha256=source.manifest_sha256,
        target_sha256=target.manifest_sha256,
        user_id=owner,
        idempotency_key=uuid4(),
        request_sha256="c" * 64,
        generation=1,
        accepted_at=now,
        updated_at=now,
    )
    return UpgradeExecutionPlan(
        operation=op,
        source=source,
        target=target,
        maintenance=UpgradeMaintenance(
            job_id=op.job_id,
            generation=1,
            targets=("coire-edge-a", "coire-edge-b") if paired else ("coire-edge-a",),
            state="draining",
            updated_at=now,
            drain_deadline_at=now + timedelta(seconds=900),
        ),
        authorization_expires_at=now + timedelta(seconds=30),
        model_id=uuid4(),
        model_variant_id=uuid4(),
        model_slug="owner--tiny",
        model_manifest_sha256="d" * 64,
        peer_runtime_sha256="c" * 64 if paired else None,
    )


def core_plan(*, schema_changed: bool = False) -> UpgradeExecutionPlan:
    import hashlib

    value = plan()
    source_manifest = UpgradeReleaseManifest.model_validate(
        value.source.manifest.model_dump(mode="json")
        | {
            "kind": "control_plane",
            "platform": "linux-arm64",
            "project": "coire",
            "environment_id": None,
            "environment_sha256": None,
            "schema_head": "0034_upgrades",
            "compatibility": {
                "source_sha256s": ["f" * 64],
                "readable_schema_heads": ["0034_upgrades", "0035_candidate"],
            },
            "compose_sha256": "a" * 64,
            "host_release_key": "core-source",
            "services": [
                UpgradeServiceSpec(
                    service="coire-api",
                    image_id="sha256:" + "a" * 64,
                    configuration={},
                    configuration_sha256=hashlib.sha256(b"{}").hexdigest(),
                ).model_dump(mode="json")
            ],
        }
    )
    candidate = UpgradeReleaseManifest.model_validate(
        source_manifest.model_dump(mode="json")
        | {
            "version": "target",
            "compose_sha256": "b" * 64,
            "host_release_key": "core-target",
            "schema_head": "0035_candidate" if schema_changed else "0034_upgrades",
            "compatibility": {
                "source_sha256s": [source_manifest.sha256()],
                "readable_schema_heads": ["0034_upgrades", "0035_candidate"],
            },
        }
    )
    source = UpgradeRelease.model_validate(
        value.source.model_dump(mode="json")
        | {"manifest": source_manifest, "manifest_sha256": source_manifest.sha256()}
    )
    target = UpgradeRelease.model_validate(
        value.target.model_dump(mode="json")
        | {"manifest": candidate, "manifest_sha256": candidate.sha256()}
    )
    operation = UpgradeOperation.model_validate(
        value.operation.model_dump(mode="json")
        | {
            "kind": "control_plane",
            "node": None,
            "cohort": ["coire-api"],
            "source_sha256": source.manifest_sha256,
            "target_sha256": target.manifest_sha256,
        }
    )
    now = datetime.now(UTC)
    backup = (
        BackupEvidence(
            backup_id="backup",
            project="coire",
            schema_head="0034_upgrades",
            created_at=now,
            sha256_digest="c" * 64,
            size_bytes=1,
            validated_at=now,
            validation_sha256="d" * 64,
        )
        if schema_changed
        else None
    )
    return UpgradeExecutionPlan.model_validate(
        value.model_dump(mode="json")
        | {
            "operation": operation,
            "source": source,
            "target": target,
            "backup": backup,
            "initial_schema_head": "0034_upgrades",
            "initial_schema_sha256": "e" * 64,
            "schema_head_after": candidate.schema_head,
            "maintenance": value.maintenance.model_dump(mode="json")
            | {"targets": ["coire-api", "core", "coire-edge-a", "coire-edge-b"]},
        }
    )


def prepared_artifact_catalog(root: Path) -> UpgradeReleaseRegister:
    catalog = PrivateUpgradeStore(root)
    store = PrivateUpgradeStore(catalog.path("candidate"))
    content = b"verified-wheel-fixture"
    artifact = store.path("node-wheel")
    artifact.write_bytes(content)
    artifact.chmod(0o600)
    manifest = UpgradeReleaseManifest(
        kind=UpgradeKind.STUDIO,
        version="0.2.0",
        source_revision="a" * 40,
        platform="macos-arm64",
        prepared_at=datetime.now(UTC),
        compatibility=UpgradeCompatibility(
            source_sha256s=("b" * 64,), readable_schema_heads=("0034_upgrades",)
        ),
        environment_id="0.2.0",
        environment_sha256="c" * 64,
        artifact_sha256s={"node-wheel": hashlib.sha256(content).hexdigest()},
        staged_bytes=len(content),
    )
    release = UpgradeRelease(
        release_id=uuid4(),
        manifest=manifest,
        manifest_sha256=manifest.sha256(),
        prepared_by=uuid4(),
        prepared_artifact_id="candidate",
    )
    store.write("release.json", release, replace=False)
    return UpgradeReleaseRegister(
        prepared_artifact_id="candidate", expected_sha256=release.manifest_sha256
    )
