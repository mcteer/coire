"""Existing acquisition contracts reject changed IDs and preserve aggregate disk holds."""

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("field", ["workflow_id", "variant_id", "memory_bytes", "disk_bytes"])
def test_reservation_replay_cannot_change_payload(client: TestClient, field: str) -> None:
    body: dict[str, str | int] = {
        "idempotency_key": str(uuid.uuid4()),
        "workflow_id": str(uuid.uuid4()),
        "variant_id": str(uuid.uuid4()),
        "memory_bytes": 1,
        "disk_bytes": 1,
    }
    assert client.post("/node/jobs/reservations", json=body).status_code == 201
    changed = {**body, field: 2 if field.endswith("bytes") else str(uuid.uuid4())}
    assert client.post("/node/jobs/reservations", json=changed).status_code == 409
    replay = client.post("/node/jobs/reservations", json=body)
    assert replay.status_code == 200 and replay.json()["memory_bytes"] == 1
