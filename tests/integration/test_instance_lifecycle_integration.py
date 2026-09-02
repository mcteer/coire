from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from conftest import drain_runtime, wait_nodes_healthy

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy/compose"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run instance lifecycle tests",
    ),
]


def _sql(statement: str) -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "coire-it",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "coire",
            "-d",
            "coire",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            statement,
        ],
        cwd=COMPOSE_DIR,
        check=True,
        capture_output=True,
    )


def _sql_value(statement: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "coire-it",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "coire",
            "-d",
            "coire",
            "-Atc",
            statement,
        ],
        cwd=COMPOSE_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _request_and_selected_node(
    client: httpx.Client,
    model_id: str,
    headers: dict[str, str],
    *,
    affinity_node: str | None = None,
) -> str:
    marker = f"route-{uuid.uuid4()}"
    body: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": marker}],
    }
    if affinity_node is not None:
        body["coire_affinity_node"] = affinity_node
    response = client.post("/v1/chat/completions", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return _sql_value(
        "SELECT n.name FROM usage_records u "
        "JOIN engine_processes e ON e.id=u.engine_id "
        "JOIN nodes n ON n.id=e.node_id "
        f"WHERE u.requested_model_id='{model_id}' ORDER BY u.started_at DESC LIMIT 1"
    )


def _wait_instance(
    client: httpx.Client,
    instance_id: str,
    headers: dict[str, str],
    states: set[str],
    timeout: float = 90,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/instances/{instance_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = cast(dict[str, Any], response.json())
        if body["state"] in states:
            return body
        time.sleep(0.5)
    raise AssertionError(f"instance {instance_id} did not reach {states}")


def _verified_candidate(
    client: httpx.Client, headers: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    for model in client.get("/api/v1/admin/models", headers=headers).json():
        variants = client.get(
            f"/api/v1/admin/models/{model['id']}/variants", headers=headers
        ).json()
        for variant in variants:
            if variant["validated"] and variant["state"] == "ready":
                return model, variant
    raise AssertionError("acquisition tests must provide a verified model")


def test_restart_two_instances_drain_and_registration_token_reuse(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    with httpx.Client(base_url=api_url, timeout=60) as client:
        drain_runtime(client, admin_headers)
        wait_nodes_healthy(client, admin_headers)
        model, variant = _verified_candidate(client, admin_headers)
        first = client.post(
            "/api/v1/instances",
            headers=admin_headers,
            json={
                "model_id": model["id"],
                "variant_id": variant["id"],
                "policy": "single:coire-edge-a",
            },
        )
        assert first.status_code == 202, first.text
        first_id = first.json()["id"]
        subprocess.run(
            ["docker", "compose", "-p", "coire-it", "restart", "coire-scheduler"],
            cwd=COMPOSE_DIR,
            check=True,
            capture_output=True,
        )
        first_ready = _wait_instance(client, first_id, admin_headers, {"ready", "failed"})
        assert first_ready["state"] == "ready", first_ready
        assert len(first_ready["members"]) == 1
        with client.stream(
            "GET", f"/api/v1/instances/{first_id}/events", headers=admin_headers
        ) as events:
            first_data = next(line for line in events.iter_lines() if line.startswith("data: "))
            assert json.loads(first_data.removeprefix("data: "))["state"] == "ready"

        # Scheduler restart can briefly leave the peer's registration stale while the first
        # instance is already recovered.  Wait for both node heartbeats before placing the
        # second instance on the other Studio.
        wait_nodes_healthy(client, admin_headers)
        second = client.post(
            "/api/v1/instances",
            headers=admin_headers,
            json={
                "model_id": model["id"],
                "variant_id": variant["id"],
                "policy": "single:coire-edge-b",
            },
        )
        assert second.status_code == 202, second.text
        second_ready = _wait_instance(
            client, second.json()["id"], admin_headers, {"ready", "failed"}
        )
        assert second_ready["state"] == "ready", second_ready
        assert first_ready["members"][0]["node_name"] != second_ready["members"][0]["node_name"]

        _sql(
            "UPDATE model_instances SET in_flight=CASE "
            f"WHEN id='{first_id}' THEN 7 WHEN id='{second_ready['id']}' THEN 0 ELSE in_flight END"
        )
        assert _request_and_selected_node(client, model["id"], admin_headers) == "coire-edge-b"
        assert (
            _request_and_selected_node(
                client,
                model["id"],
                admin_headers,
                affinity_node="coire-edge-a",
            )
            == "coire-edge-a"
        )

        state = client.get("/api/v1/state", headers=admin_headers)
        assert state.status_code == 200, state.text
        live = [item for item in state.json()["instances"] if item["state"] == "ready"]
        assert {first_id, second_ready["id"]}.issubset({item["id"] for item in live})

        lease_id = uuid.uuid4()
        reservation_id = first_ready["members"][0]["reservation_id"]
        _sql(
            "INSERT INTO request_leases (id,reservation_id,request_id,expires_at) VALUES "
            f"('{lease_id}','{reservation_id}','forced-drain',now()+interval '2 minutes')"
        )
        drain_started = time.monotonic()
        drained = client.delete(f"/api/v1/instances/{first_id}", headers=admin_headers)
        assert drained.status_code == 202, drained.text
        stopped = _wait_instance(client, first_id, admin_headers, {"stopped", "failed"})
        assert stopped["state"] == "stopped", stopped
        assert time.monotonic() - drain_started < 10
        assert _request_and_selected_node(client, model["id"], admin_headers) == "coire-edge-b"
        with client.stream(
            "GET", f"/api/v1/instances/{first_id}/events", headers=admin_headers
        ) as events:
            terminal = next(line for line in events.iter_lines() if line.startswith("data: "))
            assert json.loads(terminal.removeprefix("data: "))["state"] == "stopped"

        doomed = client.post(
            "/api/v1/instances",
            headers=admin_headers,
            json={
                "model_id": model["id"],
                "variant_id": variant["id"],
                "policy": "single:coire-missing",
            },
        )
        assert doomed.status_code == 202, doomed.text
        failed = _wait_instance(client, doomed.json()["id"], admin_headers, {"failed"})
        with client.stream(
            "GET", f"/api/v1/instances/{failed['id']}/events", headers=admin_headers
        ) as events:
            failure = next(line for line in events.iter_lines() if line.startswith("data: "))
            assert json.loads(failure.removeprefix("data: "))["state"] == "failed"

        declared = client.post(
            "/api/v1/admin/nodes",
            headers=admin_headers,
            json={
                "name": "coire-test-worker",
                "control_host": "coire-test-worker.lab",
                "memory_total_bytes": 1024,
                "disk_total_bytes": 1024,
            },
        )
        assert declared.status_code == 201, declared.text
        credential = declared.json()
        registration = {
            "name": "coire-test-worker",
            "token": credential["token"],
            "endpoints": {
                "contract_version": 2,
                "control_host": "coire-test-worker.lab",
                "data_host": None,
            },
            "memory_total_bytes": 1024,
            "disk_total_bytes": 1024,
            "agent_version": "integration",
        }
        accepted = client.post("/api/v1/nodes/register", json=registration)
        assert accepted.status_code == 200, accepted.text
        reused = client.post("/api/v1/nodes/register", json=registration)
        assert reused.status_code == 401, reused.text
        # This scenario deliberately leaves the synthetic node unreachable after abuse.
        # Remove only that fixture row so later cluster-wide health/isolation scenarios start
        # from the same two-node topology; its immutable audit evidence remains.
        _sql("DELETE FROM nodes WHERE name='coire-test-worker'")
