"""Peer replication: export grants, the import job, and the mesh-only rule (T027).

The invariant this file exists for is spec FR-007 / SC-004: a model copy may not cross the
egress interface. That is enforced structurally — the export routes are mounted only on the
mesh app — and the tests here assert both halves: the mesh app serves them, and the egress app
returns 404 even with a valid grant and the fallback marker.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from coire_core.models.node import NetworkPath, NodePath
from coire_node.store import Store
from coire_node.testing.harness import TOKEN, Agent

GRANT = "g" * 44
SLUG = "fake--mlx-tiny"


def _seed(store: Store, slug: str = SLUG) -> None:
    """A verified copy, as a completed pull would leave it."""
    base = store.path_for(slug)
    base.mkdir(parents=True, exist_ok=True)
    (base / "config.json").write_bytes(b'{"quantization": {"bits": 4}}')
    (base / "model.safetensors").write_bytes(bytes(range(256)) * 64)
    (base / "nested").mkdir(exist_ok=True)
    (base / "nested" / "tokenizer.json").write_bytes(b"{}")
    store.write_manifest(store.hash_tree(slug, repo_id="fake/mlx-tiny", revision="abc"))


class TestGrants:
    def test_a_grant_is_required_and_scopes_one_model(self, agent: Agent) -> None:
        _seed(agent.store)
        with agent.client() as client:
            client.post(
                f"/node/models/{SLUG}/export",
                json={
                    "grant": GRANT,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        with TestClient(agent.app()) as anon:
            assert anon.get(f"/node/export/{GRANT}/manifest").status_code == 200
            assert anon.get(f"/node/export/{'z' * 44}/manifest").status_code == 404

    def test_granting_export_for_an_absent_copy_is_404(self, agent: Agent) -> None:
        with agent.client() as client:
            resp = client.post(
                f"/node/models/{SLUG}/export",
                json={
                    "grant": GRANT,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        assert resp.status_code == 404

    def test_an_expired_grant_is_indistinguishable_from_an_unknown_one(self, agent: Agent) -> None:
        """Distinguishing them would confirm that a grant existed."""
        _seed(agent.store)
        agent.grants.register(GRANT, SLUG, datetime.now(UTC) - timedelta(seconds=1))
        with TestClient(agent.app()) as anon:
            assert anon.get(f"/node/export/{GRANT}/manifest").status_code == 404

    def test_revocation_closes_the_path(self, agent: Agent) -> None:
        _seed(agent.store)
        agent.grants.register(GRANT, SLUG, datetime.now(UTC) + timedelta(hours=1))
        with agent.client() as client:
            assert client.delete(f"/node/models/{SLUG}/export").status_code == 204
        with TestClient(agent.app()) as anon:
            assert anon.get(f"/node/export/{GRANT}/manifest").status_code == 404

    def test_a_grant_cannot_reach_another_model(self, agent: Agent) -> None:
        _seed(agent.store)
        _seed(agent.store, "other--model")
        agent.grants.register(GRANT, SLUG, datetime.now(UTC) + timedelta(hours=1))
        with TestClient(agent.app()) as anon:
            body = anon.get(f"/node/export/{GRANT}/manifest").json()
            assert body["slug"] == SLUG


class TestMeshOnly:
    """Spec FR-007: replication may not use the egress interface."""

    def test_the_egress_listener_does_not_serve_exports(self, agent: Agent) -> None:
        _seed(agent.store)
        agent.grants.register(GRANT, SLUG, datetime.now(UTC) + timedelta(hours=1))

        with TestClient(agent.app(NodePath.MESH)) as mesh:
            assert mesh.get(f"/node/export/{GRANT}/manifest").status_code == 200

        with TestClient(agent.app(NodePath.FALLBACK)) as egress:
            headers = {"X-Coire-Path": "fallback", "Authorization": f"Bearer {TOKEN}"}
            # A valid grant, the fallback marker, and a bearer token: still absent.
            assert egress.get(f"/node/export/{GRANT}/manifest", headers=headers).status_code == 404
            assert (
                egress.get(f"/node/export/{GRANT}/files/config.json", headers=headers).status_code
                == 404
            )

    def test_health_still_works_on_the_egress_listener(self, agent: Agent) -> None:
        """The point is to exclude bulk transfer, not to break the fallback path itself."""
        with TestClient(agent.app(NodePath.FALLBACK)) as egress:
            resp = egress.get(
                "/node/health",
                headers={"X-Coire-Path": "fallback", "Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status_code == 200
            assert resp.json()["path"] == "fallback"


class TestSeparatedDataListener:
    def test_exports_exist_only_on_data_listener(self, agent: Agent) -> None:
        _seed(agent.store)
        agent.grants.register(GRANT, SLUG, datetime.now(UTC) + timedelta(hours=1))
        with TestClient(agent.app(NetworkPath.CONTROL)) as control:
            assert control.get(f"/node/export/{GRANT}/manifest").status_code == 404
        with TestClient(agent.app(NetworkPath.DATA)) as data:
            assert data.get(f"/node/export/{GRANT}/manifest").status_code == 200


class TestFileTransfer:
    def test_files_stream_and_ranges_are_honoured(self, agent: Agent) -> None:
        """Range support is what lets an interrupted import resume mid-file."""
        _seed(agent.store)
        agent.grants.register(GRANT, SLUG, datetime.now(UTC) + timedelta(hours=1))
        with TestClient(agent.app()) as anon:
            whole = anon.get(f"/node/export/{GRANT}/files/model.safetensors")
            assert whole.status_code == 200
            part = anon.get(
                f"/node/export/{GRANT}/files/model.safetensors",
                headers={"Range": "bytes=100-"},
            )
            assert part.status_code == 206
            assert part.content == whole.content[100:]

    def test_nested_paths_work(self, agent: Agent) -> None:
        _seed(agent.store)
        agent.grants.register(GRANT, SLUG, datetime.now(UTC) + timedelta(hours=1))
        with TestClient(agent.app()) as anon:
            assert anon.get(f"/node/export/{GRANT}/files/nested/tokenizer.json").status_code == 200

    @pytest.mark.parametrize(
        "path", ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "nested/../../../etc/hosts"]
    )
    def test_traversal_out_of_the_copy_is_refused(self, agent: Agent, path: str) -> None:
        """A manifest arrives from another node, so its paths are not trusted here either."""
        _seed(agent.store)
        agent.grants.register(GRANT, SLUG, datetime.now(UTC) + timedelta(hours=1))
        with TestClient(agent.app()) as anon:
            assert anon.get(f"/node/export/{GRANT}/files/{path}").status_code in (404, 400)


class TestImportRoundTrip:
    """Two real agents: one exports over HTTP, the other imports and verifies."""

    def test_a_copy_replicates_and_verifies(self, tmp_path: Path) -> None:
        origin = Agent(tmp_path / "origin")
        replica = Agent(tmp_path / "replica")
        _seed(origin.store)
        manifest = origin.store.read_manifest(SLUG)
        assert manifest is not None
        origin.grants.register(GRANT, SLUG, datetime.now(UTC) + timedelta(hours=1))

        # Serve the origin on a real socket: the import runs in a worker subprocess and makes
        # genuine HTTP requests, so an ASGI test client would not do.
        config = uvicorn.Config(
            origin.app(NetworkPath.DATA),
            host="127.0.0.1",
            port=0,
            log_level="error",
            ws="none",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started and server.servers:
                break
            time.sleep(0.05)
        assert server.started, "the origin agent did not start"
        port = server.servers[0].sockets[0].getsockname()[1]

        # `source_node` is a literal address here, which MeshClient leaves unsuffixed.
        try:
            job_id = uuid.uuid4()
            replica.settings.node_data_listen_port = port
            created, _ = replica.jobs.start(
                job_id=job_id,
                kind=__import__("coire_core.models.jobs", fromlist=["JobKind"]).JobKind.IMPORT,
                slug=SLUG,
                params={
                    "source_node": "127.0.0.1",
                    "grant": GRANT,
                    "manifest": manifest.model_dump(mode="json"),
                    "node_data_port": port,
                },
            )
            assert created
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = replica.jobs.status(job_id)
                assert status is not None
                if status.is_terminal:
                    break
                time.sleep(0.2)
            assert status is not None
            assert status.stage.value == "done", status.error
            assert replica.store.verify_against(SLUG, manifest) == []
            assert replica.store.read_manifest(SLUG) is not None
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            origin.engines.shutdown()
            replica.engines.shutdown()
