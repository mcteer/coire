"""Full bytes, rather than labels or caller paths, authorize staged releases."""

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import JsonValue
from upgrade_fixtures import prepared_artifact_catalog as prepared

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.upgrade_artifacts import verify_prepared_release


def test_verified_content_and_manifest_identity(tmp_path: Path) -> None:
    request = prepared(tmp_path / "prepared")
    assert (
        verify_prepared_release(tmp_path / "prepared", request).manifest_sha256
        == request.expected_sha256
    )
    with pytest.raises(UpgradeConflict):
        verify_prepared_release(
            tmp_path / "prepared", request.model_copy(update={"expected_sha256": "f" * 64})
        )
    (tmp_path / "prepared/candidate/node-wheel").write_bytes(b"tampered")
    with pytest.raises(UpgradeConflict):
        verify_prepared_release(tmp_path / "prepared", request)


@pytest.mark.parametrize("unsafe", [False, True])
def test_core_publication_verifies_fixed_one_shot_migration(tmp_path: Path, unsafe: bool) -> None:
    import json

    from upgrade_fixtures import core_plan

    from coire_core.models.upgrades import UpgradeServiceSpec
    from coire_core.upgrade_artifacts import publish_prepared_release

    original = core_plan().target
    services = tuple(
        UpgradeServiceSpec(
            service=item.service,
            image_id=item.image_id,
            configuration={"image": item.image_id},
            configuration_sha256=hashlib.sha256(
                json.dumps({"image": item.image_id}, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )
        for item in original.manifest.services
    )
    migration = {
        "image": "sha256:" + "c" * 64,
        "user": "65532:65532",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "always" if unsafe else "no",
        "pids_limit": 128,
        "mem_limit": "512m",
        "networks": {"coire-db": {}, "coire-telemetry": {}},
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }
    payload = json.dumps(
        {
            "name": "coire",
            "services": {
                **{item.service: item.configuration for item in services},
                "coire-migrate": migration,
            },
        }
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    manifest = original.manifest.model_copy(
        update={
            "compose_sha256": digest,
            "artifact_sha256s": {"compose.json": digest},
            "staged_bytes": len(payload),
            "services": services,
        }
    )
    release = original.model_copy(
        update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
    )
    artifact = tmp_path / "compose.json"
    artifact.write_bytes(payload)
    if unsafe:
        with pytest.raises(UpgradeConflict, match="migration configuration"):
            publish_prepared_release(tmp_path / "catalog", release, {"compose.json": artifact})
        assert not (tmp_path / "catalog" / release.prepared_artifact_id).exists()
    else:
        request = publish_prepared_release(
            tmp_path / "catalog", release, {"compose.json": artifact}
        )
        assert verify_prepared_release(tmp_path / "catalog", request) == release


def test_linked_artifact_cannot_supply_content(tmp_path: Path) -> None:
    request = prepared(tmp_path / "prepared")
    artifact = tmp_path / "prepared/candidate/node-wheel"
    artifact.rename(tmp_path / "outside")
    artifact.symlink_to(tmp_path / "outside")
    with pytest.raises(UpgradeUnavailable):
        verify_prepared_release(tmp_path / "prepared", request)


def test_publish_prepared_release_is_immutable_and_failure_leaves_no_candidate(
    tmp_path: Path,
) -> None:
    from coire_core.upgrade_artifacts import publish_prepared_release

    reference = prepared(tmp_path / "original")
    release = verify_prepared_release(tmp_path / "original", reference)
    artifact = tmp_path / "original/candidate/node-wheel"
    destination = tmp_path / "published"
    request = publish_prepared_release(destination, release, {"node-wheel": artifact})
    assert verify_prepared_release(destination, request) == release
    with pytest.raises(UpgradeConflict):
        publish_prepared_release(destination, release, {"node-wheel": artifact})
    assert verify_prepared_release(destination, request) == release
    artifact.write_bytes(b"changed")
    failed = tmp_path / "failed"
    with pytest.raises(UpgradeConflict):
        publish_prepared_release(failed, release, {"node-wheel": artifact})
    assert not (failed / "candidate").exists()
    assert not list(failed.glob("*.pending"))


def test_core_preparation_binds_frozen_compose_and_each_service_config(tmp_path: Path) -> None:
    import json

    from upgrade_fixtures import core_plan

    from coire_core.models.upgrades import UpgradeServiceSpec
    from coire_core.upgrade_artifacts import publish_prepared_release, read_prepared_compose

    original = core_plan().target
    config: dict[str, JsonValue] = {"image": "sha256:" + "b" * 64, "read_only": True}
    payload = json.dumps({"name": "coire", "services": {"coire-api": config}}).encode()
    digest = hashlib.sha256(payload).hexdigest()
    manifest = original.manifest.model_copy(
        update={
            "compose_sha256": digest,
            "artifact_sha256s": {"compose.json": digest},
            "staged_bytes": len(payload),
            "services": (
                UpgradeServiceSpec(
                    service="coire-api",
                    image_id=str(config["image"]),
                    configuration=config,
                    configuration_sha256=hashlib.sha256(
                        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                ),
            ),
        }
    )
    release = original.model_copy(
        update={"manifest": manifest, "manifest_sha256": manifest.sha256()}
    )
    artifact = tmp_path / "compose.json"
    artifact.write_bytes(payload)
    root = tmp_path / "good"
    request = publish_prepared_release(root, release, {"compose.json": artifact})
    assert verify_prepared_release(root, request) == release
    assert read_prepared_compose(root, release) == json.loads(payload)
    with pytest.raises(UpgradeConflict):
        read_prepared_compose(root, release.model_copy(update={"prepared_by": uuid4()}))
    wrong = manifest.model_copy(update={"compose_sha256": "f" * 64})
    with pytest.raises(UpgradeConflict, match="Compose identity"):
        publish_prepared_release(
            tmp_path / "wrong",
            release.model_copy(
                update={
                    "manifest": wrong,
                    "manifest_sha256": wrong.sha256(),
                }
            ),
            {"compose.json": artifact},
        )

    changed_config = {**config, "read_only": False}
    changed_service = UpgradeServiceSpec(
        service="coire-api",
        image_id=str(config["image"]),
        configuration=changed_config,
        configuration_sha256=hashlib.sha256(
            json.dumps(changed_config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    wrong = manifest.model_copy(update={"services": (changed_service,)})
    with pytest.raises(UpgradeConflict, match="service configuration"):
        publish_prepared_release(
            tmp_path / "wrong-config",
            release.model_copy(
                update={
                    "manifest": wrong,
                    "manifest_sha256": wrong.sha256(),
                }
            ),
            {"compose.json": artifact},
        )
