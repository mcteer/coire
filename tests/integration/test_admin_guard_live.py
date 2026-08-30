"""Spec SC-002 and FR-005 against the running stack (T042, T044).

The unit-level guard test proves every admin route refuses. This one proves it end to end —
through nginx, with a real database behind it — and that the refusal leaves an audit row and
changes nothing. It also checks where the Hugging Face credential is, which is a property of
the deployment rather than of any one process.
"""

from __future__ import annotations

import json
import os
import subprocess

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run admin-guard tests",
    ),
]

PROJECT = "coire-it"
COMPOSE_DIR = "deploy/compose"

ADMIN_OPERATIONS = [
    ("POST", "/api/v1/admin/models"),
    ("GET", "/api/v1/admin/models"),
    ("GET", "/api/v1/admin/models/00000000-0000-0000-0000-000000000000"),
    ("PATCH", "/api/v1/admin/models/00000000-0000-0000-0000-000000000000"),
    ("DELETE", "/api/v1/admin/models/00000000-0000-0000-0000-000000000000"),
    ("POST", "/api/v1/admin/models/00000000-0000-0000-0000-000000000000/retire"),
    ("POST", "/api/v1/admin/models/00000000-0000-0000-0000-000000000000/retry"),
    ("POST", "/api/v1/admin/models/00000000-0000-0000-0000-000000000000/load"),
    ("GET", "/api/v1/admin/models/00000000-0000-0000-0000-000000000000/job"),
    ("GET", "/api/v1/admin/engines"),
    ("GET", "/api/v1/admin/engines/00000000-0000-0000-0000-000000000000"),
    ("DELETE", "/api/v1/admin/engines/00000000-0000-0000-0000-000000000000"),
    ("GET", "/api/v1/admin/nodes"),
    ("GET", "/api/v1/admin/audit"),
]


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, *args],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
    )


class TestEveryAdminRouteRefuses:
    @pytest.mark.parametrize(("method", "path"), ADMIN_OPERATIONS)
    def test_without_a_credential(self, api_url: str, method: str, path: str) -> None:
        with httpx.Client(base_url=api_url, timeout=15.0) as client:
            resp = client.request(method, path, json={})
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"

    @pytest.mark.parametrize(("method", "path"), ADMIN_OPERATIONS)
    def test_with_a_wrong_credential(self, api_url: str, method: str, path: str) -> None:
        with httpx.Client(base_url=api_url, timeout=15.0) as client:
            resp = client.request(
                method, path, json={}, headers={"Authorization": "Bearer not-the-token"}
            )
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"

    def test_refusals_change_nothing(self, api_url: str, admin_headers: dict[str, str]) -> None:
        with httpx.Client(base_url=api_url, timeout=15.0) as client:
            before = client.get("/api/v1/admin/models", headers=admin_headers).json()
            for method, path in ADMIN_OPERATIONS:
                client.request(method, path, json={"repo_id": "attacker/model"})
            after = client.get("/api/v1/admin/models", headers=admin_headers).json()
        assert [m["id"] for m in before] == [m["id"] for m in after]

    def test_every_refusal_leaves_an_audit_row(
        self, api_url: str, admin_headers: dict[str, str]
    ) -> None:
        """SC-002 is verified by reading the audit log, not by observing that nothing
        happened: a control that leaves no trace of having fired cannot be tested."""
        with httpx.Client(base_url=api_url, timeout=15.0) as client:
            before = client.get(
                "/api/v1/admin/audit", headers=admin_headers, params={"limit": 500}
            ).json()
            refused_before = sum(1 for r in before if r["outcome"] == "refused")

            client.get("/api/v1/admin/models")
            client.post("/api/v1/admin/models", json={"repo_id": "a/b"})

            after = client.get(
                "/api/v1/admin/audit", headers=admin_headers, params={"limit": 500}
            ).json()

        refused_after = sum(1 for r in after if r["outcome"] == "refused")
        assert refused_after >= refused_before + 2
        rows = [r for r in after if r["outcome"] == "refused"]
        assert any(r["action"] == "admin.refused" for r in rows)
        assert all(r["actor"] == "anonymous" for r in rows if r["action"] == "admin.refused")


class TestUserRouteIsOpen:
    def test_the_model_listing_needs_no_credential(self, api_url: str) -> None:
        with httpx.Client(base_url=api_url, timeout=15.0) as client:
            assert client.get("/api/v1/models").status_code == 200


class TestCredentialPlacement:
    """Spec FR-005: the Hugging Face token exists only where a node agent runs."""

    def test_no_control_plane_container_carries_it(self) -> None:
        ids = _compose("ps", "-q").stdout.split()
        assert ids, "no containers running"
        offenders: list[str] = []
        for cid in ids:
            inspect = subprocess.run(["docker", "inspect", cid], capture_output=True, text=True)
            data = json.loads(inspect.stdout)[0]
            name = data["Name"].lstrip("/")
            env = data["Config"].get("Env") or []
            has_hf = any(
                e.split("=", 1)[0].upper().startswith("HF_TOKEN") and e.split("=", 1)[1]
                for e in env
            )
            # node-a is the acquisition origin and is the one place it belongs.
            if has_hf and "node-a" not in name:
                offenders.append(name)
        assert offenders == [], f"Hugging Face token present in: {offenders}"

    def test_the_api_has_no_hugging_face_secret_mounted(self) -> None:
        listing = _compose(
            "exec",
            "-T",
            "coire-api",
            "/app/.venv/bin/python3",
            "-c",
            "import os;print(sorted(os.listdir('/run/secrets')))",
        )
        assert listing.returncode == 0, listing.stderr
        secrets = listing.stdout.strip()
        assert "admin_token" in secrets
        assert "hf" not in secrets.lower()

    def test_the_repository_contains_no_token(self) -> None:
        """A literal token committed anywhere would defeat every other control."""
        grep = subprocess.run(
            ["git", "grep", "-lE", r"hf_[A-Za-z0-9]{30,}", "--", ".", ":!*.lock"],
            capture_output=True,
            text=True,
        )
        assert grep.stdout.strip() == "", f"token-shaped strings in: {grep.stdout}"
