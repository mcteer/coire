"""Opt-in live Code acceptance through Chat and the managed Studio run path."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration


def _configuration() -> tuple[str, str, str, str, str]:
    if os.environ.get("COIRE_TEST_CHAT_CODE") != "1":
        pytest.skip("enable live Studio Code acceptance explicitly")
    url = os.environ.get("COIRE_TEST_CHAT_URL")
    workspace = os.environ.get("COIRE_TEST_CODE_WORKSPACE")
    verified = os.environ.get("COIRE_TEST_CODE_MODEL")
    unverified = os.environ.get("COIRE_TEST_CODE_UNVERIFIED_MODEL")
    service = os.environ.get("COIRE_TEST_CHAT_KEYCHAIN_SERVICE")
    if not all((url, workspace, verified, unverified, service)):
        pytest.skip("configure the prepared workspace, code models and Keychain service")
    assert url and workspace and verified and unverified and service
    key = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service], text=True
    ).strip()
    assert key
    return url, workspace, verified, unverified, key


def _client(url: str, key: str, *, timeout: float = 360) -> httpx.Client:
    return httpx.Client(
        base_url=url,
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {key}",
            "Origin": os.environ.get("COIRE_TEST_CHAT_ORIGIN", "http://localhost:8180"),
        },
    )


def _send_code_turn(
    client: httpx.Client,
    conversation_id: str,
    *,
    revision: int,
    model_id: str,
    workspace_id: str,
    action: str,
    content: str,
    **references: str,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    body = {
        "client_request_id": str(uuid.uuid4()),
        "expected_revision": revision,
        "model_id": model_id,
        "workspace_id": workspace_id,
        "action": action,
        "content": content,
        **references,
    }
    accepted: dict[str, Any] | None = None
    terminal: dict[str, Any] | None = None
    activity_types: list[str] = []
    with client.stream(
        "POST", f"/api/v1/chat/conversations/{conversation_id}/turns", json=body
    ) as response:
        assert response.status_code == 200, response.text
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])["payload"]
            if payload["type"] == "turn.accepted":
                accepted = payload["turn"]
            elif payload["type"].startswith("run.activity"):
                activity_types.append(payload["type"])
            elif payload["type"] == "turn.terminal":
                terminal = payload
                break
    assert accepted is not None and terminal is not None
    detail = client.get(f"/api/v1/chat/conversations/{conversation_id}/turns/{accepted['id']}")
    assert detail.status_code == 200, detail.text
    return detail.json(), terminal, activity_types


def _delete_conversation(client: httpx.Client, conversation_id: str) -> None:
    path = f"/api/v1/chat/conversations/{conversation_id}"
    detail = client.get(path)
    if detail.status_code == 200:
        deleted = client.request(
            "DELETE",
            path,
            json={"expected_revision": detail.json()["conversation"]["revision"]},
        )
        assert deleted.status_code == 202, deleted.text


def test_live_code_research_plan_verified_apply_and_bundle_import() -> None:
    url, workspace, verified, unverified, key = _configuration()
    with _client(url, key) as client:
        created = client.post(
            "/api/v1/chat/conversations", json={"mode": "code", "model_id": verified}
        )
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]
        try:
            revision = created.json()["revision"]
            research, terminal, research_activity = _send_code_turn(
                client,
                conversation_id,
                revision=revision,
                model_id=verified,
                workspace_id=workspace,
                action="research",
                content="Read the file named README. Summarize its one line, and cite README line 1. There are no other files or tests.",
            )
            assert terminal["state"] == "completed", terminal
            source = research["coding_result"]["source_revision"]
            assert research["coding_result"]["citations"][0]["path"] == "README"
            assert research["coding_result"]["citations"][0]["line"] == 1
            revision += 1
            plan, terminal, plan_activity = _send_code_turn(
                client,
                conversation_id,
                revision=revision,
                model_id=verified,
                workspace_id=workspace,
                action="plan",
                research_id=research["turn"]["coding_call_id"],
                content="Plan a tiny change to README: replace Hello World! with Hello Coire!. Include one verification step that checks the new line.",
            )
            assert terminal["state"] == "completed", terminal
            assert plan["coding_result"]["source_revision"] == source
            assert plan["coding_result"]["steps"]
            revision += 1

            refused = client.post(
                f"/api/v1/chat/conversations/{conversation_id}/turns",
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "expected_revision": revision,
                    "model_id": unverified,
                    "workspace_id": workspace,
                    "plan_id": plan["turn"]["coding_call_id"],
                    "action": "apply",
                    "content": "Attempt the same change.",
                },
            )
            assert refused.status_code == 409, refused.text
            assert "harness-verified" in refused.text
            assert (
                client.get(f"/api/v1/chat/conversations/{conversation_id}").json()["conversation"][
                    "revision"
                ]
                == revision
            )

            applied, terminal, apply_activity = _send_code_turn(
                client,
                conversation_id,
                revision=revision,
                model_id=verified,
                workspace_id=workspace,
                action="apply",
                plan_id=plan["turn"]["coding_call_id"],
                content="Implement the plan. Replace the complete README content with the line Hello Coire! followed by a newline. Do not modify any other file.",
            )
            assert terminal["state"] == "completed", terminal
            result = applied["coding_result"]
            assert result["base_revision"] == source
            assert result["head_revision"] != source
            assert result["branch"].startswith("coire/")
            assert "+Hello Coire!" in result["diff_excerpt"]
            assert result["tests"]["status"] == "not_found"  # sample has no tests

            artifact_id = result["artifact_id"]
            metadata = client.get(f"/api/v1/mcp/artifacts/{artifact_id}/metadata")
            assert metadata.status_code == 200, metadata.text
            downloaded = client.get(
                f"/api/v1/chat/conversations/{conversation_id}/turns/{applied['turn']['id']}/artifact"
            )
            assert downloaded.status_code == 200, downloaded.text
            assert len(downloaded.content) == metadata.json()["size_bytes"]
            assert hashlib.sha256(downloaded.content).hexdigest() == metadata.json()["sha256"]
            with tempfile.TemporaryDirectory(prefix="coire-code-bundle-") as root:
                repository = Path(root)
                bundle = repository / "branch.bundle"
                bundle.write_bytes(downloaded.content)
                rows = client.get("/api/v1/workspaces").json()
                upstream = next(row["repository_url"] for row in rows if row["id"] == workspace)
                clone = repository / "repo"
                # The Studio bundle is incremental. Import it onto the registered upstream
                # history rather than an empty repository that lacks the prerequisite commits.
                subprocess.run(
                    ["git", "clone", "--quiet", upstream, str(clone)],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(clone), "bundle", "verify", str(bundle)],
                    check=True,
                    capture_output=True,
                )
                fetched = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(clone),
                        "fetch",
                        str(bundle),
                        f"refs/heads/{result['branch']}",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                assert fetched.returncode == 0, fetched.stderr
                imported = subprocess.check_output(
                    ["git", "-C", str(clone), "show", "FETCH_HEAD:README"], text=True
                )
                assert imported.strip() == "Hello Coire!"
            print(
                "Code result IDs:",
                research["coding_result"]["run_id"],
                plan["coding_result"]["run_id"],
                result["run_id"],
                "activity types:",
                research_activity + plan_activity + apply_activity,
            )
        finally:
            _delete_conversation(client, conversation_id)


def test_live_code_running_stop_kills_and_revokes_token() -> None:
    url, workspace, verified, _, key = _configuration()
    with _client(url, key, timeout=20) as client:
        baseline = {row["id"] for row in client.get("/api/v1/admin/runs").json()}
        created = client.post(
            "/api/v1/chat/conversations", json={"mode": "code", "model_id": verified}
        )
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]
        accepted: list[str] = []
        terminal: list[tuple[str, float]] = []

        def stream() -> None:
            with _client(url, key) as observer:
                body = {
                    "client_request_id": str(uuid.uuid4()),
                    "expected_revision": created.json()["revision"],
                    "model_id": verified,
                    "workspace_id": workspace,
                    "action": "research",
                    "content": "Read README and summarize its line with a citation to README line 1.",
                }
                with observer.stream(
                    "POST", f"/api/v1/chat/conversations/{conversation_id}/turns", json=body
                ) as response:
                    assert response.status_code == 200, response.text
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            payload = json.loads(line[6:])["payload"]
                            if payload["type"] == "turn.accepted":
                                accepted.append(payload["turn"]["id"])
                            elif payload["type"] == "turn.terminal":
                                terminal.append((payload["state"], time.monotonic()))
                                break

        thread = threading.Thread(target=stream, daemon=True)
        thread.start()
        run_id: str | None = None
        stop_at: float | None = None
        try:
            for _ in range(300):
                if terminal:
                    break
                rows = [
                    row
                    for row in client.get("/api/v1/admin/runs").json()
                    if row["id"] not in baseline
                ]
                if rows and rows[0]["state"] == "running" and accepted:
                    run_id = rows[0]["id"]
                    assert rows[0]["node_name"] == "coire-edge-a"
                    stop_at = time.monotonic()
                    stopped = client.post(
                        f"/api/v1/chat/conversations/{conversation_id}/turns/{accepted[0]}/stop",
                        json={"reason": "user_stop"},
                    )
                    assert stopped.status_code == 200, stopped.text
                    break
                time.sleep(0.1)
            assert run_id is not None and stop_at is not None, (
                "run never reached a Studio container"
            )
            thread.join(timeout=15)
            assert terminal and terminal[0][0] == "stopped", terminal
            assert terminal[0][1] - stop_at <= 5
            run = next(
                row for row in client.get("/api/v1/admin/runs").json() if row["id"] == run_id
            )
            assert run["state"] == "killed"

            project = os.environ.get("COIRE_TEST_CHAT_COMPOSE_PROJECT", "coire")
            query = (
                'PGPASSWORD="$(cat /run/secrets/postgres_password)" '
                'psql -U coire -d coire -Atqc "SELECT revoked_at IS NOT NULL FROM run_tokens '
                f"WHERE run_id = '{run_id}'\""
            )
            checked = subprocess.check_output(
                ["docker", "exec", f"{project}-postgres-1", "sh", "-c", query], text=True
            ).strip()
            assert checked == "t"
        finally:
            _delete_conversation(client, conversation_id)


def test_live_code_api_restart_recovers_the_same_run() -> None:
    if os.environ.get("COIRE_TEST_CHAT_CODE_RESTART") != "1":
        pytest.skip("enable the local core API restart gate explicitly")
    url, workspace, verified, _, key = _configuration()
    project = os.environ.get("COIRE_TEST_CHAT_COMPOSE_PROJECT", "coire")
    manifest = Path.home() / ".coire" / "projects" / project / "current" / "compose.json"
    assert manifest.is_file()
    with _client(url, key, timeout=20) as client:
        baseline = {row["id"] for row in client.get("/api/v1/admin/runs").json()}
        created = client.post(
            "/api/v1/chat/conversations", json={"mode": "code", "model_id": verified}
        )
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]
        accepted: list[str] = []

        def stream() -> None:
            try:
                with _client(url, key) as observer:
                    body = {
                        "client_request_id": str(uuid.uuid4()),
                        "expected_revision": created.json()["revision"],
                        "model_id": verified,
                        "workspace_id": workspace,
                        "action": "research",
                        "content": "Read README and summarize its line with a citation to README line 1.",
                    }
                    with observer.stream(
                        "POST", f"/api/v1/chat/conversations/{conversation_id}/turns", json=body
                    ) as response:
                        assert response.status_code == 200, response.text
                        for line in response.iter_lines():
                            if line.startswith("data: "):
                                payload = json.loads(line[6:])["payload"]
                                if payload["type"] == "turn.accepted":
                                    accepted.append(payload["turn"]["id"])
                                if payload["type"] == "turn.terminal":
                                    break
            except httpx.TransportError:
                # Restart closes this observer; durable state is checked below.
                pass

        thread = threading.Thread(target=stream, daemon=True)
        thread.start()
        run_id: str | None = None
        try:
            for _ in range(300):
                try:
                    rows = [
                        row
                        for row in client.get("/api/v1/admin/runs").json()
                        if row["id"] not in baseline
                    ]
                except httpx.TransportError:
                    rows = []
                if rows and rows[0]["state"] == "running" and accepted:
                    run_id = rows[0]["id"]
                    break
                time.sleep(0.1)
            assert run_id is not None, "run did not start before API restart"
            subprocess.run(
                ["docker", "compose", "-p", project, "-f", str(manifest), "restart", "coire-api"],
                check=True,
                capture_output=True,
                timeout=90,
            )
            detail: httpx.Response | None = None
            for _ in range(90):
                try:
                    detail = client.get(
                        f"/api/v1/chat/conversations/{conversation_id}/turns/{accepted[0]}"
                    )
                    if detail.status_code == 200 and detail.json()["turn"]["state"] in {
                        "completed",
                        "failed",
                        "stopped",
                        "interrupted",
                    }:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(1)
            assert detail is not None
            assert detail.status_code == 200, detail.text
            assert detail.json()["turn"]["state"] == "completed", detail.text
            assert detail.json()["coding_result"]["run_id"] == run_id
        finally:
            thread.join(timeout=5)
            _delete_conversation(client, conversation_id)
