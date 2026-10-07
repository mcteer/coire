"""Real Postgres exact-instance leases and persisted first-content latency evidence."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_training_measurement_transactions import measurement_db as measurement_db

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelVariantRow,
    NodeRow,
    RequestLeaseRow,
    TrainingCommandRow,
    TrainingMeasurementRow,
    UsageRecordRow,
    UserRow,
)
from coire_api.gateway import proxy, usage
from coire_api.gateway.resolution import ModelNotFoundError, resolve_model
from coire_api.training.gateway_measurements import gateway_measurement_generate
from coire_api.training.measurements import freeze_measurement_inputs
from coire_api.training.service import payload_digest
from coire_core.errors import TrainingConflict, TrainingForbidden
from coire_core.models.adapters import InferenceTarget
from coire_core.models.auth import UserRole
from coire_core.models.engine import EngineState
from coire_core.models.gateway import GatewayProtocol, UsageOutcome
from coire_core.models.placement import MemoryReservationState
from coire_core.models.training import (
    TrainingMeasurementPrompt,
    TrainingMeasurementPromptSet,
    TrainingMeasurementRequest,
    TrainingResidentTarget,
)
from coire_core.settings import Settings
from coire_scheduler.training_guard import instance_latency_reason
from coire_scheduler.training_measurements import admit_measurement

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="requires disposable Postgres 17",
    ),
]


async def test_exact_gateway_stream_persists_thirty_fresh_samples_and_once_only_usage(
    measurement_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = measurement_db

    @asynccontextmanager
    async def session_scope():  # type: ignore[no-untyped-def]
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr(proxy, "session_scope", session_scope)
    monkeypatch.setattr(usage, "session_scope", session_scope)
    monkeypatch.setattr("coire_api.training.gateway_measurements.session_scope", session_scope)
    async with factory.begin() as session:
        row = await session.scalar(select(TrainingMeasurementRow))
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        assert row is not None and node is not None
        principal = Principal(
            kind=PrincipalKind.USER, user_id=row.owner_user_id, role=UserRole.ADMIN
        )
        request = TrainingMeasurementRequest.model_validate(row.request)
        variant = await session.get(ModelVariantRow, request.spec.model.variant_id)
        assert variant is not None
        variant.validated = True
        dispatch = await admit_measurement(
            session, row, await freeze_measurement_inputs(session, request)
        )
        assert dispatch is not None
        # Build a synthetic pre-admitted coexistence fixture without bypassing the
        # production reverse gate: resident allocation precedes the training hold.
        training_hold = await session.get(
            MemoryReservationRow, dispatch.commands[0].prepare.reservation_id
        )
        assert training_hold is not None
        training_hold.state = MemoryReservationState.RELEASED
        await session.flush()
        identity = row.id
        instance = ModelInstanceRow(
            id=uuid.uuid4(),
            model_id=request.spec.model.model_id,
            variant_id=request.spec.model.variant_id,
            policy="single:coire-edge-a",
            state="ready",
        )
        session.add(instance)
        await session.flush()
        engine = EngineProcessRow(
            id=uuid.uuid4(),
            model_id=instance.model_id,
            variant_id=instance.variant_id,
            instance_id=instance.id,
            node_id=node.id,
            port=12345,
            pid=123,
            process_create_time=1,
            state="ready",
            estimate_bytes=100,
        )
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node.id,
            holder_type="model",
            holder_id=str(instance.id),
            bytes=100,
            state="held",
            pinned=True,
        )
        session.add_all([engine, hold])
        await session.flush()
        session.add(
            InstanceMemberRow(
                instance_id=instance.id,
                node_id=node.id,
                rank=0,
                engine_id=engine.id,
                reservation_id=hold.id,
                host=node.name,
                port=12345,
                # Single-node placement never sets the sharded health flag.
                rank_healthy=False,
            )
        )
        target = TrainingResidentTarget(
            instance_id=instance.id,
            target=InferenceTarget(
                model_id=instance.model_id,
                variant_id=request.spec.model.variant_id,
                base_manifest_sha256="a" * 64,
            ),
        )
        decoy = ModelInstanceRow(
            id=uuid.uuid4(),
            model_id=instance.model_id,
            variant_id=instance.variant_id,
            policy="single:coire-edge-a",
            state="ready",
        )
        session.add(decoy)
        await session.flush()
        decoy_engine = EngineProcessRow(
            id=uuid.uuid4(),
            model_id=decoy.model_id,
            variant_id=decoy.variant_id,
            instance_id=decoy.id,
            node_id=node.id,
            port=12346,
            pid=124,
            process_create_time=1,
            state="ready",
            estimate_bytes=100,
        )
        decoy_hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node.id,
            holder_type="model",
            holder_id=str(decoy.id),
            bytes=100,
            state="held",
            pinned=True,
        )
        session.add_all([decoy_engine, decoy_hold])
        await session.flush()
        session.add(
            InstanceMemberRow(
                instance_id=decoy.id,
                node_id=node.id,
                rank=0,
                engine_id=decoy_engine.id,
                reservation_id=decoy_hold.id,
                host=node.name,
                port=12346,
                rank_healthy=True,
            )
        )
        decoy_target = TrainingResidentTarget(instance_id=decoy.id, target=target.target)
        prompt = TrainingMeasurementPrompt(
            text="synthetic CPU transport fixture",
            input_tokens=4000,
            tokens_by_instance={instance.id: 4000, decoy.id: 4000},
        )
        prompts = TrainingMeasurementPromptSet(prompts=[prompt])
        request.mode = "coexistence"
        request.resident_targets = [target, decoy_target]
        request.workload.sha256 = prompts.canonical_sha256()
        row.request = request.model_dump(mode="json")
        for probe in dispatch.commands:
            probe.mode = "coexistence"
            probe.resident_targets = request.resident_targets
            probe.resident_engine_ids = {instance.id: engine.id, decoy.id: decoy_engine.id}
        session.add(
            TrainingCommandRow(
                actor_user_id=row.owner_user_id,
                operation="training.measurement",
                subject_id=str(row.id),
                idempotency_key="synthetic-gateway",
                request_sha256=payload_digest(request),
                state="dispatching",
                payload={
                    "principal": principal.model_dump(mode="json"),
                    "prompts": prompts.model_dump(mode="json"),
                    "dispatch": dispatch.model_dump(mode="json"),
                },
            )
        )
        endpoint = f"/node/engines/{engine.id}/proxy/v1/chat/completions"
        await session.flush()
        training_hold.state = MemoryReservationState.HELD
    calls = 0

    def upstream(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert http_request.url.path == endpoint
        assert http_request.headers["authorization"] == "Bearer fixture-token"
        return httpx.Response(
            200,
            text=(
                ': keepalive\n\ndata: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"discard me"}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":4000,"completion_tokens":2}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    settings = Settings(
        training_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}')
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(proxy, "_engine_client", client)
        generate = gateway_measurement_generate(settings)
        for _ in range(29):
            result = await generate(principal, identity, target, prompt, 16)
            assert result.instance_id == target.instance_id and result.target == target.target
            assert result.input_tokens == 4000 and result.output_tokens == 2
            assert result.first_token_seconds >= 0
        async with factory.begin() as session:
            assert await instance_latency_reason(session, target) == "insufficient_samples"
        await generate(principal, identity, target, prompt, 16)
        async with factory.begin() as session:
            assert await instance_latency_reason(session, target) is None
            assert await instance_latency_reason(session, decoy_target) == "insufficient_samples"
            rows = list(await session.scalars(select(UsageRecordRow)))
            assert len(rows) == 30
            assert all(
                r.instance_id == target.instance_id
                and r.first_token_at is not None
                and r.first_token_duration_ms is not None
                and r.outcome is UsageOutcome.SUCCEEDED
                for r in rows
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RequestLeaseRow)
                    .where(RequestLeaseRow.released_at.is_(None))
                )
                == 0
            )
            newest = max(r.first_token_at for r in rows if r.first_token_at is not None)
            assert (
                await instance_latency_reason(session, target, now=newest + timedelta(seconds=60))
                is None
            )
            assert (
                await instance_latency_reason(
                    session, target, now=newest + timedelta(seconds=60, microseconds=1)
                )
                == "insufficient_samples"
            )
            for record in rows:
                record.first_token_duration_ms = 1500
            await session.flush()
            assert await instance_latency_reason(session, target) is None
            rows[0].first_token_duration_ms = 1501
            rows[1].first_token_duration_ms = 1501
            await session.flush()
            assert await instance_latency_reason(session, target) == "latency_breach"
            with pytest.raises(ModelNotFoundError):
                await resolve_model(
                    session,
                    target.target.model_id,
                    principal,
                    variant_id=target.target.variant_id,
                    instance_id=uuid.uuid4(),
                )
        with pytest.raises(TrainingConflict):
            await generate(
                principal, identity, target, prompt.model_copy(update={"text": "unfrozen"}), 16
            )
        assert calls == 30
        for field, value in (
            ("state", EngineState.STOPPED),
            ("instance_id", decoy.id),
            ("port", 54321),
        ):
            async with factory.begin() as session:
                owned_engine = await session.get(EngineProcessRow, engine.id)
                assert owned_engine is not None
                previous = getattr(owned_engine, field)
                setattr(owned_engine, field, value)
            with pytest.raises(TrainingConflict, match="resident engine changed"):
                await generate(principal, identity, target, prompt, 16)
            assert calls == 30
            async with factory.begin() as session:
                owned_engine = await session.get(EngineProcessRow, engine.id)
                assert owned_engine is not None
                setattr(owned_engine, field, previous)
        async with factory.begin() as session:
            user = await session.get(UserRow, principal.user_id)
            assert user is not None
            user.active = False
        with pytest.raises(TrainingForbidden):
            await generate(principal, identity, target, prompt, 16)
        assert calls == 30
    # Database uniqueness, including concurrent callers, guards settlement as well as insertion.
    request_id = uuid.uuid4()
    kwargs = {
        "request_id": request_id,
        "principal": principal,
        "requested_model_id": str(target.target.model_id),
        "model_id": target.target.model_id,
        "engine_id": None,
        "instance_id": target.instance_id,
        "protocol": GatewayProtocol.OPENAI,
        "prompt_tokens": 4000,
        "completion_tokens": 2,
        "started_at": datetime.now(UTC),
        "outcome": UsageOutcome.SUCCEEDED,
    }
    await asyncio.gather(usage.persist_usage(**kwargs), usage.persist_usage(**kwargs))  # type: ignore[arg-type]
    async with factory.begin() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(UsageRecordRow)
                .where(UsageRecordRow.request_id == request_id)
            )
            == 1
        )
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def delayed_write():  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr(usage, "session_scope", delayed_write)
    cancelled_id = uuid.uuid4()
    task = asyncio.create_task(usage.persist_usage(**{**kwargs, "request_id": cancelled_id}))  # type: ignore[arg-type]
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with factory.begin() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(UsageRecordRow)
                .where(UsageRecordRow.request_id == cancelled_id)
            )
            == 1
        )
