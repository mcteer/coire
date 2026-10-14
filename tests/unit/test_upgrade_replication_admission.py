"""Model replication is authenticated before maintenance and cannot cross its epoch."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_upgrade_maintenance import command

from coire_core.settings import Settings
from coire_node.grants import Grants
from coire_node.maintenance import NodeMaintenance
from coire_node.routes import export, models


def test_existing_transfer_finishes_and_retains_visible_drain_obligation(tmp_path: Path) -> None:
    gate = NodeMaintenance(tmp_path / "state", "coire-edge-a")
    app = FastAPI()
    app.state.maintenance = gate
    grants = Grants()
    app.state.grants = grants
    app.state.store = Mock()
    app.include_router(export.router)
    app.include_router(models.router)
    grants.register("a" * 32, "tiny", datetime.now(UTC) + timedelta(minutes=1))
    app.state.store.path_for.return_value = tmp_path
    (tmp_path / "model.safetensors").write_bytes(b"accepted-transfer")
    manifest = Mock()
    manifest.model_dump.return_value = {"fixture": True}

    def read_manifest(_: str) -> Mock:
        assert gate._admitting == 1
        return manifest

    app.state.store.read_manifest.side_effect = read_manifest
    gate.apply(command())
    with TestClient(app) as client:
        assert client.get("/node/export/" + "b" * 32 + "/manifest").status_code == 404
        response = client.get("/node/export/" + "a" * 32 + "/manifest")
        assert response.status_code == 200
        response = client.get("/node/export/" + "a" * 32 + "/files/model.safetensors")
        assert response.status_code == 200 and response.content == b"accepted-transfer"
        response = client.post(
            "/node/models/owner--tiny/export",
            json={
                "grant": "c" * 32,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            },
        )
        assert response.status_code == 503 and len(grants) == 1
    assert gate._admitting == 0
    assert grants.upgrade_entries()[0].state == "active"
    assert "a" * 32 not in grants.upgrade_entries()[0].model_dump_json()
    grants.revoke_for("tiny")
    assert grants.upgrade_entries() == ()


def test_model_metadata_acquisition_cannot_start_during_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = NodeMaintenance(tmp_path / "state", "coire-edge-a")
    app = FastAPI()
    app.state.maintenance = gate
    app.state.settings = Settings()
    app.include_router(models.router)
    gate.apply(command())
    inspect = Mock()
    monkeypatch.setattr(models.hub, "inspect", inspect)
    with TestClient(app) as client:
        response = client.post("/node/models/inspect", json={"repo_id": "fixture/tiny"})
        assert response.status_code == 503
    inspect.assert_not_called()
