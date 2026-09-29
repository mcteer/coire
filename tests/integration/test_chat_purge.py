"""Deleted Chat content is denied immediately and purged in disposable Compose."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1"
        or os.environ.get("COIRE_CHAT_ENABLED") != "true"
        or not os.environ.get("COIRE_CHAT_PUBLIC_ORIGIN"),
        reason="run in the disposable Chat-enabled Compose project",
    ),
]

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy/compose"


def _sql(statement: str) -> str:
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
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            statement,
        ],
        cwd=COMPOSE_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_deleted_conversation_is_denied_then_scrubbed(
    api_url: str,
    admin_headers: dict[str, str],
    access_token_factory: Callable[..., str],
) -> None:
    email = f"chat-purge-{uuid.uuid4().hex[:12]}@integration.test"
    title = f"private-{uuid.uuid4().hex}"
    with httpx.Client(base_url=api_url, timeout=30) as client:
        owner = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={"email": email, "display_name": "Purge Test", "role": "user"},
        )
        assert owner.status_code == 201, owner.text
        client.headers.update(
            {
                "cf-access-jwt-assertion": access_token_factory(email=email),
                "Origin": os.environ["COIRE_CHAT_PUBLIC_ORIGIN"],
            }
        )
        created = client.post("/api/v1/chat/conversations", json={"mode": "chat", "title": title})
        assert created.status_code == 201, created.text
        conversation_id = str(uuid.UUID(created.json()["id"]))
        path = f"/api/v1/chat/conversations/{conversation_id}"
        deleted = client.request(
            "DELETE", path, json={"expected_revision": created.json()["revision"]}
        )
        assert deleted.status_code == 202, deleted.text
        assert client.get(path).status_code == 404
        assert client.get(f"{path}/events").status_code == 404

        subprocess.run(
            ["docker", "compose", "-p", "coire-it", "restart", "coire-api"],
            cwd=COMPOSE_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
        restart_deadline = time.monotonic() + 30
        while time.monotonic() < restart_deadline:
            try:
                if client.get(path).status_code == 404:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail("Deleted conversation was not denied after API restart")
        assert client.get(f"{path}/events").status_code == 404

        # Advance only this private fixture past the fixed five-minute grace period.
        _sql(
            "UPDATE chat_conversations SET deleted_at = now() - interval '6 minutes' "
            f"WHERE id = '{conversation_id}'"
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = _sql(
                "SELECT (purged_at IS NOT NULL)::text || '|' || title "
                f"FROM chat_conversations WHERE id = '{conversation_id}'"
            )
            if row == "true|Deleted conversation":
                break
            time.sleep(0.5)
        else:
            pytest.fail("Chat maintenance did not scrub the deleted conversation")
        assert client.get(path).status_code == 404
