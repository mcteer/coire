"""Measurement control never mounts without node authentication."""

import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from training_measurement_fixtures import experiment

from coire_core.settings import Settings
from coire_node.routes.training_measurements import attach
from coire_node.training.journal import TrainingJournal
from coire_node.training.measurement import MeasurementSupervisor


async def test_authenticated_control_mount_and_strict_probe_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No model/input assets are supplied; exercise only CPU admission and HTTP auth.
    monkeypatch.setattr("coire_node.training.measurement.platform.node", lambda: "coire-edge-a")
    monkeypatch.setattr("coire_node.training.measurement.platform.system", lambda: "Darwin")
    monkeypatch.setattr("coire_node.training.measurement.platform.machine", lambda: "arm64")
    monkeypatch.setattr("coire_node.training.measurement.measurement_hook_available", lambda: True)
    _, dispatch = experiment()
    probe = dispatch.commands[0]
    journal = TrainingJournal(
        tmp_path / "journal", node=probe.prepare.node, admission_lock=threading.RLock()
    )
    native = MeasurementSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/pinned/bin/python"),
        accelerator_guard=lambda _: None,
        hardware_sha256=lambda: probe.hardware_sha256,
        store_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        memory_available=lambda: 16 * 1024**3,
        disk_available=lambda: 100 * 1024**3,
    )
    app = FastAPI()
    app.state.settings = Settings(training_enabled=True, node_name=probe.prepare.node)

    async def authenticated(authorization: str | None = Header(default=None)) -> None:
        if authorization != "Bearer test-node":
            raise HTTPException(401, "node authentication required")

    app.state.require_node_token = authenticated
    attach(app, native)
    url = f"/node/training/measurements/{probe.prepare.attempt_id}/prepare"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://node.test"
    ) as client:
        cap_url = "/node/training/measurements/capabilities"
        assert (await client.get(cap_url)).status_code == 401
        cap = await client.get(cap_url, headers={"Authorization": "Bearer test-node"})
        assert cap.status_code == 200 and cap.json()["node"] == probe.prepare.node
        # Capability is code/inventory metadata; it never substitutes for a real collective.
        assert 1 in cap.json()["world_sizes"]
        assert cap.json()["measurement_checkpoint"] is True
        monkeypatch.setattr("coire_node.training.measurement.platform.system", lambda: "Linux")
        unsupported_cap = await client.get(cap_url, headers={"Authorization": "Bearer test-node"})
        assert unsupported_cap.status_code == 200
        assert unsupported_cap.json()["world_sizes"] == []
        monkeypatch.setattr("coire_node.training.measurement.platform.system", lambda: "Darwin")
        app.state.settings.training_enabled = False
        disabled_cap = await client.get(cap_url, headers={"Authorization": "Bearer test-node"})
        assert disabled_cap.json()["world_sizes"] == []
        app.state.settings.training_enabled = True
        assert (await client.post(url, json=probe.model_dump(mode="json"))).status_code == 401
        assert journal.records() == []
        invalid = {**probe.model_dump(mode="json"), "model_path": "/caller/path"}
        assert (
            await client.post(url, json=invalid, headers={"Authorization": "Bearer test-node"})
        ).status_code == 422
        receipt = await client.post(
            url, json=probe.model_dump(mode="json"), headers={"Authorization": "Bearer test-node"}
        )
        assert receipt.status_code == 200 and not receipt.json()["ready"]
    journal.close()
