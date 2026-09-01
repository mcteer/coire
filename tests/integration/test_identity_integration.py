from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy/compose"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run identity integration",
    ),
]


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
            "-Atc",
            statement,
        ],
        cwd=COMPOSE_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_user_key_scope_rotation_revocation_limits_and_audit(
    api_url: str,
    admin_headers: dict[str, str],
    access_token_factory,  # type: ignore[no-untyped-def]
) -> None:
    with httpx.Client(base_url=api_url, timeout=30) as client:
        browser = client.get(
            "/api/v1/me",
            headers={"cf-access-jwt-assertion": access_token_factory()},
        )
        assert browser.status_code == 200, browser.text
        assert browser.json()["email"] == "admin@integration.test"
        unmatched = client.get(
            "/api/v1/me",
            headers={"cf-access-jwt-assertion": access_token_factory(email="absent@example.test")},
        )
        assert unmatched.status_code == 401, unmatched.text
        users = client.get("/api/v1/admin/users", headers=admin_headers)
        assert users.status_code == 200, users.text
        bootstrap = next(item for item in users.json() if item["email"] == "admin@integration.test")
        refused_last_admin = client.delete(
            f"/api/v1/admin/users/{bootstrap['id']}", headers=admin_headers
        )
        assert refused_last_admin.status_code == 409, refused_last_admin.text

        created = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={"email": "key-user@example.test", "display_name": "Key User", "role": "user"},
        )
        assert created.status_code == 201, created.text
        user = created.json()
        issued = client.post(
            f"/api/v1/admin/users/{user['id']}/keys",
            headers=admin_headers,
            json={
                "name": "chat",
                "scopes": ["chat"],
                "requests_per_minute": 2,
                "monthly_budget_tokens": 100,
            },
        )
        assert issued.status_code == 201, issued.text
        key = issued.json()
        secret = key["secret"]
        assert secret.startswith(f"coire_{key['prefix']}_")
        metadata = client.get(f"/api/v1/admin/users/{user['id']}/keys", headers=admin_headers)
        assert metadata.status_code == 200
        assert "secret" not in metadata.text

        key_headers = {"Authorization": f"Bearer {secret}"}
        assert client.get("/v1/models", headers=key_headers).status_code == 200
        assert client.get("/api/v1/admin/users", headers=key_headers).status_code == 403
        limited = client.get("/v1/models", headers=key_headers)
        assert limited.status_code == 429, limited.text
        assert limited.json()["code"] == "rate_limit_exceeded"
        assert int(limited.headers["Retry-After"]) >= 1

        rotated = client.post(f"/api/v1/admin/keys/{key['id']}/rotate", headers=admin_headers)
        assert rotated.status_code == 200, rotated.text
        replacement = rotated.json()["secret"]
        assert replacement != secret
        assert client.get("/v1/models", headers=key_headers).status_code == 401
        revoked = client.delete(f"/api/v1/admin/keys/{key['id']}", headers=admin_headers)
        assert revoked.status_code == 204
        assert (
            client.get("/v1/models", headers={"Authorization": f"Bearer {replacement}"}).status_code
            == 401
        )

        quota_issue = client.post(
            f"/api/v1/admin/users/{user['id']}/keys",
            headers=admin_headers,
            json={
                "name": "quota",
                "scopes": ["chat"],
                "requests_per_minute": 100,
                "monthly_budget_tokens": 1,
            },
        )
        assert quota_issue.status_code == 201, quota_issue.text
        quota = quota_issue.json()
        _sql(
            "INSERT INTO api_key_usage_accumulators "
            "(api_key_id,period_start,period_end,requests,prompt_tokens,completion_tokens) VALUES "
            f"('{uuid.UUID(quota['id'])}',date_trunc('month',now()),"
            "date_trunc('month',now())+interval '1 month',1,1,0)"
        )
        quota_refusal = client.get(
            "/v1/models", headers={"Authorization": f"Bearer {quota['secret']}"}
        )
        assert quota_refusal.status_code == 429, quota_refusal.text
        assert quota_refusal.json()["code"] == "monthly_quota_exceeded"
        assert quota_refusal.json()["budget_tokens"] == 1
        increased = client.patch(
            f"/api/v1/admin/keys/{quota['id']}",
            headers=admin_headers,
            json={"monthly_budget_tokens": 2},
        )
        assert increased.status_code == 200, increased.text
        assert increased.json()["tokens_consumed"] == 1
        assert (
            client.get(
                "/v1/models", headers={"Authorization": f"Bearer {quota['secret']}"}
            ).status_code
            == 200
        )
        keys = client.get(f"/api/v1/admin/users/{user['id']}/keys", headers=admin_headers).json()
        assert next(item for item in keys if item["id"] == quota["id"])["tokens_consumed"] == 1

        deactivated = client.delete(f"/api/v1/admin/users/{user['id']}", headers=admin_headers)
        assert deactivated.status_code == 204, deactivated.text
        assert (
            client.get(
                "/v1/models", headers={"Authorization": f"Bearer {quota['secret']}"}
            ).status_code
            == 401
        )

        audit = client.get("/api/v1/admin/audit?limit=500", headers=admin_headers)
        assert audit.status_code == 200, audit.text
        actions = {item["action"] for item in audit.json()}
        assert {
            "identity.admin.bootstrap",
            "user.create",
            "api_key.create",
            "api_key.update",
            "api_key.rotate",
            "api_key.revoke",
            "user.deactivate",
            "authentication.refused",
        } <= actions
        serialized = json.dumps(audit.json())
        assert secret not in serialized
        assert replacement not in serialized
        assert quota["secret"] not in serialized
        logs = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "coire-it",
                "logs",
                "--no-color",
                "coire-api",
                "coire-mcp",
                "otel-collector",
            ],
            cwd=COMPOSE_DIR,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert secret not in logs
        assert replacement not in logs
        assert quota["secret"] not in logs
