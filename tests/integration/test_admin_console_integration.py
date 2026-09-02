from __future__ import annotations

import os

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run admin-console integration",
    ),
]


def test_admin_snapshot_ask_and_role_guard(
    api_url: str,
    admin_headers: dict[str, str],
    access_token_factory,  # type: ignore[no-untyped-def]
) -> None:
    with httpx.Client(base_url=api_url, timeout=30) as client:
        snapshot = client.get("/api/v1/admin/console", headers=admin_headers)
        assert snapshot.status_code == 200, snapshot.text
        body = snapshot.json()
        assert {node["name"] for node in body["cluster"]["nodes"]} == {
            "coire-edge-a",
            "coire-edge-b",
        }
        assert all("disk_free_bytes" in node for node in body["cluster"]["nodes"])
        activity = client.get("/api/v1/admin/console/activity", headers=admin_headers)
        assert activity.status_code == 200, activity.text
        assert set(activity.json()) == {"items", "next_cursor"}
        answer = client.post(
            "/api/v1/admin/ops/ask",
            headers=admin_headers,
            json={"question": "What needs attention?"},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["sources"]

        user = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={
                "email": "console-user@example.test",
                "display_name": "Console User",
                "role": "user",
            },
        )
        assert user.status_code in {201, 409}
        if user.status_code == 409:
            users = client.get("/api/v1/admin/users", headers=admin_headers).json()
            console_user = next(
                item for item in users if item["email"] == "console-user@example.test"
            )
        else:
            console_user = user.json()
        missing_version = client.patch(
            f"/api/v1/admin/users/{console_user['id']}",
            headers=admin_headers,
            json={"display_name": "Unsafe overwrite"},
        )
        assert missing_version.status_code == 428
        stale_version = client.patch(
            f"/api/v1/admin/users/{console_user['id']}",
            headers={**admin_headers, "If-Match": "2000-01-01T00:00:00Z"},
            json={"display_name": "Stale overwrite"},
        )
        assert stale_version.status_code == 409
        current_version = client.patch(
            f"/api/v1/admin/users/{console_user['id']}",
            headers={**admin_headers, "If-Match": console_user["updated_at"]},
            json={"display_name": "Console User"},
        )
        assert current_version.status_code == 200, current_version.text
        refused = client.get(
            "/api/v1/admin/console",
            headers={
                "cf-access-jwt-assertion": access_token_factory(email="console-user@example.test")
            },
        )
        assert refused.status_code == 403


def test_console_stream_starts_with_reconciliable_snapshot(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    with (
        httpx.Client(base_url=api_url, timeout=30) as client,
        client.stream(
            "GET",
            "/api/v1/admin/console/events",
            headers={**admin_headers, "Last-Event-ID": "1"},
        ) as response,
    ):
        assert response.status_code == 200
        lines = response.iter_lines()
        assert next(line for line in lines if line.startswith("event:")) == "event: reconcile"
        data = next(line for line in lines if line.startswith("data:"))
        assert '"snapshot"' in data
