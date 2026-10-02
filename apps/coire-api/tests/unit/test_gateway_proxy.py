from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from coire_api.gateway import proxy
from coire_core.errors import ChatModelUnavailable
from coire_core.settings import Settings


@pytest.mark.asyncio
async def test_engine_client_is_reused_and_closed() -> None:
    await proxy.close_engine_client()
    proxy.init_engine_client()
    first = proxy._client()
    assert proxy._client() is first

    await proxy.close_engine_client()
    assert first.is_closed


@pytest.mark.asyncio
async def test_stream_records_upstream_first_chunk_timing() -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: {}\n\ndata: [DONE]\n\n")

    await proxy.close_engine_client()
    proxy._engine_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    timing = proxy.StreamTiming()
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]

    chunks = [chunk async for chunk in proxy.stream("http://engine", {}, settings, timing)]

    assert chunks
    assert timing.upstream_started_at is not None
    assert timing.first_chunk_at is not None
    assert timing.request_started_at <= timing.upstream_started_at <= timing.first_chunk_at
    await proxy.close_engine_client()


@pytest.mark.asyncio
async def test_truncated_stream_emits_retry_guidance() -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'data: {"choices":[]}\n\n')

    await proxy.close_engine_client()
    proxy._engine_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    chunks: list[bytes] = []
    with pytest.raises(proxy.EngineProxyError):
        async for chunk in proxy.stream("http://engine", {}, settings):
            chunks.append(chunk)
    terminal = chunks[-1].decode()
    assert "retry: 1000" in terminal
    assert '"coire_retry_after": 1' in terminal
    await proxy.close_engine_client()


@pytest.mark.asyncio
async def test_closing_gateway_stream_promptly_closes_upstream() -> None:
    closed = False

    class EndlessStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
            raise AssertionError("gateway consumed upstream after client close")

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=EndlessStream())

    await proxy.close_engine_client()
    proxy._engine_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    source = cast(AsyncGenerator[bytes], proxy.stream("http://engine", {}, settings))
    assert await anext(source)
    await source.aclose()
    assert closed
    await proxy.close_engine_client()


@pytest.mark.asyncio
async def test_node_proxy_request_acquires_and_releases_memory_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    calls: list[tuple[str, object]] = []

    class Session:
        async def get(self, _model: object, _id: object) -> object:
            return SimpleNamespace(model_id=uuid.uuid4(), instance_id=None, node_id=uuid.uuid4())

        async def scalar(self, _query: object) -> object:
            return SimpleNamespace(id=reservation_id)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    async def acquire(
        _session: object, reservation: uuid.UUID, request: str, *, ttl_seconds: float
    ) -> object:
        calls.append(("acquire", reservation))
        assert request
        assert ttl_seconds == 60
        return SimpleNamespace(id=lease_id)

    async def release(_session: object, lease: uuid.UUID) -> None:
        calls.append(("release", lease))

    monkeypatch.setattr(proxy, "session_scope", sessions)
    monkeypatch.setattr(proxy, "acquire_lease", acquire)
    monkeypatch.setattr(proxy, "release_lease", release)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]

    async with proxy.request_lease(
        f"http://coire-edge-a.lab:9400/node/engines/{engine_id}/proxy", settings
    ):
        assert calls == [("acquire", reservation_id)]
    assert calls == [("acquire", reservation_id), ("release", lease_id)]


@pytest.mark.parametrize("renewal", ["expired", "unavailable"])
async def test_inference_stops_when_its_memory_lease_cannot_be_renewed(
    monkeypatch: pytest.MonkeyPatch, renewal: str
) -> None:
    lease_id = uuid.uuid4()
    released: list[uuid.UUID] = []

    class Session:
        async def get(self, model: object, identity: object) -> object:
            return SimpleNamespace(model_id=uuid.uuid4(), instance_id=None, node_id=uuid.uuid4())

        async def scalar(self, query: object) -> object:
            return SimpleNamespace(id=uuid.uuid4())

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    async def acquire(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(id=lease_id)

    async def refresh(*args: object, **kwargs: object) -> bool:
        if renewal == "unavailable":
            raise OSError("database unavailable")
        return False

    async def release(session: object, identity: uuid.UUID) -> None:
        released.append(identity)

    monkeypatch.setattr(proxy, "session_scope", sessions)
    monkeypatch.setattr(proxy, "acquire_lease", acquire)
    monkeypatch.setattr(proxy, "refresh_lease", refresh)
    monkeypatch.setattr(proxy, "release_lease", release)
    settings = Settings(_secrets_dir="/nonexistent", placement_lease_ttl_s=0.2)  # type: ignore[call-arg]
    with pytest.raises(asyncio.CancelledError):
        async with proxy.request_lease(
            f"http://coire-edge-a.lab:9400/node/engines/{uuid.uuid4()}/proxy", settings
        ):
            await asyncio.sleep(1)
    assert released == [lease_id]


async def test_sharded_request_locks_all_nodes_before_acquiring_any_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = uuid.uuid4()
    node_ids = [uuid.UUID(int=2 << 64), uuid.UUID(int=1 << 64)]
    reservations = [uuid.uuid4(), uuid.uuid4()]
    calls: list[tuple[str, object]] = []
    instance = SimpleNamespace(in_flight=0)

    class Session:
        async def get(self, model: object, identity: object) -> object:
            del model
            return instance if identity == instance_id else SimpleNamespace(instance_id=instance_id)

        async def execute(self, query: object) -> object:
            del query
            members = [
                SimpleNamespace(node_id=node, reservation_id=reservation)
                for node, reservation in zip(node_ids, reservations, strict=True)
            ]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: members))

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    async def lock(session: object, nodes: list[uuid.UUID]) -> None:
        del session
        calls.append(("lock", nodes))

    async def acquire(
        session: object, reservation: uuid.UUID, request: str, **kwargs: object
    ) -> object:
        del session, request, kwargs
        assert calls[0] == ("lock", node_ids)
        calls.append(("acquire", reservation))
        return SimpleNamespace(id=reservation)

    async def release(session: object, lease: uuid.UUID) -> None:
        del session
        calls.append(("release", lease))

    monkeypatch.setattr(proxy, "session_scope", sessions)
    monkeypatch.setattr(proxy, "lock_nodes_for_admission", lock, raising=False)
    monkeypatch.setattr(proxy, "acquire_lease", acquire)
    monkeypatch.setattr(proxy, "release_lease", release)
    async with proxy.request_lease(
        f"http://coire-edge-a.lab:9400/node/shard-groups/{uuid.uuid4()}/proxy",
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    ):
        assert calls == [("lock", node_ids), *(("acquire", item) for item in reservations)]
        assert instance.in_flight == 1
    assert instance.in_flight == 0


async def test_node_proxy_refuses_unreserved_engine_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        async def get(self, model: object, identity: object) -> object:
            return SimpleNamespace(model_id=uuid.uuid4(), instance_id=None, node_id=uuid.uuid4())

        async def scalar(self, statement: object) -> None:
            return None

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(proxy, "session_scope", sessions)
    with pytest.raises(ChatModelUnavailable):
        async with proxy.request_lease(
            f"http://coire-edge-a.lab:9400/node/engines/{uuid.uuid4()}/proxy",
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        ):
            pytest.fail("unreserved engine request reached the upstream")
