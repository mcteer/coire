from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def test_reservation_route_is_idempotent_and_release_is_idempotent(client: TestClient) -> None:
    reservation_id = uuid.uuid4()
    body = {
        "idempotency_key": str(reservation_id),
        "workflow_id": str(uuid.uuid4()),
        "variant_id": str(uuid.uuid4()),
        "memory_bytes": 1,
        "disk_bytes": 1,
    }
    first = client.post("/node/jobs/reservations", json=body)
    assert first.status_code == 201
    assert first.json()["state"] == "held"
    second = client.post("/node/jobs/reservations", json=body)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert client.delete(f"/node/jobs/reservations/{reservation_id}").status_code == 204
    assert client.delete(f"/node/jobs/reservations/{reservation_id}").status_code == 204


def test_convert_requires_a_held_reservation(client: TestClient) -> None:
    response = client.post(
        "/node/jobs/convert",
        json={
            "job_id": str(uuid.uuid4()),
            "repo_id": "org/model",
            "revision": "a" * 40,
            "source_slug": "org--model.raw",
            "target_slug": "org--model.4bit",
            "reservation_id": str(uuid.uuid4()),
            "recipe": {"name": "4bit", "precision": "4bit", "bits": 4},
        },
    )
    assert response.status_code == 409
    assert "reservation" in response.json()["detail"]
