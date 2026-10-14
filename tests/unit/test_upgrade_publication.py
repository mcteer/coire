"""Container publication changes filesystem ownership without trusting received paths."""

import io
from pathlib import Path

import pytest
from upgrade_fixtures import prepared_artifact_catalog as prepared

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import UpgradePublicationBundle
from coire_core.upgrade_artifacts import verify_prepared_release
from coire_core.upgrade_publication import publication_bundle, publish_stream


def test_stream_publication_preserves_exact_bytes_and_refuses_republication(tmp_path: Path) -> None:
    request = prepared(tmp_path / "operator")
    release = verify_prepared_release(tmp_path / "operator", request)
    bundle = publication_bundle(release, {"node-wheel": tmp_path / "operator/candidate/node-wheel"})
    result = publish_stream(io.BytesIO(bundle.model_dump_json().encode()), tmp_path / "service")
    assert result == request
    assert verify_prepared_release(tmp_path / "service", result) == release
    artifact = tmp_path / "service/candidate/node-wheel"
    assert artifact.read_bytes() == b"verified-wheel-fixture"
    assert artifact.stat().st_mode & 0o777 == 0o600
    with pytest.raises(UpgradeConflict):
        publish_stream(io.BytesIO(bundle.model_dump_json().encode()), tmp_path / "service")
    assert artifact.read_bytes() == b"verified-wheel-fixture"


@pytest.mark.parametrize("fault", ["hash", "encoding", "path", "duplicate", "field"])
def test_untrusted_publication_cannot_create_received_paths_or_tampered_artifacts(
    tmp_path: Path, fault: str
) -> None:
    request = prepared(tmp_path / "operator")
    release = verify_prepared_release(tmp_path / "operator", request)
    bundle = publication_bundle(release, {"node-wheel": tmp_path / "operator/candidate/node-wheel"})
    document = bundle.model_dump(mode="json")
    if fault == "hash":
        document["artifacts"][0]["content_base64"] = "dGFtcGVyZWQ="
    elif fault == "encoding":
        document["artifacts"][0]["content_base64"] = "not base64"
    elif fault == "path":
        document["artifacts"][0]["key"] = "../outside"
    elif fault == "duplicate":
        document["artifacts"].append(document["artifacts"][0])
    else:
        document["destination"] = str(tmp_path / "outside")
    import json

    with pytest.raises(UpgradeConflict):
        publish_stream(io.BytesIO(json.dumps(document).encode()), tmp_path / "service")
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "service/candidate").exists()


def test_host_reader_refuses_linked_or_modified_input(tmp_path: Path) -> None:
    request = prepared(tmp_path / "operator")
    release = verify_prepared_release(tmp_path / "operator", request)
    artifact = tmp_path / "operator/candidate/node-wheel"
    linked = tmp_path / "linked"
    linked.symlink_to(artifact)
    from coire_core.errors import UpgradeUnavailable

    with pytest.raises(UpgradeUnavailable):
        publication_bundle(release, {"node-wheel": linked})
    artifact.write_bytes(b"changed")
    with pytest.raises(UpgradeConflict):
        publication_bundle(release, {"node-wheel": artifact})
    with pytest.raises(ValueError):
        UpgradePublicationBundle.model_validate({"release": release, "artifacts": []})
