"""Disposable composed MCP loop using the CI node and deterministic fake engine."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_run_orchestration import prepare_verified_model  # type: ignore[import-not-found]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 for the disposable composed project",
    ),
]

SOURCE = {
    "repository_url": "https://github.com/octocat/Hello-World.git",
    "revision": "HEAD",
}


def _call(
    client: httpx.Client,
    key: str,
    tool: str,
    arguments: dict[str, object],
    identifier: int,
) -> dict[str, Any]:
    __tracebackhide__ = True
    try:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
        )
    except httpx.TimeoutException:
        pytest.fail(f"MCP {tool} timed out: {_recent_runs(client, key)}", pytrace=False)
    assert response.status_code == 200, response.text
    message = response.json()
    assert "error" not in message, message
    if message["result"].get("isError", False):
        pytest.fail(f"MCP {tool} failed: {_recent_runs(client, key)}", pytrace=False)
    return cast(dict[str, Any], message["result"]["structuredContent"])


def _recent_runs(client: httpx.Client, key: str) -> object:
    try:
        with httpx.Client(base_url=client.base_url, timeout=10) as diagnostic:
            response = diagnostic.get("/api/v1/runs", headers={"Authorization": f"Bearer {key}"})
        if response.status_code != 200:
            return {"runs_status": response.status_code}
        return [
            {
                "id": run["id"],
                "state": run["state"],
                "failure_code": run.get("failure_code"),
                "mcp_tool": run.get("mcp_tool"),
                "node_id": run.get("node_id"),
            }
            for run in response.json()[:6]
        ]
    except httpx.HTTPError as exc:
        return {"runs_error": type(exc).__name__}


def test_composed_research_plan_apply_and_branch_import(
    api_url: str,
    admin_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    with httpx.Client(base_url=api_url, timeout=240) as client:
        model_id, variant_id = prepare_verified_model(client, admin_headers)
        model = client.get(f"/api/v1/admin/models/{model_id}", headers=admin_headers).json()
        prior_tags = model["tags"]
        patched = client.patch(
            f"/api/v1/admin/models/{model_id}",
            headers={**admin_headers, "If-Match": model["updated_at"]},
            json={"tags": sorted(set(prior_tags) | {"coding"})},
        )
        assert patched.status_code == 200, patched.text
        existing = client.get("/api/v1/instances", headers=admin_headers)
        assert existing.status_code == 200, existing.text
        ready = next(
            (
                item
                for item in existing.json()
                if item["model_id"] == model_id
                and item["variant_id"] == variant_id
                and item["state"] == "ready"
                and item["policy"] == "single:coire-edge-a"
            ),
            None,
        )
        created_instance = ready is None
        if ready is None:
            launched = client.post(
                "/api/v1/instances",
                headers=admin_headers,
                json={
                    "model_id": model_id,
                    "variant_id": variant_id,
                    "policy": "single:coire-edge-a",
                },
            )
            assert launched.status_code == 202, launched.text
            instance_id = launched.json()["id"]
            deadline = time.monotonic() + 120
            current_instance: httpx.Response | None = None
            while time.monotonic() < deadline:
                current_instance = client.get(
                    f"/api/v1/instances/{instance_id}", headers=admin_headers
                )
                assert current_instance.status_code == 200, current_instance.text
                if current_instance.json()["state"] in {"ready", "failed"}:
                    break
                time.sleep(0.5)
            assert current_instance is not None and current_instance.json()["state"] == "ready"
        else:
            instance_id = ready["id"]
        user_email = f"mcp-{uuid.uuid4().hex[:12]}@integration.test"
        created = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={"email": user_email, "display_name": "MCP test", "role": "user"},
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]
        key_response = client.post(
            f"/api/v1/admin/users/{user_id}/keys",
            headers=admin_headers,
            json={
                "name": "mcp-integration",
                "scopes": ["mcp"],
                "requests_per_minute": 60,
                "monthly_budget_tokens": 100000,
            },
        )
        assert key_response.status_code == 201, key_response.text
        key = key_response.json()["secret"]
        try:
            research = _call(
                client, key, "research", {"source": SOURCE, "question": "Read README"}, 1
            )
            assert research["citations"][0]["path"] == "README"
            assert research["citations"][0]["line"] == 1
            plan = _call(
                client,
                key,
                "plan",
                {
                    "source": SOURCE,
                    "goal": "Update README",
                    "research_result_id": research["result_id"],
                },
                2,
            )
            assert plan["source_revision"] == research["source_revision"]
            assert plan["steps"][0]["acceptance_criteria"]
            applied = _call(
                client,
                key,
                "apply",
                {
                    "source": SOURCE,
                    "plan_result_id": plan["result_id"],
                },
                3,
            )
            assert applied["base_revision"] == research["source_revision"]
            assert applied["branch"].startswith("coire/")
            assert applied["tests"]["status"] == "not_found"
            assert "Changed by Coire" in applied["diff_excerpt"]
            metadata = client.get(
                f"/api/v1/mcp/artifacts/{applied['artifact_id']}/metadata",
                headers={"Authorization": f"Bearer {key}"},
            )
            assert metadata.status_code == 200, metadata.text
            downloaded = client.get(
                applied["artifact_url"], headers={"Authorization": f"Bearer {key}"}
            )
            assert downloaded.status_code == 200, downloaded.text[:200]
            assert len(downloaded.content) == metadata.json()["size_bytes"]
            bundle = tmp_path / "branch.bundle"
            bundle.write_bytes(downloaded.content)
            checkout = tmp_path / "imported"
            subprocess.run(
                ["git", "clone", "-q", SOURCE["repository_url"], str(checkout)], check=True
            )
            subprocess.run(
                ["git", "-C", str(checkout), "fetch", str(bundle), applied["branch"]],
                check=True,
            )
            head = subprocess.check_output(
                ["git", "-C", str(checkout), "rev-parse", "FETCH_HEAD"], text=True
            ).strip()
            assert head == applied["head_revision"]

            failing = _call(
                client,
                key,
                "apply",
                {
                    "source": SOURCE,
                    "plan": "mcp-test-failing: add a failing Python test",
                },
                4,
            )
            assert failing["tests"]["status"] == "failed"
            assert failing["artifact_id"]

            def another(index: int) -> dict[str, Any]:
                with httpx.Client(base_url=api_url, timeout=240) as concurrent:
                    return _call(
                        concurrent,
                        key,
                        "apply",
                        {
                            "source": SOURCE,
                            "plan": f"mcp-test-no-tests-{index}: update README",
                        },
                        index + 5,
                    )

            # The CI mesh has two simulated nodes. Keep this clone-isolation check on
            # node A, where the fake model instance is running; cross-node failover is
            # exercised by its own integration suite.
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
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    left, right = list(pool.map(another, (0, 1)))
            finally:
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
                        "UPDATE nodes SET control_host='coire-edge-b' WHERE name='coire-edge-b'",
                    ],
                    check=True,
                    capture_output=True,
                )
            assert left["branch"] != right["branch"]
            assert left["base_revision"] == right["base_revision"]
        finally:
            if created_instance:
                client.delete(f"/api/v1/instances/{instance_id}", headers=admin_headers)
            current = client.get(f"/api/v1/admin/models/{model_id}", headers=admin_headers)
            if current.status_code == 200:
                client.patch(
                    f"/api/v1/admin/models/{model_id}",
                    headers={**admin_headers, "If-Match": current.json()["updated_at"]},
                    json={"tags": prior_tags},
                )
            client.delete(f"/api/v1/admin/users/{user_id}", headers=admin_headers)
