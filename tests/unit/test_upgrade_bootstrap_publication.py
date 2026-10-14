"""Publishing first-install intent cannot claim readiness or replace accepted history."""

import io
from pathlib import Path
from unittest.mock import Mock

import pytest
from upgrade_bootstrap_fixtures import prepared_bootstrap

from coire_core import upgrade_bootstrap_publication as service
from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import CoreBootstrapSeed
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault", [None, "project", "catalog", "retained", "selected", "duplicate", "bound"]
)
def test_bootstrap_publication_retains_only_exact_sealed_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    seed, _ = prepared_bootstrap()
    state = tmp_path / "state"
    store = PrivateUpgradeStore(state / "bootstrap")
    if fault == "retained":
        store.write(
            "initial-seed.json", seed.model_copy(update={"executor_runtime_id": "other-owner"})
        )
    if fault == "selected":
        store.write("selected-seed.json", seed)
    verified = (
        seed.source.model_copy(update={"prepared_artifact_id": "other"})
        if fault == "catalog"
        else seed.source
    )
    verifier = Mock(return_value=verified)
    monkeypatch.setattr(service, "verify_prepared_release", verifier)
    payload = seed.model_dump_json().encode()
    if fault == "duplicate":
        payload = b'{"generation":7,' + payload[1:]
    if fault == "bound":
        payload = b"x" * (service.MAX_SEED_BYTES + 1)
    if fault:
        with pytest.raises(UpgradeConflict):
            service.publish_bootstrap_intent(
                io.BytesIO(payload),
                tmp_path / "catalog",
                state,
                project="foreign" if fault == "project" else "coire",
            )
        assert store.read("initial-seed-request.json", CoreBootstrapSeed) is None
    else:
        assert (
            service.publish_bootstrap_intent(
                io.BytesIO(payload), tmp_path / "catalog", state, project="coire"
            )
            == seed
        )
        assert (
            service.publish_bootstrap_intent(
                io.BytesIO(payload), tmp_path / "catalog", state, project="coire"
            )
            == seed
        )
        assert store.read("initial-seed-request.json", CoreBootstrapSeed) == seed
        assert store.read("initial-seed.json", CoreBootstrapSeed) is None
        assert not (state / "coordination").exists()
        assert store.path("initial-seed-request.json").stat().st_mode & 0o777 == 0o600
        with pytest.raises(UpgradeConflict):
            service.publish_bootstrap_intent(
                io.BytesIO(seed.model_copy(update={"generation": 2}).model_dump_json().encode()),
                tmp_path / "catalog",
                state,
                project="coire",
            )
        assert store.read("initial-seed-request.json", CoreBootstrapSeed) == seed
