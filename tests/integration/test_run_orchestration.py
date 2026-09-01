"""Composed API → DBOS scheduler → node broker → Docker run recovery proof."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run integration tests",
    ),
]

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy" / "compose"
TINY_REPO = "hf-internal-testing/tiny-random-LlamaForCausalLM"


def wait_for(
    client: httpx.Client,
    path: str,
    headers: dict[str, str],
    terminal: set[str],
    *,
    timeout: float = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        latest = response.json()
        if str(latest["state"]) in terminal:
            return latest
        time.sleep(0.5)
    raise AssertionError(f"state did not reach {terminal}: {latest}")


def wait_for_container(run_id: str, *, timeout: float = 15) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.coire.agent-run={run_id}",
                "--format",
                "{{.ID}}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if rows:
            assert len(rows) == 1
            return rows[0]
        time.sleep(0.05)
    raise AssertionError(f"run container did not become active: {run_id}")


def restore_transient_publication(
    api_url: str,
    admin_headers: dict[str, str],
    model_id: str,
    variant_id: str,
    prior_instance_ids: set[str],
) -> None:
    try:
        with httpx.Client(base_url=api_url, timeout=10) as client:
            instances = client.get("/api/v1/instances", headers=admin_headers).json()
            drained_ids: set[str] = set()
            for instance in instances:
                if (
                    instance["model_id"] == model_id
                    and instance["id"] not in prior_instance_ids
                    and instance["state"] == "ready"
                ):
                    response = client.delete(
                        f"/api/v1/instances/{instance['id']}", headers=admin_headers
                    )
                    response.raise_for_status()
                    drained_ids.add(str(instance["id"]))
            deadline = time.monotonic() + 20
            while drained_ids and time.monotonic() < deadline:
                instances = client.get("/api/v1/instances", headers=admin_headers).json()
                terminal = {
                    str(instance["id"])
                    for instance in instances
                    if instance["state"] in {"stopped", "failed"}
                }
                drained_ids -= terminal
                if drained_ids:
                    time.sleep(0.25)
            variants = client.get(
                f"/api/v1/admin/models/{model_id}/variants", headers=admin_headers
            ).json()
            current = next(item for item in variants if item["id"] == variant_id)
            publication = client.patch(
                f"/api/v1/admin/models/{model_id}/variants/{variant_id}",
                headers={**admin_headers, "If-Match": current["updated_at"]},
                json={"published": False, "is_default": False},
            )
            publication.raise_for_status()
            model = client.get(f"/api/v1/admin/models/{model_id}", headers=admin_headers).json()
            restored = client.patch(
                f"/api/v1/admin/models/{model_id}",
                headers={**admin_headers, "If-Match": model["updated_at"]},
                json={"visibility": "admin_only", "tags": []},
            )
            restored.raise_for_status()
    except (httpx.HTTPError, KeyError, StopIteration, ValueError):
        return


def prepare_verified_model(client: httpx.Client, admin_headers: dict[str, str]) -> tuple[str, str]:
    existing_models = client.get("/api/v1/admin/models", headers=admin_headers)
    assert existing_models.status_code == 200, existing_models.text
    acquired: tuple[str, str] | None = None
    for model in existing_models.json():
        variants = client.get(
            f"/api/v1/admin/models/{model['id']}/variants", headers=admin_headers
        ).json()
        reusable = next((variant for variant in variants if variant["validated"]), None)
        has_node_a_copy = any(
            copy.get("node") == "coire-edge-a" and copy.get("verified")
            for copy in model.get("copies", [])
        )
        if reusable is not None and has_node_a_copy and acquired is None:
            acquired = str(model["id"]), str(reusable["id"])
        if (
            model.get("visibility") != "published"
            or "general" not in model.get("tags", [])
            or model.get("capability_profile")
            != {
                "tool_calling": "none",
                "structured_output": "json_mode",
                "context_window": 4096,
            }
            or not has_node_a_copy
        ):
            continue
        reusable = next(
            (
                variant
                for variant in variants
                if variant["published"] and variant["validated"] and variant["harness_verified"]
            ),
            None,
        )
        if reusable is not None:
            return str(model["id"]), str(reusable["id"])

    if acquired is not None:
        model_id, variant_id = acquired
    else:
        response = client.post(
            "/api/v1/admin/models/acquisitions",
            headers=admin_headers,
            json={
                "repo_id": TINY_REPO,
                "keep_raw": False,
                "variant": {"name": "run-integration-bf16", "precision": "bf16"},
            },
        )
        assert response.status_code in (200, 202), response.text
        workflow = wait_for(
            client,
            f"/api/v1/admin/acquisitions/{response.json()['id']}",
            admin_headers,
            {"succeeded", "failed"},
        )
        assert workflow["state"] == "succeeded", workflow
        model_id, variant_id = str(workflow["model_id"]), str(workflow["variant_id"])

    model = client.get(f"/api/v1/admin/models/{model_id}", headers=admin_headers).json()
    updated = client.patch(
        f"/api/v1/admin/models/{model_id}",
        headers={**admin_headers, "If-Match": model["updated_at"]},
        json={
            "visibility": "published",
            "tags": ["general"],
            "capability_profile": {
                "tool_calling": "none",
                "structured_output": "json_mode",
                "context_window": 4096,
            },
        },
    )
    assert updated.status_code == 200, updated.text
    evaluation = client.post(
        "/api/v1/admin/harness-evaluations",
        headers=admin_headers,
        json={
            "variant_id": variant_id,
            "scores": {
                "tool_calling": 1,
                "structured_output": 1,
                "edit_application": 1,
                "long_context": 1,
            },
            "verdict": "passed",
            "harness_version": "integration",
            "engine_version": "fake-mlx-lm",
        },
    )
    assert evaluation.status_code == 201, evaluation.text
    variants = client.get(f"/api/v1/admin/models/{model_id}/variants", headers=admin_headers).json()
    variant = next(item for item in variants if item["id"] == variant_id)
    publication = client.patch(
        f"/api/v1/admin/models/{model_id}/variants/{variant_id}",
        headers={**admin_headers, "If-Match": variant["updated_at"]},
        json={"published": True, "is_default": True},
    )
    assert publication.status_code == 200, publication.text
    return model_id, variant_id


def test_scheduler_restart_preserves_one_real_run_container(
    api_url: str,
    admin_headers: dict[str, str],
    access_token_factory: Callable[..., str],
    request: pytest.FixtureRequest,
) -> None:
    user_email = "run-owner@integration.test"
    user_headers = {"cf-access-jwt-assertion": access_token_factory(email=user_email)}
    with httpx.Client(base_url=api_url, timeout=60) as client:
        users = client.get("/api/v1/admin/users", headers=admin_headers).json()
        if not any(item["email"] == user_email for item in users):
            created_user = client.post(
                "/api/v1/admin/users",
                headers=admin_headers,
                json={"email": user_email, "display_name": "Run Owner", "role": "user"},
            )
            assert created_user.status_code == 201, created_user.text
        model_id, variant_id = prepare_verified_model(client, admin_headers)
        prior_instance_ids = {
            str(instance["id"])
            for instance in client.get("/api/v1/instances", headers=admin_headers).json()
        }
        request.addfinalizer(
            lambda: restore_transient_publication(
                api_url, admin_headers, model_id, variant_id, prior_instance_ids
            )
        )

        # Acquisition needs both replicas. Afterwards, make node B temporarily ineligible
        # for run placement without restarting it (registration credentials are one-time).
        subprocess.run(
            [
                "docker",
                "exec",
                "coire-it-postgres-1",
                "psql",
                "-U",
                "coire",
                "-d",
                "coire",
                "-c",
                "UPDATE nodes SET control_host=NULL WHERE name='coire-edge-b'",
            ],
            check=True,
            capture_output=True,
        )
        request.addfinalizer(
            lambda: subprocess.run(
                [
                    "docker",
                    "exec",
                    "coire-it-postgres-1",
                    "psql",
                    "-U",
                    "coire",
                    "-d",
                    "coire",
                    "-c",
                    "UPDATE nodes SET control_host='coire-edge-b' WHERE name='coire-edge-b'",
                ],
                check=False,
                capture_output=True,
            )
        )

        def submit(workspace_ref: str, task: str) -> dict[str, Any]:
            state_dir = Path(os.environ["COIRE_IT_RUN_WORKSPACE_ROOT"]) / workspace_ref / ".coire"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "request.json").write_text(
                json.dumps(
                    {
                        "profile": "general",
                        "variant_id": variant_id,
                        "task_class": "read",
                        "task": task,
                        "capability_profile": {
                            "tool_calling": "none",
                            "structured_output": "json_mode",
                            "context_window": 4096,
                        },
                        "context_window": 4096,
                    }
                )
            )
            state_dir.chmod(0o777)
            response = client.post(
                "/api/v1/runs",
                headers=user_headers,
                json={
                    "profile": "general",
                    "primary_model_id": model_id,
                    "workspace_ref": workspace_ref,
                    "permitted_model_ids": [model_id],
                    "permitted_tools": [],
                    "spend_limit_tokens": 1000,
                    "limits": {"memory_bytes": 268435456, "timeout_seconds": 60},
                },
            )
            assert response.status_code == 202, response.text
            return cast(dict[str, Any], response.json())

        first = submit("scheduler-restart-first", "coire-harness-json slow-completion")
        first_id = str(first["id"])
        container_id = wait_for_container(first_id)
        second = submit("scheduler-restart-second", "coire-harness-json")
        second_id = str(second["id"])
        time.sleep(0.5)
        queued = client.get(f"/api/v1/runs/{second_id}", headers=user_headers).json()
        assert queued["state"] in {"queued", "placing"}
        assert queued["container_id"] is None

        subprocess.run(
            ["docker", "compose", "-p", "coire-it", "restart", "coire-scheduler"],
            cwd=COMPOSE_DIR,
            check=True,
            capture_output=True,
        )
        after_restart = subprocess.run(
            ["docker", "inspect", container_id, "--format", "{{.Id}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert after_restart.startswith(container_id)
        terminal = wait_for(
            client,
            f"/api/v1/runs/{first_id}",
            user_headers,
            {"succeeded", "failed", "result_collection_failed", "timed_out"},
            timeout=180,
        )
        assert terminal["state"] == "succeeded", json.dumps(terminal, indent=2)
        assert terminal["result"]["output"]["answer"] == "bounded"
        successor = wait_for(
            client,
            f"/api/v1/runs/{second_id}",
            user_headers,
            {"succeeded", "failed", "result_collection_failed", "timed_out"},
            timeout=180,
        )
        assert successor["state"] == "succeeded", json.dumps(successor, indent=2)
        cleanup_deadline = time.monotonic() + 15
        while True:
            containers = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    "label=com.coire.agent-run",
                    "--format",
                    "{{.ID}}",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if not containers:
                break
            if time.monotonic() >= cleanup_deadline:
                raise AssertionError(f"run containers were not removed: {containers}")
            time.sleep(0.25)
