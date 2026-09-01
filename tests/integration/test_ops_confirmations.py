"""Composed proof for exact, single-use, restart-invalidated ops authority."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast

import httpx
import pytest
from conftest import (  # type: ignore[import-not-found]
    INTEGRATION_SECRETS,
    OPS_SERVICE_TOKEN,
    drain_runtime,
    integration_env,
)
from test_run_orchestration import prepare_verified_model  # type: ignore[import-not-found]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run integration tests",
    ),
]
COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy" / "compose"
_VERIFIED_MODEL: tuple[str, str] | None = None


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


def _sql_scalar(statement: str) -> int:
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
            "-At",
            "-c",
            statement,
        ],
        cwd=COMPOSE_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())


def _recreate_ops(model_id: str) -> None:
    INTEGRATION_SECRETS["COIRE_OPS_MODEL_ID"] = model_id
    subprocess.run(
        ["docker", "compose", "-p", "coire-it", "up", "-d", "--force-recreate", "coire-ops"],
        cwd=COMPOSE_DIR,
        env=integration_env(COMPOSE_PROJECT_NAME="coire-it"),
        check=True,
        capture_output=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        health = subprocess.run(
            [
                "docker",
                "inspect",
                "coire-it-coire-ops-1",
                "--format",
                "{{.State.Health.Status}}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if health.stdout.strip() == "healthy":
            return
        time.sleep(0.5)
    raise AssertionError("coire-ops did not become healthy after model configuration")


def _wait_instance(
    client: httpx.Client,
    instance_id: str,
    headers: dict[str, str],
    states: set[str],
    *,
    timeout: float = 90,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/instances/{instance_id}", headers=headers)
        assert response.status_code == 200, response.text
        latest = cast(dict[str, Any], response.json())
        if latest["state"] in states:
            return latest
        time.sleep(0.25)
    raise AssertionError(f"instance did not reach {states}: {latest}")


def _ready_instance(
    client: httpx.Client, admin_headers: dict[str, str]
) -> tuple[str, dict[str, Any]]:
    global _VERIFIED_MODEL
    drain_runtime(client, admin_headers)
    if _VERIFIED_MODEL is None:
        _VERIFIED_MODEL = prepare_verified_model(client, admin_headers)
    model_id, variant_id = _VERIFIED_MODEL
    response = client.post(
        "/api/v1/instances",
        headers=admin_headers,
        json={
            "model_id": model_id,
            "variant_id": variant_id,
            "policy": "single:coire-edge-a",
        },
    )
    assert response.status_code == 202, response.text
    ready = _wait_instance(client, response.json()["id"], admin_headers, {"ready", "failed"})
    assert ready["state"] == "ready", ready
    return model_id, ready


@contextmanager
def _session_heartbeats(api_url: str, service_headers: dict[str, str], session_id: str) -> Any:
    """Keep a manual ops session alive while CI waits on a model lifecycle."""

    stop = Event()
    with httpx.Client(base_url=api_url, timeout=10) as initial_client:
        initial = initial_client.patch(
            f"/api/v1/internal/ops/sessions/{session_id}", headers=service_headers
        )
        assert initial.status_code == 200, initial.text

    def heartbeat() -> None:
        with httpx.Client(base_url=api_url, timeout=10) as heartbeat_client:
            while not stop.wait(10):
                response = heartbeat_client.patch(
                    f"/api/v1/internal/ops/sessions/{session_id}", headers=service_headers
                )
                if response.status_code != 200:
                    return

    worker = Thread(target=heartbeat, name="ops-session-heartbeat", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=5)


def _context(
    client: httpx.Client,
    human_headers: dict[str, str],
    service_headers: dict[str, str],
) -> tuple[str, str]:
    session_id = str(uuid.uuid4())
    registered = client.post(
        "/api/v1/internal/ops/sessions",
        headers=service_headers,
        json={"session_id": session_id, "service_instance": "ops-integration"},
    )
    assert registered.status_code == 201, registered.text
    conversation = client.post("/api/v1/admin/ops/conversations", headers=human_headers, json={})
    assert conversation.status_code == 201, conversation.text
    return session_id, str(conversation.json()["id"])


def _proposal(
    client: httpx.Client,
    service_headers: dict[str, str],
    *,
    session_id: str,
    conversation_id: str,
    instance: dict[str, Any],
) -> dict[str, Any]:
    # The caller keeps the manually registered session alive around long lifecycle waits.
    heartbeat = client.patch(f"/api/v1/internal/ops/sessions/{session_id}", headers=service_headers)
    assert heartbeat.status_code == 200, heartbeat.text
    action = {
        "operation": "instance.unload",
        "target_type": "instance",
        "target_id": instance["id"],
        "parameters": {},
        "precondition": {
            "resource_version": instance["updated_at"],
            "expected_state": "ready",
        },
    }
    response = client.post(
        "/api/v1/internal/ops/proposals",
        headers=service_headers,
        json={
            "conversation_id": conversation_id,
            "session_id": session_id,
            "action": action,
            "rationale": "The selected instance is ready and idle.",
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def test_unload_requires_exact_human_approval_and_decline_mutates_nothing(
    api_url: str,
    admin_headers: dict[str, str],
    access_token_factory: Callable[..., str],
) -> None:
    human = {"cf-access-jwt-assertion": access_token_factory()}
    service = {"Authorization": f"Bearer {OPS_SERVICE_TOKEN}"}
    with httpx.Client(base_url=api_url, timeout=120) as client:
        _, ready = _ready_instance(client, admin_headers)
        session_id, conversation_id = _context(client, human, service)
        declined = _proposal(
            client,
            service,
            session_id=session_id,
            conversation_id=conversation_id,
            instance=ready,
        )
        refusal = client.post(
            f"/api/v1/admin/ops/proposals/{declined['proposal']['id']}/decline",
            headers=human,
            json={"reason": "integration decline"},
        )
        assert refusal.status_code == 200, refusal.text
        assert refusal.json()["state"] == "declined"
        unchanged = client.get(f"/api/v1/instances/{ready['id']}", headers=admin_headers).json()
        assert unchanged["state"] == "ready"

        issued = _proposal(
            client,
            service,
            session_id=session_id,
            conversation_id=conversation_id,
            instance=unchanged,
        )
        confirmed = client.post(
            f"/api/v1/admin/ops/proposals/{issued['proposal']['id']}/confirm",
            headers=human,
            json={"confirm_token": issued["confirm_token"], "action": issued["proposal"]["action"]},
        )
        assert confirmed.status_code == 202, confirmed.text
        assert confirmed.json()["state"] == "executed"
        stopped = _wait_instance(client, ready["id"], admin_headers, {"stopped", "failed"})
        assert stopped["state"] == "stopped", stopped
        audits = client.get(f"/api/v1/admin/audit?target_id={ready['id']}", headers=human).json()
        actions = {row["action"] for row in audits}
        assert {"ops.action.dispatch", "ops.action.executed"} <= actions


def test_confirmation_is_single_use_not_redirectable_and_restart_invalidates(
    api_url: str,
    admin_headers: dict[str, str],
    access_token_factory: Callable[..., str],
) -> None:
    human = {"cf-access-jwt-assertion": access_token_factory()}
    service = {"Authorization": f"Bearer {OPS_SERVICE_TOKEN}"}
    with httpx.Client(base_url=api_url, timeout=120) as client:
        _, ready = _ready_instance(client, admin_headers)
        session_id, conversation_id = _context(client, human, service)
        issued = _proposal(
            client,
            service,
            session_id=session_id,
            conversation_id=conversation_id,
            instance=ready,
        )
        redirected = {**issued["proposal"]["action"], "target_id": str(uuid.uuid4())}
        mismatch = client.post(
            f"/api/v1/admin/ops/proposals/{issued['proposal']['id']}/confirm",
            headers=human,
            json={"confirm_token": issued["confirm_token"], "action": redirected},
        )
        assert mismatch.status_code == 409

        def confirm() -> int:
            with httpx.Client(base_url=api_url, timeout=30) as contender:
                return contender.post(
                    f"/api/v1/admin/ops/proposals/{issued['proposal']['id']}/confirm",
                    headers=human,
                    json={
                        "confirm_token": issued["confirm_token"],
                        "action": issued["proposal"]["action"],
                    },
                ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(lambda _: confirm(), range(2)))
        assert statuses == [202, 409]

        # Resource-version drift is detected after authority consumption and before mutation.
        with _session_heartbeats(api_url, service, session_id):
            _, ready_again = _ready_instance(client, admin_headers)
        changed = _proposal(
            client,
            service,
            session_id=session_id,
            conversation_id=conversation_id,
            instance=ready_again,
        )
        _sql(
            "UPDATE model_instances SET updated_at=updated_at + interval '1 second' "
            f"WHERE id='{ready_again['id']}'"
        )
        changed_response = client.post(
            f"/api/v1/admin/ops/proposals/{changed['proposal']['id']}/confirm",
            headers=human,
            json={
                "confirm_token": changed["confirm_token"],
                "action": changed["proposal"]["action"],
            },
        )
        assert changed_response.status_code == 409

        current = client.get(f"/api/v1/instances/{ready_again['id']}", headers=admin_headers).json()
        expired = _proposal(
            client,
            service,
            session_id=session_id,
            conversation_id=conversation_id,
            instance=current,
        )
        _sql(
            "UPDATE ops_proposals SET expires_at=now() - interval '1 second' "
            f"WHERE id='{expired['proposal']['id']}'; "
            "UPDATE ops_confirmation_tokens SET expires_at=now() - interval '1 second' "
            f"WHERE proposal_id='{expired['proposal']['id']}'"
        )
        expired_response = client.post(
            f"/api/v1/admin/ops/proposals/{expired['proposal']['id']}/confirm",
            headers=human,
            json={
                "confirm_token": expired["confirm_token"],
                "action": expired["proposal"]["action"],
            },
        )
        assert expired_response.status_code == 409

        # A new volatile service session revokes all authority from its predecessor.
        old = _proposal(
            client,
            service,
            session_id=session_id,
            conversation_id=conversation_id,
            instance=current,
        )
        new_session = str(uuid.uuid4())
        restarted = client.post(
            "/api/v1/internal/ops/sessions",
            headers=service,
            json={"session_id": new_session, "service_instance": "ops-restarted"},
        )
        assert restarted.status_code == 201
        stale = client.post(
            f"/api/v1/admin/ops/proposals/{old['proposal']['id']}/confirm",
            headers=human,
            json={"confirm_token": old["confirm_token"], "action": old["proposal"]["action"]},
        )
        assert stale.status_code == 409


def test_irreversible_proposals_are_unrepresentable_and_leave_no_proposal_audit(
    api_url: str,
    access_token_factory: Callable[..., str],
) -> None:
    human = {"cf-access-jwt-assertion": access_token_factory()}
    service = {"Authorization": f"Bearer {OPS_SERVICE_TOKEN}"}
    with httpx.Client(base_url=api_url, timeout=30) as client:
        session_id, conversation_id = _context(client, human, service)
        before = client.get("/api/v1/admin/audit?action=ops.proposal.create", headers=human).json()
        for operation, target_type in (("model.retire", "model"), ("user.delete", "user")):
            response = client.post(
                "/api/v1/internal/ops/proposals",
                headers=service,
                json={
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "action": {
                        "operation": operation,
                        "target_type": target_type,
                        "target_id": str(uuid.uuid4()),
                        "parameters": {},
                        "precondition": {"resource_version": "v1", "expected_state": "ready"},
                    },
                    "rationale": "must be rejected",
                },
            )
            assert response.status_code == 422
        after = client.get("/api/v1/admin/audit?action=ops.proposal.create", headers=human).json()
        assert len(after) == len(before)


def test_ops_degrades_without_inference_and_recovers_without_restart(
    api_url: str,
    admin_headers: dict[str, str],
    access_token_factory: Callable[..., str],
) -> None:
    human = {"cf-access-jwt-assertion": access_token_factory()}
    with httpx.Client(base_url=api_url, timeout=120) as client:
        model_id, ready = _ready_instance(client, admin_headers)
        _recreate_ops(model_id)
        stopped_response = client.delete(f"/api/v1/instances/{ready['id']}", headers=admin_headers)
        assert stopped_response.status_code == 202, stopped_response.text
        stopped = _wait_instance(client, ready["id"], admin_headers, {"stopped", "failed"})
        assert stopped["state"] == "stopped", stopped

        conversation = client.post("/api/v1/admin/ops/conversations", headers=human, json={})
        assert conversation.status_code == 201, conversation.text
        conversation_id = str(conversation.json()["id"])
        usage_before = _sql_scalar("SELECT count(*) FROM usage_records")

        status_answer = client.post(
            f"/api/v1/admin/ops/conversations/{conversation_id}/messages",
            headers=human,
            json={"question": "What is the current cluster status?"},
        )
        assert status_answer.status_code == 200, status_answer.text
        assert status_answer.json()["status"] == "degraded"
        assert status_answer.json()["degraded"] is True

        action_refusal = client.post(
            f"/api/v1/admin/ops/conversations/{conversation_id}/messages",
            headers=human,
            json={"question": "Unload an idle model instance."},
        )
        assert action_refusal.status_code == 200, action_refusal.text
        assert action_refusal.json()["status"] == "degraded"
        assert action_refusal.json()["proposal"] is None
        assert "cannot create an action proposal" in action_refusal.json()["answer"].lower()
        assert _sql_scalar("SELECT count(*) FROM usage_records") == usage_before

        restored_response = client.post(
            "/api/v1/instances",
            headers=admin_headers,
            json={
                "model_id": model_id,
                "variant_id": ready["variant_id"],
                "policy": "single:coire-edge-a",
            },
        )
        assert restored_response.status_code == 202, restored_response.text
        restored = _wait_instance(
            client, restored_response.json()["id"], admin_headers, {"ready", "failed"}
        )
        assert restored["state"] == "ready", restored
        recovered = client.post(
            f"/api/v1/admin/ops/conversations/{conversation_id}/messages",
            headers=human,
            json={"question": f"Unload ready instance {restored['id']}."},
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "proposed"
        assert recovered.json()["degraded"] is False
        assert recovered.json()["proposal"]["proposal"]["action"]["target_id"] == restored["id"]
        assert _sql_scalar("SELECT count(*) FROM usage_records") > usage_before

        process_table = subprocess.run(
            ["docker", "top", "coire-it-coire-ops-1"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.lower()
        assert "mlx_lm" not in process_table
        assert "metal" not in process_table
