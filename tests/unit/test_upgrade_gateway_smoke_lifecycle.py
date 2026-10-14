"""Serving evidence is returned only after owned native cleanup and usage settlement."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from upgrade_fixtures import core_plan

from coire_api import db
from coire_api.auth import Principal, PrincipalKind
from coire_api.gateway import proxy, resolution, usage
from coire_api.upgrades import authorization, executor, gateway_smoke, grants, smoke_instance
from coire_core.errors import UpgradeUnavailable
from coire_core.models.adapters import InferenceTarget
from coire_core.models.upgrades import (
    CoreNodeSmokeScope,
    CoreRuntimeSelection,
    UpgradePhase,
    UpgradeSmokeRequest,
)
from coire_core.settings import Settings


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize(
    "fault", [None, "startup", "generation", "usage", "cleanup", "tokens", "cancel"]
)
async def test_private_smoke_settles_and_cleans_before_retaining_serving_receipt(
    monkeypatch: pytest.MonkeyPatch, restored: bool, fault: str | None
) -> None:
    import asyncio

    plan = core_plan()
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR, subject="coire", scopes=frozenset({"upgrades:execute"})
    )
    identity, engine = uuid4(), uuid4()
    deadline = datetime.now(UTC) + timedelta(seconds=60)
    scope = CoreNodeSmokeScope(
        plan=plan,
        node="coire-edge-a",
        engine_id=engine,
        runtime_sha256="a" * 64,
        deadline_at=deadline,
        check="source" if restored else "gateway",
    )
    target = InferenceTarget(
        model_id=plan.model_id,
        variant_id=plan.model_variant_id,
        base_manifest_sha256=plan.model_manifest_sha256,
    )
    resident = resolution.ResolvedModel(
        plan.model_id,
        plan.model_slug,
        4096,
        "/private/verified",
        engine,
        scope.node,
        "http://studio/node/engines/owned/proxy",
        target=target,
        instance_id=identity,
    )
    observed = cast(
        CoreRuntimeSelection,
        SimpleNamespace(manifest_sha256=(plan.source if restored else plan.target).manifest_sha256),
    )
    events: list[str] = []

    @asynccontextmanager
    async def session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, object())

    async def wait(*_args: object) -> None:
        events.append("startup")
        if fault == "startup":
            raise RuntimeError("startup failed")

    async def generate(*_args: object) -> dict[str, object]:
        events.append("generation")
        if fault == "generation":
            raise RuntimeError("generation failed")
        if fault == "cancel":
            raise asyncio.CancelledError
        return {
            "id": "chatcmpl-smoke",
            "object": "chat.completion",
            "created": 1,
            "model": plan.model_slug,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "private generated text"},
                    "finish_reason": "length",
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 9 if fault == "tokens" else 64,
                "total_tokens": 21 if fault == "tokens" else 76,
            },
        }

    async def settle(*_args: object, **_kwargs: object) -> None:
        events.append("usage")
        if fault == "usage":
            raise RuntimeError("usage failed")

    async def cleanup(*_args: object) -> None:
        events.append("cleanup")
        if fault == "cleanup":
            raise RuntimeError("cleanup failed")

    async def retain(*_args: object) -> None:
        events.append("retain")
        assert events[-2] == "cleanup"

    monkeypatch.setattr(db, "session_scope", session)
    monkeypatch.setattr(
        executor,
        "scoped_operation",
        AsyncMock(
            return_value=(
                SimpleNamespace(
                    payload=plan.operation.model_copy(
                        update={
                            "phase": UpgradePhase.VERIFYING_SOURCE
                            if restored
                            else UpgradePhase.VERIFYING
                        }
                    ).model_dump(mode="json")
                ),
                plan,
            )
        ),
    )
    monkeypatch.setattr(grants, "initiating_actor", lambda _row: principal)
    monkeypatch.setattr(authorization, "authorize", AsyncMock())
    monkeypatch.setattr(gateway_smoke, "verify_smoke_runtime", lambda *_args, **_kwargs: observed)
    monkeypatch.setattr(gateway_smoke, "smoke_deadline", lambda *_args: deadline)
    monkeypatch.setattr(smoke_instance, "retained_smoke_result", AsyncMock(return_value=None))
    monkeypatch.setattr(smoke_instance, "cleanup_prior_smokes", AsyncMock())
    monkeypatch.setattr(
        smoke_instance, "prepare_smoke_instance", AsyncMock(return_value=(identity, scope))
    )
    monkeypatch.setattr(smoke_instance, "wait_smoke_instance", wait)
    monkeypatch.setattr(smoke_instance, "cleanup_smoke_instance", cleanup)
    monkeypatch.setattr(smoke_instance, "retain_smoke_result", retain)
    resolve = AsyncMock(return_value=resident)
    monkeypatch.setattr(resolution, "resolve_exact_target", resolve)
    monkeypatch.setattr(proxy, "complete", generate)
    monkeypatch.setattr(usage.UsageTracker, "finish", settle)
    request = UpgradeSmokeRequest(
        generation=plan.operation.generation, check="source" if restored else "gateway"
    )
    if fault:
        expected = (
            asyncio.CancelledError
            if fault == "cancel"
            else UpgradeUnavailable
            if fault == "tokens"
            else RuntimeError
        )
        with pytest.raises(expected):
            await gateway_smoke._run_gateway_smoke(
                plan.operation.job_id, request, principal, Settings()
            )
        assert "cleanup" in events and "retain" not in events
        if fault != "startup":
            assert "usage" in events
    else:
        result = await gateway_smoke._run_gateway_smoke(
            plan.operation.job_id, request, principal, Settings()
        )
        assert result.generated_tokens == 64 and result.passed
        assert "private generated text" not in result.model_dump_json()
        assert events == ["startup", "generation", "usage", "cleanup", "retain"]
        assert resolve.call_args.kwargs == {
            "instance_id": identity,
            "upgrade_job_id": plan.operation.job_id,
        }
