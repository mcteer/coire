"""The typed node client (T017)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from coire_api.nodes_client import NodeClient, NodeError, NodeErrorKind
from coire_core.models.engine import ReconcileRequest
from coire_core.models.harness import ProfileName
from coire_core.models.jobs import ChecksumManifest
from coire_core.models.registry import EngineBackend
from coire_core.models.runs import (
    RunActivity,
    RunActivityPage,
    RunActivityTool,
    RunContainerCreate,
    RunLimits,
)
from coire_core.net import ControlClient
from coire_core.settings import Settings

JOB_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime.now(UTC).isoformat()


async def test_run_activity_client_validates_run_and_cursor() -> None:
    run_id = uuid.uuid4()
    record = RunActivity(
        run_id=run_id,
        sequence=1,
        tool_name=RunActivityTool.READ_FILE,
        state="started",
        created_at=datetime.now(UTC),
    )
    good = RunActivityPage(run_id=run_id, data=[record]).model_dump(mode="json")
    client, seen = _client(lambda _request: _json(good))
    page = await client.run_activity("coire-edge-a", run_id)
    await client.aclose()
    assert page.data == [record]
    assert str(seen[0].url).endswith(f"/node/runs/{run_id}/activity?after_sequence=0&limit=100")

    bad = {**good, "next_sequence": 2}
    client, _ = _client(lambda _request: _json(bad))
    with pytest.raises(NodeError) as exc:
        await client.run_activity("coire-edge-a", run_id)
    await client.aclose()
    assert exc.value.kind is NodeErrorKind.PROTOCOL


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[NodeClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    settings.node_tokens = type(settings.node_tokens)('{"coire-edge-a": "tok-a"}')
    client = NodeClient(settings)
    client._control = ControlClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    )
    return client, seen


def _json(payload: Any, status: int = 200) -> httpx.Response:
    response: httpx.Response = httpx.Response(status, json=payload)
    return response


class TestAddressingAndAuth:
    async def test_requests_go_to_the_control_name_with_the_node_token(self) -> None:
        client, seen = _client(lambda r: _json({"items": []}))
        await client.list_models("coire-edge-a")
        await client.aclose()
        assert "coire-edge-a:9400" in str(seen[0].url)
        assert ".mesh" not in str(seen[0].url)
        assert seen[0].headers["Authorization"] == "Bearer tok-a"

    async def test_a_node_without_a_token_still_sends_a_header(self) -> None:
        """An empty bearer produces a clean 401 from the node rather than a confusing 422."""
        client, seen = _client(lambda r: _json({"items": []}))
        await client.list_models("coire-edge-b")
        await client.aclose()
        assert seen[0].headers["Authorization"] == "Bearer "


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (401, NodeErrorKind.UNAUTHORIZED),
            (404, NodeErrorKind.NOT_FOUND),
            (409, NodeErrorKind.CONFLICT),
            (423, NodeErrorKind.GATED),
            (503, NodeErrorKind.UNAVAILABLE),
            (507, NodeErrorKind.NO_SPACE),
            (500, NodeErrorKind.SERVER),
        ],
    )
    async def test_status_codes_become_kinds(self, status: int, kind: NodeErrorKind) -> None:
        client, _ = _client(lambda r: _json({"detail": "nope"}, status))
        with pytest.raises(NodeError) as exc:
            await client.inspect("coire-edge-a", "a/b")
        await client.aclose()
        assert exc.value.kind is kind
        assert exc.value.detail == "nope"

    async def test_connection_failure_is_unreachable_and_retryable(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        client, _ = _client(boom)
        with pytest.raises(NodeError) as exc:
            await client.health("coire-edge-a")
        await client.aclose()
        assert exc.value.kind is NodeErrorKind.UNREACHABLE
        assert exc.value.retryable

    async def test_a_refusal_on_the_merits_is_not_retryable(self) -> None:
        """Waiting does not make a gated repository ungated."""
        client, _ = _client(lambda r: _json({"detail": "licence"}, 423))
        with pytest.raises(NodeError) as exc:
            await client.inspect("coire-edge-a", "a/b")
        await client.aclose()
        assert not exc.value.retryable

    async def test_non_json_error_bodies_do_not_crash_the_client(self) -> None:
        client, _ = _client(lambda r: httpx.Response(500, text="<html>oops</html>"))
        with pytest.raises(NodeError) as exc:
            await client.health("coire-edge-a")
        await client.aclose()
        assert exc.value.kind is NodeErrorKind.SERVER


class TestVerbs:
    async def test_run_verbs_are_typed_and_bounded(self) -> None:
        status_body = {
            "run_id": str(JOB_ID),
            "container_id": "container-1",
            "state": "created",
            "hardened": True,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/logs"):
                assert request.url.params["offset"] == "7"
                return _json(
                    [
                        {
                            "run_id": str(JOB_ID),
                            "offset": 7,
                            "stream": "stdout",
                            "content": "bounded",
                        }
                    ]
                )
            if request.url.path.endswith("/result"):
                return _json({"run_id": str(JOB_ID), "result": {"ok": True}})
            if request.method == "DELETE":
                assert request.url.params["kill"] == "true"
                return httpx.Response(204)
            return _json(status_body, 201 if request.url.path.endswith("/runs") else 200)

        client, seen = _client(handler)
        command = RunContainerCreate(
            run_id=JOB_ID,
            profile=ProfileName.GENERAL,
            model_id=uuid.uuid4(),
            variant_id=uuid.uuid4(),
            image=f"registry.invalid/coire-agent@sha256:{'a' * 64}",
            argv=["-m", "coire_agent"],
            workspace_ref="workspace-1",
            run_token="x" * 32,
            gateway_url="http://gateway.invalid/v1",
            limits=RunLimits(),
        )
        assert (await client.create_run("coire-edge-a", command)).hardened
        assert (await client.start_run("coire-edge-a", JOB_ID)).container_id == "container-1"
        assert (await client.run_logs("coire-edge-a", JOB_ID, offset=7))[0].content == "bounded"
        assert (await client.wait_run("coire-edge-a", JOB_ID)).state == "created"
        assert (await client.collect_run("coire-edge-a", JOB_ID)).result == {"ok": True}
        await client.remove_run("coire-edge-a", JOB_ID, kill=True)
        await client.aclose()
        assert len(seen) == 6

    async def test_inspect_parses_into_the_model(self) -> None:
        client, _seen = _client(
            lambda r: _json(
                {
                    "repo_id": "mlx-community/tiny",
                    "revision": "deadbeef",
                    "files": [{"path": "a.safetensors", "bytes": 10, "upstream_sha256": "f" * 64}],
                    "total_bytes": 10,
                    "weight_bytes": 10,
                    "is_mlx_format": True,
                    "chat_template_present": True,
                }
            )
        )
        inspection = await client.inspect("coire-edge-a", "mlx-community/tiny")
        await client.aclose()
        assert inspection.revision == "deadbeef"
        assert inspection.is_mlx_format
        assert inspection.files[0].upstream_sha256 == "f" * 64

    async def test_start_engine_distinguishes_created_from_existing(self) -> None:
        """202 started it; 200 means one was already there (spec FR-019)."""
        body = {
            "engine_id": str(JOB_ID),
            "slug": "a--b",
            "port": 9500,
            "state": "starting",
            "estimate_bytes": 1,
            "started_at": NOW,
        }
        client, _ = _client(lambda r: _json(body, 202))
        created, _status = await client.start_engine(
            "coire-edge-a", engine_id=JOB_ID, slug="a--b", estimate_bytes=1
        )
        await client.aclose()
        assert created is False  # 202 => newly started

        client, _ = _client(lambda r: _json(body, 200))
        existing, _status = await client.start_engine(
            "coire-edge-a", engine_id=JOB_ID, slug="a--b", estimate_bytes=1
        )
        await client.aclose()
        assert existing is True

    async def test_vision_start_sends_registry_backend_and_bounded_options(self) -> None:
        body = {
            "engine_id": str(JOB_ID),
            "slug": "verified-vision",
            "backend": "mlx_vlm",
            "port": 9500,
            "state": "starting",
            "estimate_bytes": 4096,
            "started_at": NOW,
        }
        health = {
            "name": "coire-edge-a",
            "agent_version": "0.2.0",
            "uptime_seconds": 1,
            "cpu_percent": 1,
            "memory_total_bytes": 1024,
            "memory_free_bytes": 1024,
            "disk_total_bytes": 1024,
            "disk_free_bytes": 1024,
            "agent_cpu_percent": 1,
            "agent_rss_bytes": 1,
            "collection_budget_ok": True,
            "sampled_at": NOW,
            "path": "control",
            "supported_backends": ["mlx_lm", "mlx_vlm"],
        }
        client, seen = _client(
            lambda request: (
                _json(health) if request.url.path == "/node/health" else _json(body, 202)
            )
        )
        created, status = await client.start_engine(
            "coire-edge-a",
            engine_id=JOB_ID,
            slug="verified-vision",
            estimate_bytes=4096,
            backend=EngineBackend.MLX_VLM,
            vision_cache_size=2,
            max_num_seqs=1,
        )
        await client.aclose()
        assert not created and status.backend is EngineBackend.MLX_VLM
        assert [request.url.path for request in seen] == ["/node/health", "/node/engines"]
        sent = json.loads(seen[1].read())
        assert sent["backend"] == "mlx_vlm"
        assert sent["vision_cache_size"] == 2
        assert sent["max_num_seqs"] == 1
        assert sent["chat_template"] is None

        client, seen = _client(
            lambda _request: _json(
                {key: value for key, value in health.items() if key != "supported_backends"}
            )
        )
        with pytest.raises(NodeError) as unsupported:
            await client.start_engine(
                "coire-edge-a",
                engine_id=JOB_ID,
                slug="verified-vision",
                estimate_bytes=4096,
                backend=EngineBackend.MLX_VLM,
            )
        await client.aclose()
        assert unsupported.value.kind is NodeErrorKind.PROTOCOL
        assert [request.url.path for request in seen] == ["/node/health"]

    async def test_start_import_sends_the_manifest_and_grant(self) -> None:
        manifest = ChecksumManifest(
            slug="a--b",
            repo_id="a/b",
            revision="r",
            files=[],
            total_bytes=0,
            created_at=datetime.now(UTC),
        )
        body = {
            "job_id": str(JOB_ID),
            "kind": "import",
            "slug": "a--b",
            "stage": "queued",
            "started_at": NOW,
            "updated_at": NOW,
        }
        client, seen = _client(lambda r: _json(body, 202))
        await client.start_import(
            "coire-edge-a",
            job_id=JOB_ID,
            slug="a--b",
            source_node="coire-edge-b",
            grant="g" * 32,
            manifest=manifest,
        )
        await client.aclose()
        import json as _json_mod

        sent = _json_mod.loads(seen[0].content)
        assert sent["grant"] == "g" * 32
        assert sent["source_node"] == "coire-edge-b"
        assert sent["manifest"]["slug"] == "a--b"

    async def test_delete_model_tolerates_an_already_absent_copy(self) -> None:
        """Retirement is driven repeatedly by the reconciler; the second pass must not fail."""
        client, _ = _client(lambda r: httpx.Response(404))
        await client.delete_model("coire-edge-a", "a--b")
        await client.aclose()

    async def test_stop_engine_returns_none_when_the_node_has_forgotten_it(self) -> None:
        client, _ = _client(lambda r: httpx.Response(404))
        assert await client.stop_engine("coire-edge-a", JOB_ID) is None
        await client.aclose()

    async def test_reconcile_parses_the_three_buckets(self) -> None:
        client, _ = _client(
            lambda r: _json(
                {
                    "adopted": [
                        {
                            "engine_id": str(JOB_ID),
                            "slug": "a--b",
                            "port": 9500,
                            "state": "ready",
                            "estimate_bytes": 1,
                            "started_at": NOW,
                        }
                    ],
                    "dead": [str(uuid.uuid4())],
                    "orphans": [
                        {
                            "engine_id": None,
                            "slug": "x--y",
                            "port": 9599,
                            "state": "orphan",
                            "estimate_bytes": 0,
                            "started_at": NOW,
                        }
                    ],
                }
            )
        )
        result = await client.reconcile("coire-edge-a", ReconcileRequest())
        await client.aclose()
        assert len(result.adopted) == 1 and len(result.dead) == 1 and len(result.orphans) == 1
        assert result.orphans[0].engine_id is None
