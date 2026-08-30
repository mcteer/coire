"""Node acquisition verbs against contracts/node-api.yaml (T026)."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from coire_node.testing.fake_hub import FakeHub
from coire_node.testing.harness import Agent

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "specs/001-model-registry-node-agent/contracts/node-api.yaml"
)


@pytest.fixture(scope="session")
def contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(CONTRACT.read_text())
    return loaded


def validator_for(contract: dict[str, Any], schema_name: str) -> Draft202012Validator:
    schema = {
        **contract["components"]["schemas"][schema_name],
        "$defs": contract["components"]["schemas"],
    }
    text = yaml.dump(schema).replace("#/components/schemas/", "#/$defs/")
    return Draft202012Validator(yaml.safe_load(text))


def wait_for(client: TestClient, job_id: str, *, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/node/jobs/{job_id}").json()
        if body["stage"] in ("done", "failed", "cancelled"):
            return body
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish: {body}")


class TestInspect:
    def test_an_mlx_repo_is_recognised(
        self, client: TestClient, fake_hub: FakeHub, contract: dict[str, Any]
    ) -> None:
        resp = client.post("/node/models/inspect", json={"repo_id": "fake/mlx-tiny"})
        assert resp.status_code == 200
        body = resp.json()
        validator_for(contract, "RepoInspection").validate(body)
        assert body["is_mlx_format"] is True
        assert body["revision"] != "main", "the resolved commit sha must be pinned, not the ref"
        assert body["weight_bytes"] > 0
        assert body["chat_template_present"] is True

    def test_upstream_digests_are_carried_for_lfs_files_only(
        self, client: TestClient, fake_hub: FakeHub
    ) -> None:
        """The Hub publishes sha256 for LFS files; small files legitimately have none."""
        files = {
            f["path"]: f
            for f in client.post("/node/models/inspect", json={"repo_id": "fake/mlx-tiny"}).json()[
                "files"
            ]
        }
        assert files["model-00001-of-00002.safetensors"]["upstream_sha256"]
        assert files["config.json"]["upstream_sha256"] is None

    def test_a_raw_torch_repo_is_not_mlx(self, client: TestClient, fake_hub: FakeHub) -> None:
        body = client.post("/node/models/inspect", json={"repo_id": "fake/raw-torch"}).json()
        assert body["is_mlx_format"] is False

    def test_a_gated_repo_says_gated_not_missing(
        self, client: TestClient, fake_hub: FakeHub
    ) -> None:
        """GatedRepoError subclasses RepositoryNotFoundError, so the obvious ordering would
        send an operator hunting for a typo instead of accepting a licence."""
        resp = client.post("/node/models/inspect", json={"repo_id": "fake/gated"})
        assert resp.status_code == 423
        assert "gated" in resp.json()["detail"].lower()

    def test_a_missing_repo_is_404(self, client: TestClient, fake_hub: FakeHub) -> None:
        assert (
            client.post("/node/models/inspect", json={"repo_id": "fake/missing"}).status_code == 404
        )


class TestPull:
    def test_a_pull_completes_and_produces_a_manifest(
        self, client: TestClient, fake_hub: FakeHub, contract: dict[str, Any], agent: Agent
    ) -> None:
        job_id = str(uuid.uuid4())
        resp = client.post(
            "/node/jobs/pull",
            json={"job_id": job_id, "repo_id": "fake/mlx-tiny", "slug": "fake--mlx-tiny"},
        )
        assert resp.status_code == 202
        validator_for(contract, "JobStatus").validate(resp.json())

        final = wait_for(client, job_id)
        assert final["stage"] == "done", final.get("error")
        assert final["manifest_sha256"]
        assert final["files_done"] == 5
        # The Hub bookkeeping directory must not be in the manifest.
        assert not any(f["path"].startswith(".cache") for f in final["manifest"]["files"])
        assert agent.store.read_manifest("fake--mlx-tiny") is not None

    def test_re_issuing_the_same_job_id_is_a_no_op(
        self, client: TestClient, fake_hub: FakeHub
    ) -> None:
        """A restarted control plane repeats the current stage; it must not get a second
        download (ADR-0005)."""
        job_id = str(uuid.uuid4())
        body = {"job_id": job_id, "repo_id": "fake/mlx-tiny", "slug": "fake--mlx-tiny"}
        assert client.post("/node/jobs/pull", json=body).status_code == 202
        second = client.post("/node/jobs/pull", json=body)
        assert second.status_code == 200, "a repeat must be 200, not a new 202"
        assert second.json()["job_id"] == job_id
        wait_for(client, job_id)

    def test_a_second_job_for_the_same_slug_conflicts(
        self, client: TestClient, fake_hub: FakeHub
    ) -> None:
        first = str(uuid.uuid4())
        client.post(
            "/node/jobs/pull",
            json={"job_id": first, "repo_id": "fake/mlx-tiny", "slug": "fake--mlx-tiny"},
        )
        resp = client.post(
            "/node/jobs/pull",
            json={
                "job_id": str(uuid.uuid4()),
                "repo_id": "fake/mlx-tiny",
                "slug": "fake--mlx-tiny",
            },
        )
        assert resp.status_code == 409
        wait_for(client, first)

    def test_a_pull_that_cannot_fit_is_refused_before_it_starts(
        self, client: TestClient, fake_hub: FakeHub
    ) -> None:
        resp = client.post(
            "/node/jobs/pull",
            json={
                "job_id": str(uuid.uuid4()),
                "repo_id": "fake/mlx-tiny",
                "slug": "fake--mlx-tiny",
                "expected_total_bytes": 10**15,
            },
        )
        assert resp.status_code == 507

    def test_a_gated_repo_fails_the_job_with_a_gating_reason(
        self, client: TestClient, fake_hub: FakeHub
    ) -> None:
        job_id = str(uuid.uuid4())
        client.post(
            "/node/jobs/pull",
            json={"job_id": job_id, "repo_id": "fake/gated", "slug": "fake--gated"},
        )
        final = wait_for(client, job_id)
        assert final["stage"] == "failed"
        assert final["error_kind"] == "gated"


class TestVerify:
    def _pulled(self, client: TestClient) -> str:
        job_id = str(uuid.uuid4())
        client.post(
            "/node/jobs/pull",
            json={"job_id": job_id, "repo_id": "fake/mlx-tiny", "slug": "fake--mlx-tiny"},
        )
        wait_for(client, job_id)
        return "fake--mlx-tiny"

    def test_an_intact_copy_verifies(self, client: TestClient, fake_hub: FakeHub) -> None:
        slug = self._pulled(client)
        job_id = str(uuid.uuid4())
        client.post("/node/jobs/verify", json={"job_id": job_id, "slug": slug})
        assert wait_for(client, job_id)["stage"] == "done"

    def test_a_corrupted_copy_reports_the_offending_path(
        self, client: TestClient, fake_hub: FakeHub, agent: Agent
    ) -> None:
        slug = self._pulled(client)
        target = agent.store.path_for(slug) / "model-00001-of-00002.safetensors"
        target.write_bytes(target.read_bytes()[:-1])  # truncate by one byte

        job_id = str(uuid.uuid4())
        client.post("/node/jobs/verify", json={"job_id": job_id, "slug": slug})
        final = wait_for(client, job_id)
        assert final["stage"] == "failed"
        assert final["error_kind"] == "checksum_mismatch"
        assert final["mismatched_paths"] == ["model-00001-of-00002.safetensors"]


class TestJobRoutes:
    def test_an_unknown_job_is_404(self, client: TestClient) -> None:
        assert client.get(f"/node/jobs/{uuid.uuid4()}").status_code == 404

    def test_jobs_are_listed(self, client: TestClient, fake_hub: FakeHub) -> None:
        job_id = str(uuid.uuid4())
        client.post(
            "/node/jobs/pull",
            json={"job_id": job_id, "repo_id": "fake/mlx-tiny", "slug": "fake--mlx-tiny"},
        )
        wait_for(client, job_id)
        assert any(j["job_id"] == job_id for j in client.get("/node/jobs").json())

    def test_every_job_route_requires_the_node_token(self, agent: Agent) -> None:
        with TestClient(agent.app()) as anon:  # no Authorization header
            assert anon.get("/node/jobs").status_code == 401
            assert anon.post("/node/models/inspect", json={"repo_id": "a/b"}).status_code == 401
            assert anon.get("/node/engines").status_code == 401
