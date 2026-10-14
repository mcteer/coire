"""Changed identity determines cohort; public summaries omit private configuration."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import JsonValue

from coire_api.upgrades.releases import changed_cohort
from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    ServiceName,
    UpgradeCompatibility,
    UpgradeKind,
    UpgradeRelease,
    UpgradeReleaseManifest,
    UpgradeReleaseSummary,
    UpgradeServiceSpec,
)


def manifest() -> UpgradeReleaseManifest:
    configuration: dict[str, JsonValue] = {"environment": {"TOKEN_FILE": "/private/credential"}}
    digest = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    roles: tuple[ServiceName, ...] = ("coire-file-worker", "coire-api", "coire-web")
    return UpgradeReleaseManifest(
        kind=UpgradeKind.CONTROL_PLANE,
        version="one",
        source_revision="a" * 40,
        platform="linux-arm64",
        prepared_at=datetime.now(UTC),
        compatibility=UpgradeCompatibility(
            source_sha256s=("b" * 64,), readable_schema_heads=("0034_upgrades",)
        ),
        artifact_sha256s={"compose.json": "c" * 64},
        staged_bytes=1,
        project="coire",
        schema_head="0034_upgrades",
        compose_sha256="c" * 64,
        services=tuple(
            UpgradeServiceSpec(
                service=role,
                image_id="sha256:" + "d" * 64,
                configuration=configuration,
                configuration_sha256=digest,
            )
            for role in roles
        ),
    )


def test_unchanged_api_does_not_force_cluster_drain() -> None:
    source = manifest()
    assert changed_cohort(source, source) == ()
    worker = source.services[0].model_copy(update={"image_id": "sha256:" + "f" * 64})
    target = source.model_copy(update={"services": (worker, *source.services[1:])})
    assert changed_cohort(source, target) == ("coire-file-worker",)
    with pytest.raises(UpgradeConflict):
        changed_cohort(source, target.model_copy(update={"services": (worker,)}))


def test_public_release_summary_omits_environment_and_mount_sources() -> None:
    internal = manifest()
    release = UpgradeRelease(
        release_id=uuid4(),
        manifest_sha256=internal.sha256(),
        manifest=internal,
        prepared_artifact_id="candidate",
        prepared_by=uuid4(),
    )
    public = UpgradeReleaseSummary.from_release(release)
    document = public.model_dump_json()
    assert "/private/credential" not in document
    assert "TOKEN_FILE" not in document
    assert "environment" not in document
    assert public.manifest_sha256 == release.manifest_sha256
