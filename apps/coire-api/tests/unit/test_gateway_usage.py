import uuid

import pytest

from coire_api.auth import ANONYMOUS
from coire_api.gateway.usage import UsageTracker
from coire_core.models.gateway import GatewayProtocol, UsageOutcome


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
