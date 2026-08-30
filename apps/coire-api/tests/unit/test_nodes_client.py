"""The typed node client (T017)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from coire_api.nodes_client import NodeClient, NodeError, NodeErrorKind
from coire_core.models.engine import ReconcileRequest
from coire_core.models.jobs import ChecksumManifest
from coire_core.net import MeshClient
from coire_core.settings import Settings

JOB_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime.now(UTC).isoformat()


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
    client._mesh = MeshClient(client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    return client, seen


def _json(payload: Any, status: int = 200) -> httpx.Response:
    response: httpx.Response = httpx.Response(status, json=payload)
    return response


class TestAddressingAndAuth:
    async def test_requests_go_to_the_mesh_name_with_the_node_token(self) -> None:
        client, seen = _client(lambda r: _json({"items": []}))
        await client.list_models("coire-edge-a")
        await client.aclose()
        assert "coire-edge-a.mesh:9400" in str(seen[0].url)
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
