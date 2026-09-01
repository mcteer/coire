"""Composed placement, pin immunity, LRU pressure, and durable scheduler recovery."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from conftest import drain_runtime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run placement tests",
    ),
]

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy" / "compose"


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
        text=True,
    )


def _wait(
    client: httpx.Client, decision: dict[str, object], headers: dict[str, str]
) -> dict[str, object]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        decision = client.get(f"/api/v1/admin/placements/{decision['id']}", headers=headers).json()
        if decision["state"] in ("ready", "refused", "failed"):
            return decision
        time.sleep(1)
    return decision


def test_pin_refusal_then_unpin_lru_eviction_survives_scheduler_restart(
    api_url: str, admin_headers: dict[str, str], request: pytest.FixtureRequest
) -> None:
    with httpx.Client(base_url=api_url, timeout=60.0) as client:
        drain_runtime(client, admin_headers)
        models = client.get("/api/v1/admin/models", headers=admin_headers).json()
        candidates: list[tuple[dict[str, object], dict[str, object]]] = []
        for model in models:
            variants = client.get(
                f"/api/v1/admin/models/{model['id']}/variants", headers=admin_headers
            ).json()
            verified = [item for item in variants if item["validated"] and item["state"] == "ready"]
            if verified:
                candidates.append((model, verified[0]))
        assert len(candidates) >= 2, "acquisition scenarios must provide two verified models"
        (first_model, first_variant), (second_model, second_variant) = candidates[:2]

        first = client.post(
            f"/api/v1/admin/models/{first_model['id']}/placement",
            headers=admin_headers,
            json={"variant_id": first_variant["id"], "policy": "single:coire-edge-a"},
        )
        assert first.status_code == 202, first.text
        first_decision = _wait(client, first.json(), admin_headers)
        assert first_decision["state"] == "ready", first_decision

        ledgers = client.get("/api/v1/admin/ledger", headers=admin_headers).json()
        ledger = next(item for item in ledgers if item["node_name"] == "coire-edge-a")
        original_budget = int(ledger["budget_bytes"])
        ledger_id = str(ledger["node_id"])
        request.addfinalizer(
            lambda: _sql(
                "UPDATE node_memory_ledgers "
                f"SET budget_bytes={original_budget} WHERE node_id='{ledger_id}'"
            )
        )
        first_reservation = next(
            item
            for item in ledger["reservations"]
            if item["holder_type"] == "model" and item["holder_id"] == first_model["id"]
        )
        pin = client.patch(
            f"/api/v1/admin/ledger/reservations/{first_reservation['id']}",
            headers=admin_headers,
            json={"pinned": True},
        )
        assert pin.status_code == 204, pin.text
        constrained_budget = (
            int(ledger["reserved_bytes"]) + int(second_variant["memory_estimate_bytes"]) - 1
        )
        changed = client.patch(
            f"/api/v1/admin/ledger/{ledger['node_id']}",
            headers=admin_headers,
            json={"budget_bytes": constrained_budget},
        )
        assert changed.status_code == 200, changed.text

        refused = client.post(
            f"/api/v1/admin/models/{second_model['id']}/placement",
            headers=admin_headers,
            json={"variant_id": second_variant["id"], "policy": "single:coire-edge-a"},
        )
        assert refused.status_code == 202, refused.text
        refused_decision = _wait(client, refused.json(), admin_headers)
        assert refused_decision["state"] == "refused", refused_decision
        assert any(item["reason"] == "pinned" for item in refused_decision["occupants"])

        unpin = client.patch(
            f"/api/v1/admin/ledger/reservations/{first_reservation['id']}",
            headers=admin_headers,
            json={"pinned": False},
        )
        assert unpin.status_code == 204, unpin.text
        audit = client.get(
            "/api/v1/admin/audit",
            headers=admin_headers,
            params={"target_id": first_reservation["id"]},
        ).json()
        assert {item["action"] for item in audit} >= {"model.pin", "model.unpin"}
        accepted = client.post(
            f"/api/v1/admin/models/{second_model['id']}/placement",
            headers=admin_headers,
            json={"variant_id": second_variant["id"], "policy": "single:coire-edge-a"},
        )
        assert accepted.status_code == 202, accepted.text
        subprocess.run(
            ["docker", "compose", "-p", "coire-it", "restart", "coire-scheduler"],
            cwd=COMPOSE_DIR,
            check=True,
            capture_output=True,
        )
        accepted_decision = _wait(client, accepted.json(), admin_headers)
        assert accepted_decision["state"] == "ready", accepted_decision
        assert first_reservation["id"] in accepted_decision["evicted_reservation_ids"]

        final = client.get("/api/v1/admin/ledger", headers=admin_headers).json()
        final_a = next(item for item in final if item["node_name"] == "coire-edge-a")
        assert final_a["reserved_bytes"] <= final_a["budget_bytes"]
        assert not any(item["holder_id"] == first_model["id"] for item in final_a["reservations"])

        second_reservation = next(
            item for item in final_a["reservations"] if item["holder_id"] == second_model["id"]
        )
        ttl = client.patch(
            f"/api/v1/admin/models/{second_model['id']}",
            headers={**admin_headers, "If-Match": second_model["updated_at"]},
            json={"idle_ttl_seconds": 60},
        )
        assert ttl.status_code == 200, ttl.text
        lease_id = uuid.uuid4()
        _sql(
            "UPDATE memory_reservations SET last_used_at=now()-interval '2 minutes' "
            f"WHERE id='{second_reservation['id']}'; "
            "INSERT INTO request_leases (id,reservation_id,request_id,expires_at) VALUES "
            f"('{lease_id}','{second_reservation['id']}','ttl-race',now()+interval '2 minutes');"
        )
        time.sleep(3)
        protected = client.get("/api/v1/admin/ledger", headers=admin_headers).json()
        protected_a = next(item for item in protected if item["node_name"] == "coire-edge-a")
        assert any(item["id"] == second_reservation["id"] for item in protected_a["reservations"])
        _sql(f"UPDATE request_leases SET released_at=now() WHERE id='{lease_id}'")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            after_ttl = client.get("/api/v1/admin/ledger", headers=admin_headers).json()
            after_ttl_a = next(item for item in after_ttl if item["node_name"] == "coire-edge-a")
            if not any(
                item["id"] == second_reservation["id"] for item in after_ttl_a["reservations"]
            ):
                break
            time.sleep(2)
        else:
            raise AssertionError("idle TTL did not release the model reservation")

        sandbox_bytes = next(
            item["bytes"]
            for item in after_ttl_a["reservations"]
            if item["holder_type"] == "sandbox"
        )
        one_model_budget = int(sandbox_bytes) + max(
            int(first_variant["memory_estimate_bytes"]),
            int(second_variant["memory_estimate_bytes"]),
        )
        reset_budget = client.patch(
            f"/api/v1/admin/ledger/{ledger['node_id']}",
            headers=admin_headers,
            json={"budget_bytes": one_model_budget},
        )
        assert reset_budget.status_code == 200, reset_budget.text

        def submit(model: dict[str, object], variant: dict[str, object]) -> dict[str, object]:
            with httpx.Client(base_url=api_url, timeout=60.0) as concurrent_client:
                response = concurrent_client.post(
                    f"/api/v1/admin/models/{model['id']}/placement",
                    headers=admin_headers,
                    json={"variant_id": variant["id"], "policy": "single:coire-edge-a"},
                )
                assert response.status_code == 202, response.text
                return response.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [
                pool.submit(submit, first_model, first_variant),
                pool.submit(submit, second_model, second_variant),
            ]
            concurrent_decisions = [item.result() for item in pending]
        completed = [_wait(client, item, admin_headers) for item in concurrent_decisions]
        assert all(item["state"] in ("ready", "refused") for item in completed)
        concurrent_ledger = client.get("/api/v1/admin/ledger", headers=admin_headers).json()
        concurrent_a = next(
            item for item in concurrent_ledger if item["node_name"] == "coire-edge-a"
        )
        assert concurrent_a["reserved_bytes"] <= concurrent_a["budget_bytes"]
        assert sum(item["holder_type"] == "model" for item in concurrent_a["reservations"]) <= 1
