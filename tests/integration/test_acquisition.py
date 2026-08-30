"""Acquisition end to end (T029, spec SC-001, SC-003, SC-004).

Runs against the compose stack with two node agents on the simulated mesh. The model is
`mlx-community/Qwen2.5-0.5B-Instruct-4bit` — about 280 MB, MLX-format, ungated — so CI can
repeat it and Principle VII's tiny-model rule holds.

The strongest assertion here is structural rather than observational: `node-b` is attached only
to the mesh network and has no route to the internet, so a copy that arrives there *cannot*
have come from Hugging Face.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

TEST_REPO = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"
RAW_REPO = "meta-llama/Llama-3.2-1B-Instruct"
ACQUIRE_TIMEOUT_S = 900.0


def _api(api_url: str) -> httpx.Client:
    return httpx.Client(base_url=api_url, timeout=30.0)


def add_model(
    client: httpx.Client, headers: dict[str, str], repo: str, **extra: Any
) -> httpx.Response:
    return client.post("/api/v1/admin/models", headers=headers, json={"repo_id": repo, **extra})


def wait_ready(
    client: httpx.Client, headers: dict[str, str], model_id: str, *, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.get(f"/api/v1/admin/models/{model_id}", headers=headers).json()
        if last["state"] in ("ready", "failed"):
            return last
        time.sleep(2.0)
    raise AssertionError(f"model did not settle: state={last.get('state')} job={last.get('job')}")


@pytest.fixture(scope="module")
def acquired(api_url: str, admin_headers: dict[str, str]) -> dict[str, Any]:
    """Acquire the test model once for the whole module."""
    with _api(api_url) as client:
        existing = client.get("/api/v1/admin/models", headers=admin_headers).json()
        for model in existing:
            if model["repo_id"] == TEST_REPO:
                return wait_ready(client, admin_headers, model["id"], timeout=ACQUIRE_TIMEOUT_S)

        resp = add_model(client, admin_headers, TEST_REPO, tags=["general"])
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["state"] == "downloading"
        return wait_ready(client, admin_headers, body["id"], timeout=ACQUIRE_TIMEOUT_S)


class TestHappyPath:
    """SC-001: an admin adds a model and it reaches ready, unattended."""

    def test_the_model_reaches_ready(self, acquired: dict[str, Any]) -> None:
        assert acquired["state"] == "ready", acquired.get("state_reason")
        assert acquired["ready_at"]

    def test_both_studios_hold_a_verified_copy(self, acquired: dict[str, Any]) -> None:
        """SC-003: ready implies exactly two verified copies."""
        copies = acquired["copies"]
        assert len(copies) == 2
        assert all(c["verified"] for c in copies)
        assert {c["node"] for c in copies} == {"coire-edge-a", "coire-edge-b"}
        assert all(not c["mismatched_paths"] for c in copies)

    def test_exactly_one_copy_came_from_hugging_face(self, acquired: dict[str, Any]) -> None:
        """SC-004: one external pull per acquisition; the peer copy is replicated."""
        roles = sorted(c["role"] for c in acquired["copies"])
        assert roles == ["origin", "replica"]

    def test_both_copies_share_the_model_manifest_digest(self, acquired: dict[str, Any]) -> None:
        digests = {c["manifest_sha256"] for c in acquired["copies"]}
        assert len(digests) == 1
        assert acquired["manifest_sha256"] in digests

    def test_the_replica_has_no_route_to_hugging_face(self, acquired: dict[str, Any]) -> None:
        """The structural proof behind SC-004.

        `node-b` is attached only to the internal mesh network, so a verified copy on it
        cannot have been downloaded — it can only have been replicated over the mesh.
        """
        import subprocess

        replica = next(c for c in acquired["copies"] if c["role"] == "replica")
        assert replica["node"] == "coire-edge-b"
        probe = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "coire-it",
                "exec",
                "-T",
                "node-b",
                "python",
                "-c",
                "import socket;socket.setdefaulttimeout(4);"
                "socket.create_connection(('huggingface.co',443))",
            ],
            cwd="deploy/compose",
            capture_output=True,
            text=True,
        )
        assert probe.returncode != 0, "node-b reached the internet; SC-004 is not proven"

    def test_the_registry_records_a_plausible_estimate(self, acquired: dict[str, Any]) -> None:
        assert acquired["memory_estimate_bytes"] > acquired["weight_bytes"]
        assert acquired["precision"].startswith("4bit")
        assert acquired["file_count"] > 0

    def test_the_job_finished_cleanly(
        self, api_url: str, admin_headers: dict[str, str], acquired: dict[str, Any]
    ) -> None:
        with _api(api_url) as client:
            job = client.get(
                f"/api/v1/admin/models/{acquired['id']}/job", headers=admin_headers
            ).json()
        assert job["stage"] == "done"
        assert job["failure_reason"] is None
        assert job["origin_node"] != job["replica_node"]
        assert job["percent"] == pytest.approx(100.0, abs=1.0)

    def test_the_audit_log_records_the_acquisition(
        self, api_url: str, admin_headers: dict[str, str], acquired: dict[str, Any]
    ) -> None:
        with _api(api_url) as client:
            rows = client.get(
                "/api/v1/admin/audit",
                headers=admin_headers,
                params={"target_id": acquired["id"], "limit": 50},
            ).json()
        actions = {r["action"] for r in rows}
        assert "model.add" in actions
        assert "model.ready" in actions
        assert all("token" not in str(r["detail"]).lower() for r in rows)


class TestRejections:
    """Every refusal happens before bytes move and says why (spec FR-010)."""

    def test_a_duplicate_is_refused(
        self, api_url: str, admin_headers: dict[str, str], acquired: dict[str, Any]
    ) -> None:
        with _api(api_url) as client:
            before = len(client.get("/api/v1/admin/models", headers=admin_headers).json())
            resp = add_model(client, admin_headers, TEST_REPO)
            after = len(client.get("/api/v1/admin/models", headers=admin_headers).json())
        assert resp.status_code == 409
        assert before == after

    def test_a_non_mlx_repo_is_refused_with_a_pointer_to_feature_002(
        self, api_url: str, admin_headers: dict[str, str]
    ) -> None:
        with _api(api_url) as client:
            resp = add_model(client, admin_headers, RAW_REPO)
            models = client.get("/api/v1/admin/models", headers=admin_headers).json()
        assert resp.status_code == 422
        body = resp.json()["detail"]
        assert body["reason"] in ("not_mlx_format", "gated", "not_found")
        if body["reason"] == "not_mlx_format":
            assert "002" in body["message"]
        # No row was created for a repository that was refused.
        assert not any(m["repo_id"] == RAW_REPO for m in models)

    def test_a_missing_repo_is_refused(self, api_url: str, admin_headers: dict[str, str]) -> None:
        with _api(api_url) as client:
            resp = add_model(client, admin_headers, "coire-test/definitely-not-a-real-repo")
        assert resp.status_code == 422
        assert resp.json()["detail"]["reason"] == "not_found"


class TestUserVisibility:
    """A model is invisible to a user until an admin publishes it (spec US5)."""

    def test_an_unpublished_model_is_absent_from_the_user_listing(
        self, api_url: str, acquired: dict[str, Any]
    ) -> None:
        with _api(api_url) as client:
            listing = client.get("/api/v1/models").json()
        assert not any(m["id"] == acquired["id"] for m in listing)

    def test_publishing_reveals_it_and_unpublishing_hides_it_again(
        self, api_url: str, admin_headers: dict[str, str], acquired: dict[str, Any]
    ) -> None:
        model_id = acquired["id"]
        with _api(api_url) as client:
            client.patch(
                f"/api/v1/admin/models/{model_id}",
                headers=admin_headers,
                json={"visibility": "published", "description": "small and fast"},
            )
            published = client.get("/api/v1/models").json()
            entry = next((m for m in published if m["id"] == model_id), None)
            assert entry is not None
            assert entry["load_state"] == "cold"
            assert entry["description"] == "small and fast"
            # The user shape carries nothing internal.
            assert "repo_id" not in entry and "copies" not in entry

            client.patch(
                f"/api/v1/admin/models/{model_id}",
                headers=admin_headers,
                json={"visibility": "admin_only"},
            )
            hidden = client.get("/api/v1/models").json()

        assert not any(m["id"] == model_id for m in hidden)
        # Unpublishing must not have touched the files.
        with _api(api_url) as client:
            after = client.get(f"/api/v1/admin/models/{model_id}", headers=admin_headers).json()
        assert after["state"] == "ready"
        assert len(after["copies"]) == 2
