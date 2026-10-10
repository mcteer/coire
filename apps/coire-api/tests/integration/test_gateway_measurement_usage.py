"""Real Postgres exact-instance leases and persisted first-content latency evidence."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_training_measurement_transactions import measurement_db as measurement_db

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    ApiKeyRow,
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    RateWindowRow,
    RequestLeaseRow,
    TrainingCommandRow,
    TrainingMeasurementRow,
    UsageAccumulatorRow,
    UsageRecordRow,
    UserRow,
    VariantCopyRow,
)
from coire_api.gateway import proxy, usage
from coire_api.gateway.resolution import ModelNotFoundError, resolve_model
from coire_api.training.gateway_measurements import gateway_measurement_generate
from coire_api.training.measurements import freeze_measurement_inputs, validate_measurement_prompts
from coire_api.training.service import payload_digest
from coire_core.errors import TrainingConflict, TrainingForbidden
from coire_core.models.acquisition import VariantState
from coire_core.models.adapters import InferenceTarget
from coire_core.models.auth import UserRole
from coire_core.models.engine import EngineState
from coire_core.models.gateway import GatewayProtocol, UsageOutcome
from coire_core.models.placement import MemoryReservationState
from coire_core.models.registry import ModelState
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


@pytest.mark.parametrize("api_key", [False, True])
async def test_exact_gateway_stream_persists_thirty_fresh_samples_and_once_only_usage(
    measurement_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    api_key: bool,
) -> None:
    factory = measurement_db
    from coire_api.identity import limits
    from coire_api.identity.windows import minute_window

    frozen_minute = minute_window()
    monkeypatch.setattr(limits, "minute_window", lambda: frozen_minute)

    @asynccontextmanager
    async def session_scope():  # type: ignore[no-untyped-def]
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr("coire_api.db.session_scope", session_scope)
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
        if api_key:
            key = ApiKeyRow(
                id=uuid.uuid4(),
                user_id=row.owner_user_id,
                name="measurement fixture",
                prefix="abcdefghijkl",
                secret_hash="synthetic-hash",
                credential_version=1,
                scopes=["admin"],
                requests_per_minute=1000,
                monthly_budget_tokens=10000000,
            )
            session.add(key)
            await session.flush()
            principal = Principal(
                kind=PrincipalKind.API_KEY,
                user_id=row.owner_user_id,
                role=UserRole.ADMIN,
                api_key_id=key.id,
                credential_version=1,
                scopes=frozenset({"admin"}),
            )
        request = TrainingMeasurementRequest.model_validate(row.request)
        variant = await session.get(ModelVariantRow, request.spec.model.variant_id)
        assert variant is not None
        variant.validated = True
        binding = await freeze_measurement_inputs(session, request)
        dispatch = await admit_measurement(session, row, binding)
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
    # Full live authority and exact resident validation should batch reads rather
    # than repeat one registry/engine inventory query per resident and stream frame.
    from coire_api.training import gateway_measurements

    original_validate = validate_measurement_prompts
    parse_checks: list[int] = []

    def count_parsing(*args):  # type: ignore[no-untyped-def]
        parse_checks.append(1)
        return original_validate(*args)

    monkeypatch.setattr(gateway_measurements, "validate_measurement_prompts", count_parsing)
    original_authorize = gateway_measurements._authorize
    authorization_checks: list[int] = []

    async def bounded_authorize(session, *args):  # type: ignore[no-untyped-def]
        assert session.bind is not None
        http_context = gateway_measurements._gateway_request.get()
        if http_context is not None and gateway_measurements._scope.get() is None:
            assert len(http_context) == 3 and session is http_context[2]
            assert session.in_transaction()
        elif http_context is not None:
            assert session is not http_context[2]
            assert session.bind is http_context[2].bind
        queries = []

        def counted(conn, cursor, statement, *args):  # type: ignore[no-untyped-def]
            queries.append(statement)

        event.listen(session.bind.sync_engine, "before_cursor_execute", counted)
        try:
            result = await original_authorize(session, *args)
        finally:
            event.remove(session.bind.sync_engine, "before_cursor_execute", counted)
        budget = (4 if not authorization_checks else 3) + int(
            session.info.pop("coire.measurement.read_retry_used", False)
        )
        if authorization_checks:
            assert "training_measurements.request," not in queries[0]
            assert "training_commands.payload," not in queries[0]
        authorization_checks.append(len(queries))
        assert len(queries) <= budget, "live measurement authority exceeded batched query budget"
        return result

    monkeypatch.setattr(gateway_measurements, "_authorize", bounded_authorize)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(proxy, "_engine_client", client)
        generate = gateway_measurement_generate(settings)
        for index in range(29):
            checks_before = len(authorization_checks)
            lease_queries: list[str] = []
            first_lease_queries: list[int] = []

            def count_lease_queries(  # type: ignore[no-untyped-def]
                conn, cursor, statement, *args, queries=lease_queries, counts=first_lease_queries
            ):
                queries.append(statement)
                if "INSERT INTO request_leases" in statement:
                    counts.append(len(queries))

            test_engine = factory.kw["bind"].sync_engine
            event.listen(test_engine, "before_cursor_execute", count_lease_queries)
            try:
                result = await generate(principal, identity, target, prompt, 16)
            finally:
                event.remove(test_engine, "before_cursor_execute", count_lease_queries)
            assert first_lease_queries and first_lease_queries[0] <= 5
            assert result.instance_id == target.instance_id and result.target == target.target
            assert result.input_tokens == 4000 and result.output_tokens == 2
            assert result.first_token_seconds >= 0
            if index == 0:
                assert len(authorization_checks) - checks_before == 6
        async with factory.begin() as session:
            assert await instance_latency_reason(session, target) == "insufficient_samples"
        await generate(principal, identity, target, prompt, 16)
        assert len(parse_checks) == 1, "identical immutable workload parsed repeatedly"
        from coire_scheduler import training_measurements
        from coire_scheduler.training_measurements import (
            MeasurementNodeClient,
            TrainingMeasurementExecutor,
        )

        monkeypatch.setattr(training_measurements, "session_scope", session_scope)
        locked_queries: list[int] = []
        locked = False

        def watcher_query(conn, cursor, statement, *args):  # type: ignore[no-untyped-def]
            nonlocal locked
            if "pg_advisory_xact_lock" in statement:
                locked = True
            if locked:
                locked_queries.append(1)

        event.listen(test_engine, "before_cursor_execute", watcher_query)
        try:
            transport = MeasurementNodeClient(settings)
            async with transport:
                executor = TrainingMeasurementExecutor(settings, transport)
                await executor.recheck(identity, principal, binding)
        finally:
            event.remove(test_engine, "before_cursor_execute", watcher_query)
        assert len(locked_queries) <= len(request.nodes) + 1
        async with factory.begin() as session:
            assert await instance_latency_reason(session, target) is None
            assert await instance_latency_reason(session, decoy_target) == "insufficient_samples"
            rows = list(await session.scalars(select(UsageRecordRow)))
            assert len(rows) == 30
            current_instance = await session.get(ModelInstanceRow, target.instance_id)
            assert current_instance is not None and current_instance.in_flight == 0
            leases = list(await session.scalars(select(RequestLeaseRow)))
            assert len(leases) == 30 and all(lease.released_at is not None for lease in leases)
            if api_key:
                consumed = await session.scalar(
                    select(UsageAccumulatorRow).where(
                        UsageAccumulatorRow.api_key_id == principal.api_key_id
                    )
                )
                assert consumed is not None
                assert (consumed.requests, consumed.prompt_tokens, consumed.completion_tokens) == (
                    30,
                    120000,
                    60,
                )
                admitted = await session.scalar(
                    select(func.sum(RateWindowRow.requests)).where(
                        RateWindowRow.api_key_id == principal.api_key_id
                    )
                )
                assert admitted == 30
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

        class ChangedWorkloadStream(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[no-untyped-def]
                yield b": keepalive\n\n"
                async with factory.begin() as session:
                    changed_command = await session.scalar(
                        select(TrainingCommandRow).where(
                            TrainingCommandRow.operation == "training.measurement",
                            TrainingCommandRow.subject_id == str(identity),
                        )
                    )
                    assert changed_command is not None
                    changed_payload = deepcopy(changed_command.payload)
                    changed_prompts = TrainingMeasurementPromptSet.model_validate(
                        changed_payload["prompts"]
                    )
                    changed_prompts.prompts[0] = changed_prompts.prompts[0].model_copy(
                        update={"text": "changed while streaming"}
                    )
                    changed_payload["prompts"] = changed_prompts.model_dump(mode="json")
                    changed_command.payload = changed_payload
                yield b'data: {"choices":[{"delta":{"content":"refuse this frame"}}]}\n\n'

        async def changed_upstream(http_request):  # type: ignore[no-untyped-def]
            return httpx.Response(200, stream=ChangedWorkloadStream())

        async with factory.begin() as session:
            original_command = await session.scalar(
                select(TrainingCommandRow).where(
                    TrainingCommandRow.operation == "training.measurement",
                    TrainingCommandRow.subject_id == str(identity),
                )
            )
            assert original_command is not None
            original_payload = deepcopy(original_command.payload)
        try:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(changed_upstream)
            ) as changed_client:
                monkeypatch.setattr(proxy, "_engine_client", changed_client)
                with pytest.raises(TrainingConflict, match="workload changed during generation"):
                    await generate(principal, identity, target, prompt, 16)
        finally:
            monkeypatch.setattr(proxy, "_engine_client", client)
            async with factory.begin() as session:
                changed_command = await session.scalar(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.operation == "training.measurement",
                        TrainingCommandRow.subject_id == str(identity),
                    )
                )
                assert changed_command is not None
                changed_command.payload = original_payload

        class ChangedRequestStream(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[no-untyped-def]
                yield b": keepalive\n\n"
                async with factory.begin() as session:
                    changed_measurement = await session.get(TrainingMeasurementRow, identity)
                    assert changed_measurement is not None
                    changed_request = deepcopy(changed_measurement.request)
                    changed_request["workload"] = {
                        **request.workload.model_dump(mode="json"),
                        "max_output_tokens": 8,
                    }
                    changed_measurement.request = changed_request
                yield b'data: {"choices":[{"delta":{"content":"refuse this frame"}}]}\n\n'

        async def changed_request_upstream(http_request):  # type: ignore[no-untyped-def]
            return httpx.Response(200, stream=ChangedRequestStream())

        try:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(changed_request_upstream)
            ) as changed_client:
                monkeypatch.setattr(proxy, "_engine_client", changed_client)
                with pytest.raises(TrainingConflict, match="workload changed during generation"):
                    await generate(principal, identity, target, prompt, 16)
        finally:
            monkeypatch.setattr(proxy, "_engine_client", client)
            async with factory.begin() as session:
                changed_measurement = await session.get(TrainingMeasurementRow, identity)
                assert changed_measurement is not None
                changed_measurement.request = request.model_dump(mode="json")

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
            copy_id = await session.scalar(
                select(VariantCopyRow.id).where(
                    VariantCopyRow.variant_id == target.target.variant_id,
                    VariantCopyRow.node_id == node.id,
                )
            )
        assert copy_id is not None
        for row_type, row_id, field, changed in (
            (ModelRow, target.target.model_id, "state", ModelState.RETIRED),
            (ModelVariantRow, target.target.variant_id, "state", VariantState.FAILED),
            (ModelVariantRow, target.target.variant_id, "validated", False),
            (VariantCopyRow, copy_id, "verified", False),
            (VariantCopyRow, copy_id, "manifest_sha256", "b" * 64),
        ):
            async with factory.begin() as session:
                changed_row = await session.get(row_type, row_id)
                assert changed_row is not None
                previous = getattr(changed_row, field)
                setattr(changed_row, field, changed)
            with pytest.raises(TrainingConflict):
                await generate(principal, identity, target, prompt, 16)
            assert calls == 30
            async with factory.begin() as session:
                changed_row = await session.get(row_type, row_id)
                assert changed_row is not None
                setattr(changed_row, field, previous)
        async with factory.begin() as session:
            # Seed an unknown preexisting occupant in the disposable fixture;
            # production admission itself refuses creating this during training.
            fixture_training_hold = await session.get(MemoryReservationRow, training_hold.id)
            assert fixture_training_hold is not None
            fixture_training_hold.state = MemoryReservationState.RELEASED
            await session.flush()
            unknown_hold = MemoryReservationRow(
                id=uuid.uuid4(),
                node_id=node.id,
                holder_type="model",
                holder_id=str(uuid.uuid4()),
                bytes=1,
                state="held",
                pinned=True,
            )
            session.add(unknown_hold)
            await session.flush()
            fixture_training_hold.state = MemoryReservationState.HELD
        with pytest.raises(TrainingConflict, match="resident set changed"):
            await generate(principal, identity, target, prompt, 16)
        assert calls == 30
        async with factory.begin() as session:
            unknown = await session.get(MemoryReservationRow, unknown_hold.id)
            assert unknown is not None
            unknown.state = MemoryReservationState.RELEASED
        async with factory.begin() as session:
            current_node = await session.get(NodeRow, node.id)
            assert current_node is not None
            original_name = current_node.name
            current_node.name = original_name + "-changed"
        with pytest.raises(TrainingConflict, match="node inventory changed"):
            await generate(principal, identity, target, prompt, 16)
        assert calls == 30
        async with factory.begin() as session:
            current_node = await session.get(NodeRow, node.id)
            assert current_node is not None
            current_node.name = original_name
        if api_key:
            from coire_api.identity.limits import MonthlyQuotaExceeded, RateLimitExceeded

            for budget, error in (
                ("rate", RateLimitExceeded),
                ("monthly", MonthlyQuotaExceeded),
                ("both", RateLimitExceeded),
            ):
                async with factory.begin() as session:
                    current_key = await session.get(ApiKeyRow, principal.api_key_id)
                    assert current_key is not None
                    leases_before = await session.scalar(
                        select(func.count()).select_from(RequestLeaseRow)
                    )
                    usage_before = await session.scalar(
                        select(func.count()).select_from(UsageRecordRow)
                    )
                    rates_before = await session.scalar(select(func.sum(RateWindowRow.requests)))
                    current_hold = await session.get(MemoryReservationRow, hold.id)
                    assert current_hold is not None
                    used_before = current_hold.last_used_at
                    if budget in {"rate", "both"}:
                        current_key.requests_per_minute = 1
                    if budget in {"monthly", "both"}:
                        current_key.monthly_budget_tokens = 120000
                with pytest.raises(error):
                    await generate(principal, identity, target, prompt, 16)
                assert calls == 30
                async with factory.begin() as session:
                    assert (
                        await session.scalar(select(func.count()).select_from(RequestLeaseRow))
                        == leases_before
                    )
                    assert (
                        await session.scalar(select(func.count()).select_from(UsageRecordRow))
                        == usage_before
                    )
                    assert (
                        await session.scalar(select(func.sum(RateWindowRow.requests)))
                        == rates_before
                    )
                    refused_instance = await session.get(ModelInstanceRow, target.instance_id)
                    refused_hold = await session.get(MemoryReservationRow, hold.id)
                    assert refused_instance is not None and refused_instance.in_flight == 0
                    assert refused_hold is not None and refused_hold.last_used_at == used_before
                    current_key = await session.get(ApiKeyRow, principal.api_key_id)
                    assert current_key is not None
                    current_key.requests_per_minute = 1000
                    current_key.monthly_budget_tokens = 10000000
            for mutation in ("revoke", "rotate", "remove_admin"):
                async with factory.begin() as session:
                    current_key = await session.get(ApiKeyRow, principal.api_key_id)
                    assert current_key is not None
                    if mutation == "revoke":
                        current_key.revoked_at = datetime.now(UTC)
                    elif mutation == "rotate":
                        current_key.credential_version += 1
                    else:
                        current_key.scopes = []
                with pytest.raises(TrainingForbidden):
                    await generate(principal, identity, target, prompt, 16)
                assert calls == 30
                async with factory.begin() as session:
                    current_key = await session.get(ApiKeyRow, principal.api_key_id)
                    assert current_key is not None
                    current_key.revoked_at = None
                    current_key.credential_version = 1
                    current_key.scopes = ["admin"]
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

    # The real gateway service must authenticate the node and preserve frozen
    # authority before the scheduler can use its measured completion metadata.
    from coire_api.app import create_app
    from coire_api.routes import internal_training

    monkeypatch.setattr(usage, "session_scope", session_scope)
    monkeypatch.setattr(internal_training, "session_scope", session_scope)
    async with factory.begin() as session:
        user = await session.get(UserRow, principal.user_id)
        assert user is not None
        user.active = True
    rpc_settings = settings.model_copy(
        update={
            "node_tokens": SecretStr(
                '{"coire-edge-a":"fixture-token","coire-edge-b":"other-node-token"}'
            )
        }
    )
    app = create_app(rpc_settings)
    app.state.training_measurement_gateway = generate
    body = {
        "principal_sha256": payload_digest(principal),
        "target": target.model_dump(mode="json"),
        "prompt": prompt.model_dump(mode="json"),
        "max_output_tokens": 16,
    }
    path = f"/api/v1/internal/training/measurements/{identity}/generate"
    headers = {"Authorization": "Bearer fixture-token", "X-Coire-Node": "coire-edge-a"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as engine_client:
        monkeypatch.setattr(proxy, "_engine_client", engine_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as rpc:
            assert (await rpc.post(path, json=body)).status_code == 401
            other_headers = {
                "Authorization": "Bearer other-node-token",
                "X-Coire-Node": "coire-edge-b",
            }
            assert (await rpc.post(path, json=body, headers=other_headers)).status_code == 404
            assert (
                await rpc.post(path, json={**body, "principal_sha256": "0" * 64}, headers=headers)
            ).status_code == 404
            assert (
                await rpc.post(path, json={**body, "report": {}}, headers=headers)
            ).status_code == 422
            wrong_prompt = {
                **body,
                "prompt": prompt.model_copy(update={"text": "unfrozen"}).model_dump(mode="json"),
            }
            assert (await rpc.post(path, json=wrong_prompt, headers=headers)).status_code == 409
            # The metadata pool must reconnect an actually terminated cached connection
            # before admission; no failed operation is replayed after that first read.
            from sqlalchemy import text

            from coire_api.training.gateway_database import MeasurementDatabase

            fixture_engine = factory.kw["bind"]
            metadata_settings = rpc_settings.model_copy(
                update={
                    "postgres_host": fixture_engine.url.host,
                    "postgres_port": fixture_engine.url.port,
                    "postgres_user": fixture_engine.url.username,
                    "postgres_db": fixture_engine.url.database,
                    "postgres_password": SecretStr(fixture_engine.url.password or ""),
                }
            )
            metadata_database = MeasurementDatabase(metadata_settings)
            app.state.training_measurement_database = metadata_database
            async with metadata_database.engine.connect() as connection:
                old_backend = await connection.scalar(text("SELECT pg_backend_pid()"))
            async with factory.begin() as session:
                assert await session.scalar(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": old_backend}
                )
            routing_queries: list[str] = []

            def routing_query(conn, cursor, statement, *args):  # type: ignore[no-untyped-def]
                routing_queries.append(statement)

            event.listen(
                metadata_database.engine.sync_engine, "before_cursor_execute", routing_query
            )
            try:
                completed = await rpc.post(path, json=body, headers=headers)
            finally:
                event.remove(
                    metadata_database.engine.sync_engine, "before_cursor_execute", routing_query
                )
            assert routing_queries and "sha256" in routing_queries[0]
            await metadata_database.close()
            app.state.training_measurement_database = None
            assert completed.status_code == 200, completed.text
            assert completed.json()["instance_id"] == str(target.instance_id)
            assert "text" not in completed.json()
            # A cached routing principal never grants authority over a changed
            # command document, even when the submitted old digest still matches.
            calls_before_mutation = calls
            async with factory.begin() as session:
                changed_command = await session.scalar(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.operation == "training.measurement",
                        TrainingCommandRow.subject_id == str(identity),
                    )
                )
                assert changed_command is not None
                changed_payload = deepcopy(changed_command.payload)
                changed_payload["principal"] = principal.model_copy(
                    update={"subject": "changed-frozen-identity"}
                ).model_dump(mode="json")
                changed_command.payload = changed_payload
            try:
                assert (await rpc.post(path, json=body, headers=headers)).status_code == 409
                assert calls == calls_before_mutation
            finally:
                async with factory.begin() as session:
                    changed_command = await session.scalar(
                        select(TrainingCommandRow).where(
                            TrainingCommandRow.operation == "training.measurement",
                            TrainingCommandRow.subject_id == str(identity),
                        )
                    )
                    assert changed_command is not None
                    changed_command.payload = original_payload
            from coire_api.training import gateway_measurement_client
            from coire_api.training.gateway_measurement_client import MeasurementGatewayClient

            monkeypatch.setattr(gateway_measurement_client, "session_scope", session_scope)
            gateway = MeasurementGatewayClient(
                rpc_settings.model_copy(update={"training_input_api_url": "http://fixture"})
            )
            await gateway.client.aclose()
            gateway.client = rpc
            metadata = await gateway.generate(principal, identity, target, prompt, 16)
            assert metadata.instance_id == target.instance_id and metadata.input_tokens == 4000

            def wrong_completion(http_request):  # type: ignore[no-untyped-def]
                assert http_request.headers["X-Coire-Node"] == "coire-edge-a"
                assert http_request.headers["Authorization"] == "Bearer fixture-token"
                return httpx.Response(
                    200, json={**completed.json(), "instance_id": str(uuid.uuid4())}
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(wrong_completion), base_url="http://fixture"
            ) as wrong_client:
                gateway.client = wrong_client
                with pytest.raises(TrainingConflict, match="different resident"):
                    await gateway.generate(principal, identity, target, prompt, 16)
            gateway.client = rpc
            gateway.settings = rpc_settings.model_copy(update={"node_tokens": SecretStr("{}")})
            with pytest.raises(TrainingConflict, match="node binding is unavailable"):
                await gateway.generate(principal, identity, target, prompt, 16)
            gateway.settings = rpc_settings
            async with factory.begin() as session:
                user = await session.get(UserRow, principal.user_id)
                assert user is not None
                user.active = False
            assert (await rpc.post(path, json=body, headers=headers)).status_code == 403
