"""Live same-node chat regression withdraws approval and fences image work."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.images.quota import _QUOTA_LOCK
from coire_core.models.images import ImageJobState
from coire_scheduler import image_latency

NODE = uuid.uuid4()
JOB = "01J00000000000000000000000"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1499.999", 1499.999), ("1500.001", 1500.001), (None, None)],
)
async def test_prometheus_instant_vector_is_node_scoped_and_bounded(
    value: str | None, expected: float | None
) -> None:
    async def response(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "prometheus"
        assert 'node="coire-edge-b"' in request.url.params["query"]
        result = [] if value is None else [{"metric": {}, "value": [1.0, value]}]
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
        )

    assert (
        await image_latency.query_node_first_token_p95_ms(
            "coire-edge-b", transport=httpx.MockTransport(response)
        )
        == expected
    )


async def test_invalid_or_nonfinite_latency_cannot_authorize_a_regression() -> None:
    async def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.0, "NaN"]}]},
            },
        )

    with pytest.raises(image_latency.ImageLatencyUnavailable):
        await image_latency.query_node_first_token_p95_ms(
            "coire-edge-b", transport=httpx.MockTransport(response)
        )
    with pytest.raises(image_latency.ImageLatencyUnavailable):
        await image_latency.query_node_first_token_p95_ms('coire-edge-b"} or up')


@pytest.mark.parametrize(
    ("p95_ms", "thermal_alarm", "audit_action"),
    [
        (1500.001, False, "image.coexistence.latency_invalidated"),
        (None, True, "image.coexistence.thermal_invalidated"),
    ],
)
async def test_regression_invalidates_profile_and_requests_fenced_cancel(
    monkeypatch: pytest.MonkeyPatch,
    p95_ms: float | None,
    thermal_alarm: bool,
    audit_action: str,
) -> None:
    now = datetime.now(UTC)
    profile = SimpleNamespace(id=uuid.uuid4(), status="approved", invalidated_at=None, node_id=NODE)
    job = SimpleNamespace(
        id=JOB,
        state=ImageJobState.RUNNING,
        selected_node_id=NODE,
        cancel_requested_at=None,
        updated_at=now - timedelta(seconds=1),
        version=2,
    )
    audits: list[str] = []

    class Rows:
        def __init__(self, items: list[object]) -> None:
            self.items = items

        def all(self) -> list[object]:
            return self.items

    class Session:
        def __init__(self) -> None:
            self.queries = 0

        async def execute(self, statement: object) -> None:
            assert statement is _QUOTA_LOCK

        async def scalars(self, statement: object) -> Rows:
            self.queries += 1
            return Rows([profile] if self.queries == 1 else [job])

    session = Session()

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, session)

    async def audit(*_: object, **kwargs: object) -> None:
        audits.append(cast(str, kwargs["action"]))

    monkeypatch.setattr(image_latency, "session_scope", scope)
    monkeypatch.setattr(image_latency, "write_audit", audit)
    assert await image_latency.invalidate_regressed_node(
        NODE, p95_ms=p95_ms, thermal_alarm=thermal_alarm, now=now
    ) == (
        1,
        1,
    )
    assert profile.status == "invalidated" and profile.invalidated_at == now
    assert job.state == ImageJobState.CANCELLING
    assert job.cancel_requested_at == now and job.version == 3
    assert audits == [audit_action, "image.latency_cancel"]


async def test_below_limit_does_not_touch_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        image_latency,
        "session_scope",
        lambda: pytest.fail("healthy latency must not lock rows"),
    )
    assert await image_latency.invalidate_regressed_node(NODE, p95_ms=1500.0) == (0, 0)


async def test_monitor_queries_approved_node_then_withdraws_on_live_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[tuple[uuid.UUID, float]] = []

    class Rows:
        def all(self) -> list[tuple[uuid.UUID, str]]:
            return [(NODE, "coire-edge-b")]

    class Session:
        async def execute(self, statement: object) -> Rows:
            return Rows()

        async def get(self, model: object, identity: object) -> None:
            return None

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, Session())

    async def invalidate(node_id: uuid.UUID, *, p95_ms: float) -> tuple[int, int]:
        called.append((node_id, p95_ms))
        return 1, 1

    async def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1.0, "1500.1"]}],
                },
            },
        )

    monkeypatch.setattr(image_latency, "session_scope", scope)
    monkeypatch.setattr(image_latency, "invalidate_regressed_node", invalidate)
    await image_latency.monitor_image_latency_once(transport=httpx.MockTransport(response))
    assert called == [(NODE, 1500.1)]


async def test_monitor_failure_withdraws_approval_instead_of_keeping_unmeasured_mix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[tuple[uuid.UUID, float | None]] = []

    class Rows:
        def all(self) -> list[tuple[uuid.UUID, str]]:
            return [(NODE, "coire-edge-b")]

    class Session:
        async def execute(self, statement: object) -> Rows:
            return Rows()

        async def get(self, model: object, identity: object) -> None:
            return None

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, Session())

    async def invalidate(node_id: uuid.UUID, *, p95_ms: float | None) -> tuple[int, int]:
        called.append((node_id, p95_ms))
        return 1, 1

    async def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    monkeypatch.setattr(image_latency, "session_scope", scope)
    monkeypatch.setattr(image_latency, "invalidate_regressed_node", invalidate)
    await image_latency.monitor_image_latency_once(transport=httpx.MockTransport(failed))
    assert called == [(NODE, None)]


async def test_fresh_thermal_alarm_withdraws_approval_before_latency_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[uuid.UUID, bool]] = []

    class Rows:
        def all(self) -> list[tuple[uuid.UUID, str]]:
            return [(NODE, "coire-edge-b")]

    class Session:
        async def execute(self, statement: object) -> Rows:
            return Rows()

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, Session())

    async def thermal(session: object, node_id: uuid.UUID, now: datetime) -> bool:
        assert node_id == NODE
        return True

    async def invalidate(
        node_id: uuid.UUID, *, p95_ms: float | None, thermal_alarm: bool
    ) -> tuple[int, int]:
        assert p95_ms is None
        calls.append((node_id, thermal_alarm))
        return 1, 1

    async def no_metrics(request: httpx.Request) -> httpx.Response:
        pytest.fail("thermal alarm must stop mixed dispatch before querying latency")

    monkeypatch.setattr(image_latency, "session_scope", scope)
    monkeypatch.setattr(image_latency, "node_thermal_alarm", thermal)
    monkeypatch.setattr(image_latency, "invalidate_regressed_node", invalidate)
    await image_latency.monitor_image_latency_once(transport=httpx.MockTransport(no_metrics))
    assert calls == [(NODE, True)]
