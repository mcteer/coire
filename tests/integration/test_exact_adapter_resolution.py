"""Actual Postgres target isolation; simulated residency is not engine acceptance."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.auth import ADMIN, Principal, PrincipalKind
from coire_api.db import (
    Base,
    EngineProcessRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    TrainingAdapterRow,
    UserRow,
    VariantCopyRow,
)
from coire_api.gateway.resolution import resolve_model
from coire_api.gateway.targets import ModelNotFoundError, resolve_target

pytestmark = pytest.mark.integration


@pytest.fixture
async def target_session(training_postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(training_postgres_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


async def seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    model, variant, node, owner = [uuid.uuid4() for _ in range(4)]
    await session.execute(
        insert(UserRow).values(
            id=owner, email=f"{owner}@test.invalid", display_name="Admin", role="admin", active=True
        )
    )
    await session.execute(
        insert(ModelRow).values(
            id=model,
            slug=f"base-{model}",
            repo_id="fixture/base",
            display_name="Base",
            state="ready",
            visibility="published",
            placement_policy="single:auto",
            memory_estimate_bytes=1024,
            idle_ttl_seconds=900,
            precision="bf16",
            weight_bytes=1024,
            total_bytes=1024,
            file_count=2,
            entitlement=["base-access"],
        )
    )
    await session.execute(
        insert(ModelVariantRow).values(
            id=variant,
            model_id=model,
            name="bf16",
            slug=f"variant-{variant}",
            source_revision="offline",
            precision="bf16",
            state="ready",
            validated=True,
            is_default=True,
            harness_verified=True,
        )
    )
    await session.execute(
        insert(NodeRow).values(
            id=node,
            name="coire-edge-a",
            role="studio",
            memory_total_bytes=4096,
            disk_total_bytes=4096,
            agent_version="test",
            reachability="healthy",
        )
    )
    await session.execute(
        insert(VariantCopyRow).values(
            variant_id=variant,
            node_id=node,
            path="registry-base-slug",
            verified=True,
            manifest_sha256="a" * 64,
            role="origin",
        )
    )
    job = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    await session.execute(
        text(
            "INSERT INTO training_jobs (id,owner_user_id,model_id,base_variant_id,idempotency_key,output_slug,source_yaml,source_sha256,intent_sha256,submitted_spec,state,queue_deadline_at,execution_deadline_at) VALUES (:id,:owner,:model,:variant,'exact-test','output','{}',:hash,:hash,'{}','queued',:deadline,:deadline)"
        ),
        {
            "id": job,
            "owner": owner,
            "model": model,
            "variant": variant,
            "hash": "a" * 64,
            "deadline": datetime.now(UTC) + timedelta(hours=1),
        },
    )
    adapters = [uuid.uuid4(), uuid.uuid4()]
    for index, adapter in enumerate(adapters):
        await session.execute(
            insert(TrainingAdapterRow).values(
                id=adapter,
                model_id=model,
                base_variant_id=variant,
                source_job_id=job,
                slug=f"adapter-{index}",
                selector=f"{model}@adapter-{index}",
                base_manifest_sha256="a" * 64,
                manifest_sha256="b" * 64,
                resolved_spec_sha256="c" * 64,
                parameterization="lora",
                state="ready",
                visibility="published" if index == 0 else "admin_only",
                required_entitlements=["adapter-access"],
            )
        )
    engine_adapters: list[uuid.UUID | None] = [None, *adapters]
    for engine_adapter in engine_adapters:
        instance, engine = uuid.uuid4(), uuid.uuid4()
        await session.execute(
            insert(ModelInstanceRow).values(
                id=instance,
                model_id=model,
                variant_id=variant,
                adapter_id=engine_adapter,
                state="ready",
                policy="single:auto",
            )
        )
        await session.execute(
            insert(EngineProcessRow).values(
                id=engine,
                model_id=model,
                variant_id=variant,
                adapter_id=engine_adapter,
                instance_id=instance,
                node_id=node,
                port=9500,
                state="ready",
            )
        )
    await session.commit()
    return model, variant, adapters


async def test_base_and_two_pairs_choose_distinct_engines_and_frozen_variant(
    target_session: AsyncSession,
) -> None:
    model, variant, adapters = await seed(target_session)
    selectors: list[uuid.UUID | str] = [model, f"{model}@adapter-0", f"{model}@adapter-1"]
    selected = [await resolve_model(target_session, selector, ADMIN) for selector in selectors]
    assert len({item.engine_id for item in selected}) == 3
    assert [item.target.adapter_id for item in selected if item.target] == [None, *adapters]
    assert all(item.target and item.target.variant_id == variant for item in selected)
    adapter_row = await target_session.get(TrainingAdapterRow, adapters[0])
    assert adapter_row is not None and not adapter_row.verified
    with pytest.raises(ModelNotFoundError):
        await resolve_target(target_session, f"{model}@adapter-0", ADMIN, uuid.uuid4())


async def test_visibility_union_and_private_admin_selection(target_session: AsyncSession) -> None:
    model, _, _ = await seed(target_session)
    for entitlements in [frozenset(), frozenset({"base-access"}), frozenset({"adapter-access"})]:
        user = Principal(kind=PrincipalKind.USER, entitlements=entitlements)
        with pytest.raises(ModelNotFoundError):
            await resolve_target(target_session, f"{model}@adapter-0", user)
    user = Principal(
        kind=PrincipalKind.USER, entitlements=frozenset({"base-access", "adapter-access"})
    )
    assert (await resolve_target(target_session, f"{model}@adapter-0", user)).adapter
    with pytest.raises(ModelNotFoundError):
        await resolve_target(target_session, f"{model}@adapter-1", user)
    assert (await resolve_target(target_session, f"{model}@adapter-1", ADMIN)).adapter


async def test_failed_pair_never_substitutes_warm_base(target_session: AsyncSession) -> None:
    model, _, adapters = await seed(target_session)
    from sqlalchemy import update

    await target_session.execute(
        update(EngineProcessRow)
        .where(EngineProcessRow.adapter_id == adapters[0])
        .values(state="failed")
    )
    await target_session.commit()
    pair = await resolve_model(target_session, f"{model}@adapter-0", ADMIN)
    assert pair.engine_url is None and pair.target and pair.target.adapter_id == adapters[0]
    assert (await resolve_model(target_session, model, ADMIN)).engine_url is not None


async def test_warm_adapters_do_not_warm_base_picker_or_compatible_list(
    target_session: AsyncSession,
) -> None:
    from sqlalchemy import update

    from coire_api.chat.service import picker
    from coire_api.routes.v1 import list_models
    from coire_core.settings import Settings

    model, _, _ = await seed(target_session)
    await target_session.execute(
        update(EngineProcessRow).where(EngineProcessRow.adapter_id.is_(None)).values(state="failed")
    )
    await target_session.commit()
    user = Principal(
        kind=PrincipalKind.USER, entitlements=frozenset({"base-access", "adapter-access"})
    )
    native = await picker(target_session, user)
    compatible = await list_models(user, target_session, Settings())
    assert [entry.id for entry in native.data] == [model]
    assert native.data[0].load_state.value == "cold"
    assert compatible.data[0].coire_load_state == "cold"


async def test_once_only_usage_persists_actual_pair_identity(
    target_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager

    from sqlalchemy import select

    from coire_api.db import UsageRecordRow
    from coire_api.gateway import usage
    from coire_core.models.gateway import GatewayProtocol, UsageOutcome

    model, variant, adapters = await seed(target_session)
    resolved = await resolve_model(target_session, f"{model}@adapter-0", ADMIN)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(target_session.bind) as session:
            yield session
            await session.commit()

    monkeypatch.setattr(usage, "session_scope", scope)
    tracker = usage.UsageTracker(ADMIN, f"{model}@adapter-0", GatewayProtocol.OPENAI)
    tracker.bind_resolution(resolved)
    tracker.prompt_tokens, tracker.completion_tokens = 4, 2
    await tracker.finish(UsageOutcome.SUCCEEDED)
    await tracker.finish(UsageOutcome.SUCCEEDED)
    records = (await target_session.scalars(select(UsageRecordRow))).all()
    assert len(records) == 1
    assert (records[0].model_id, records[0].variant_id, records[0].adapter_id) == (
        model,
        variant,
        adapters[0],
    )
    assert records[0].engine_id == resolved.engine_id


async def test_exact_run_scope_checks_parent_uuid_and_private_owner_without_admin_authority(
    target_session: AsyncSession,
) -> None:
    from sqlalchemy import select

    model, variant, _ = await seed(target_session)
    selected = await resolve_target(target_session, f"{model}@adapter-1", ADMIN)
    assert selected.identity is not None
    owner = await target_session.scalar(select(UserRow))
    assert owner is not None
    principal = Principal(
        kind=PrincipalKind.RUN,
        user_id=owner.id,
        permitted_model_ids=frozenset({model}),
        permitted_targets=(selected.identity,),
        scopes=frozenset({"chat"}),
    )
    assert not principal.is_admin
    assert (
        await resolve_target(target_session, f"{model}@adapter-1", principal)
    ).identity == selected.identity
    denied_selectors: list[uuid.UUID | str] = [model, f"{model}@adapter-0"]
    for selector in denied_selectors:
        with pytest.raises(ModelNotFoundError):
            await resolve_target(target_session, selector, principal)
    stale = principal.model_copy(
        update={
            "permitted_targets": (
                selected.identity.model_copy(update={"adapter_manifest_sha256": "d" * 64}),
            )
        }
    )
    with pytest.raises(ModelNotFoundError):
        await resolve_target(target_session, f"{model}@adapter-1", stale)
    assert (
        await resolve_model(target_session, f"{model}@adapter-1", principal, variant_id=variant)
    ).engine_id


async def test_parent_uuid_cannot_substitute_another_granted_variant(
    target_session: AsyncSession,
) -> None:
    model, variant, _ = await seed(target_session)
    selected = await resolve_target(target_session, model, ADMIN)
    assert selected.identity is not None
    grant = selected.identity.model_copy(update={"variant_id": uuid.uuid4()})
    principal = Principal(
        kind=PrincipalKind.RUN,
        permitted_model_ids=frozenset({model}),
        permitted_targets=(grant,),
        entitlements=frozenset({"base-access"}),
    )
    with pytest.raises(ModelNotFoundError):
        await resolve_target(target_session, model, principal)
    principal = principal.model_copy(update={"permitted_targets": (selected.identity,)})
    assert (await resolve_target(target_session, model, principal)).identity == selected.identity
    assert (
        await resolve_target(target_session, model, principal, variant)
    ).identity == selected.identity


async def test_public_pair_and_exact_variant_payloads_and_listing(
    target_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager

    from starlette.requests import Request

    from coire_api.gateway import usage
    from coire_api.routes import v1
    from coire_core.models.gateway import (
        AnthropicMessage,
        AnthropicMessagesRequest,
        ChatCompletionRequest,
        ChatMessage,
    )
    from coire_core.settings import Settings

    model, variant, _ = await seed(target_session)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(target_session.bind) as session:
            yield session
            await session.commit()

    payloads: list[dict[str, object]] = []

    async def complete(
        url: str, payload: dict[str, object], settings: Settings
    ) -> dict[str, object]:
        payloads.append(payload)
        return {
            "id": "response",
            "choices": [
                {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(usage, "session_scope", scope)
    monkeypatch.setattr(v1, "complete", complete)
    selector = f"{model}@adapter-0"
    request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions"})
    result = await v1.chat_completions(
        ChatCompletionRequest(
            model=selector,
            coire_variant_id=variant,
            messages=[ChatMessage(role="user", content="hi")],
        ),
        request,
        ADMIN,
        target_session,
        Settings(),
    )
    assert isinstance(result, dict) and result["model"] == selector
    result = await v1.anthropic_messages(
        AnthropicMessagesRequest(
            model=selector,
            coire_variant_id=variant,
            max_tokens=4,
            messages=[AnthropicMessage(role="user", content="hi")],
        ),
        request,
        ADMIN,
        target_session,
        Settings(),
    )
    assert isinstance(result, dict) and result["model"] == selector
    assert len(payloads) == 2 and all("coire_variant_id" not in payload for payload in payloads)
    assert all(payload["model"] == "registry-base-slug" for payload in payloads)
    listing = await v1.list_models(ADMIN, target_session, Settings())
    assert {str(item.id) for item in listing.data} == {str(model), selector, f"{model}@adapter-1"}


async def test_instance_creation_deduplicates_exact_pairs_and_rejects_manifest_substitution(
    target_session: AsyncSession,
) -> None:
    from fastapi import HTTPException

    from coire_api.routes.instances import create_instance
    from coire_core.models.instance import InstanceCreate

    model, variant, adapters = await seed(target_session)
    pair = (await resolve_target(target_session, f"{model}@adapter-0", ADMIN)).identity
    assert pair is not None
    request = InstanceCreate(model_id=model, variant_id=variant, target=pair)
    first = await create_instance(request, ADMIN, target_session)
    duplicate = await create_instance(request, ADMIN, target_session)
    second = await create_instance(
        InstanceCreate(model_id=model, variant_id=variant, adapter_id=adapters[1]),
        ADMIN,
        target_session,
    )
    base = await create_instance(
        InstanceCreate(model_id=model, variant_id=variant), ADMIN, target_session
    )
    assert first.id == duplicate.id
    assert len({first.id, second.id, base.id}) == 3
    assert first.target == pair and second.target and second.target.adapter_id == adapters[1]
    assert base.target and base.target.adapter_id is None
    with pytest.raises(HTTPException) as refusal:
        await create_instance(
            InstanceCreate(
                model_id=model,
                variant_id=variant,
                target=pair.model_copy(update={"adapter_manifest_sha256": "d" * 64}),
            ),
            ADMIN,
            target_session,
        )
    assert refusal.value.status_code == 409
