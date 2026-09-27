"""Feature 002 durable raw-to-MLX acquisition through the composed topology."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run acquisition pipeline tests",
    ),
]

TINY_RAW_REPO = "hf-internal-testing/tiny-random-LlamaForCausalLM"
TINY_MLX_REPO = "mlx-community/SmolLM-135M-Instruct-4bit"
UNSUPPORTED_REPO = "hf-internal-testing/tiny-random-bert"
COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy" / "compose"


def _wait_for_workflow(
    client: httpx.Client, workflow: dict[str, object], headers: dict[str, str]
) -> dict[str, object]:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        workflow = client.get(
            f"/api/v1/admin/acquisitions/{workflow['id']}", headers=headers
        ).json()
        if workflow["state"] in ("succeeded", "failed"):
            return workflow
        time.sleep(2)
    return workflow


def test_raw_pipeline_reaches_two_verified_copies_and_removes_raw(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    with httpx.Client(base_url=api_url, timeout=60.0) as client:
        response = client.post(
            "/api/v1/admin/models/acquisitions",
            headers=admin_headers,
            json={
                "repo_id": TINY_RAW_REPO,
                "keep_raw": False,
                "variant": {"name": "bf16", "precision": "bf16"},
            },
        )
        assert response.status_code in (200, 202), response.text
        workflow = response.json()
        duplicate = client.post(
            "/api/v1/admin/models/acquisitions",
            headers=admin_headers,
            json={
                "repo_id": TINY_RAW_REPO,
                "keep_raw": False,
                "variant": {"name": "bf16", "precision": "bf16"},
            },
        )
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["id"] == workflow["id"]
        workflow = _wait_for_workflow(client, workflow, admin_headers)
        assert workflow["state"] == "succeeded", workflow
        assert [stage["stage"] for stage in workflow["stages"]] == [
            "inspect",
            "pull",
            "convert",
            "validate",
            "replicate",
        ]

        variants = client.get(
            f"/api/v1/admin/models/{workflow['model_id']}/variants", headers=admin_headers
        ).json()
        variant = next(item for item in variants if item["id"] == workflow["variant_id"])
        assert variant["state"] == "ready"
        assert variant["validated"] is True
        assert variant["raw_retained"] is False


def test_unsupported_architecture_is_refused_before_weight_transfer(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    with httpx.Client(base_url=api_url, timeout=60.0) as client:
        response = client.post(
            "/api/v1/admin/models/acquisitions",
            headers=admin_headers,
            json={
                "repo_id": UNSUPPORTED_REPO,
                "variant": {"name": "bf16", "precision": "bf16"},
            },
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "unsupported_architecture"
        assert detail["bytes_transferred"] == 0


def test_node_disk_admission_refuses_before_starting_a_pull(
    node_tokens: dict[str, str],
) -> None:
    payload = json.dumps(
        {
            "job_id": str(uuid.uuid4()),
            "repo_id": TINY_RAW_REPO,
            "slug": "disk-admission-fixture.raw",
            "revision": "main",
            "expected_total_bytes": 10**15,
        }
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "coire-it",
            "exec",
            "-T",
            "node-a",
            "curl",
            "-sS",
            "-w",
            "\n%{http_code}",
            "-H",
            f"Authorization: Bearer {node_tokens['coire-edge-a']}",
            "-H",
            "Content-Type: application/json",
            "http://coire-edge-a:9400/node/jobs/pull",
            "--data-binary",
            payload,
        ],
        cwd=COMPOSE_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    body, code = result.stdout.rsplit("\n", 1)
    assert code == "507", body
    assert "needs" in body and "free" in body


def test_second_variant_dequantizes_verified_copy_without_another_hub_pull(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    with httpx.Client(base_url=api_url, timeout=60.0) as client:
        response = client.post(
            "/api/v1/admin/models/acquisitions",
            headers=admin_headers,
            json={
                "repo_id": TINY_RAW_REPO,
                "keep_raw": False,
                "variant": {"name": "8bit", "precision": "8bit"},
            },
        )
        assert response.status_code == 202, response.text
        workflow = _wait_for_workflow(client, response.json(), admin_headers)
        assert workflow["state"] == "succeeded", workflow
        pull = next(stage for stage in workflow["stages"] if stage["stage"] == "pull")
        assert pull["status"] == "skipped"
        assert "no external pull" in pull["public_summary"]


def test_already_mlx_repository_skips_conversion(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    with httpx.Client(base_url=api_url, timeout=60.0) as client:
        response = client.post(
            "/api/v1/admin/models/acquisitions",
            headers=admin_headers,
            json={
                "repo_id": TINY_MLX_REPO,
                "keep_raw": False,
                "variant": {"name": "upstream", "precision": "4bit"},
            },
        )
        assert response.status_code == 202, response.text
        workflow = _wait_for_workflow(client, response.json(), admin_headers)
        assert workflow["state"] == "succeeded", workflow
        converted = next(stage for stage in workflow["stages"] if stage["stage"] == "convert")
        assert converted["status"] == "succeeded"
        assert converted["public_summary"] == "source is already MLX"


def test_scheduler_restart_recovers_the_queued_workflow(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    with httpx.Client(base_url=api_url, timeout=60.0) as client:
        response = client.post(
            "/api/v1/admin/models/acquisitions",
            headers=admin_headers,
            json={
                "repo_id": TINY_RAW_REPO,
                "keep_raw": False,
                "variant": {"name": "6bit", "precision": "6bit"},
            },
        )
        assert response.status_code == 202, response.text
        subprocess.run(
            ["docker", "compose", "-p", "coire-it", "restart", "coire-scheduler"],
            cwd=COMPOSE_DIR,
            check=True,
            capture_output=True,
        )
        workflow = _wait_for_workflow(client, response.json(), admin_headers)
        assert workflow["state"] == "succeeded", workflow


def test_node_restart_reattaches_the_idempotent_stage_command(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    with httpx.Client(base_url=api_url, timeout=60.0) as client:
        response = client.post(
            "/api/v1/admin/models/acquisitions",
            headers=admin_headers,
            json={
                "repo_id": TINY_RAW_REPO,
                "keep_raw": False,
                "variant": {"name": "4bit", "precision": "4bit"},
            },
        )
        assert response.status_code == 202, response.text
        subprocess.run(
            ["docker", "compose", "-p", "coire-it", "restart", "node-a"],
            cwd=COMPOSE_DIR,
            check=True,
            capture_output=True,
        )
        workflow = _wait_for_workflow(client, response.json(), admin_headers)
        assert workflow["state"] == "succeeded", workflow
