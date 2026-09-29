import uuid

import pytest

from coire_api.auth import ANONYMOUS
from coire_api.gateway.resolution import ResolvedModel
from coire_api.gateway.usage import UsageTracker
from coire_core.models.gateway import GatewayProtocol, UsageOutcome
from coire_core.models.registry import EngineBackend


@pytest.mark.asyncio
async def test_tracker_finalizes_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def persist(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist)
    tracker = UsageTracker(ANONYMOUS, str(uuid.uuid4()), GatewayProtocol.OPENAI)
    await tracker.finish(UsageOutcome.SUCCEEDED)
    await tracker.finish(UsageOutcome.FAILED, failure_code="late")
    assert len(calls) == 1
    assert calls[0]["outcome"] is UsageOutcome.SUCCEEDED


@pytest.mark.asyncio
async def test_tracker_preserves_refused_requested_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def persist(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist)
    tracker = UsageTracker(ANONYMOUS, "unknown", GatewayProtocol.ANTHROPIC)
    await tracker.finish(UsageOutcome.REFUSED, failure_code="model_not_found")
    assert calls[0]["requested_model_id"] == "unknown"
    assert calls[0]["model_id"] is None


@pytest.mark.asyncio
async def test_vision_outcome_metric_is_once_only_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.gateway import usage

    labels: list[dict[str, str]] = []

    class Counter:
        def add(self, _value: int, attributes: dict[str, str]) -> None:
            labels.append(attributes)

    async def persist(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(usage, "vision_requests_total", Counter())
    monkeypatch.setattr(usage, "persist_usage", persist)
    model_id = uuid.uuid4()
    tracker = UsageTracker(ANONYMOUS, str(model_id), GatewayProtocol.OPENAI)
    tracker.bind_resolution(
        ResolvedModel(model_id, "private-slug", 4096, None, None, None, None, EngineBackend.MLX_VLM)
    )
    await tracker.finish(UsageOutcome.SUCCEEDED)
    await tracker.finish(UsageOutcome.FAILED, failure_code="late")
    assert labels == [{"outcome": "succeeded"}]
