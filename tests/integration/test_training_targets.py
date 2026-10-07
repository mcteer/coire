"""Exact-subject scorecards, run admission and revocation using disposable Postgres."""

from __future__ import annotations

import json

import httpx
import pytest
from integration.test_exact_adapter_resolution import seed
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_agent.gateway_model import GatewayTransport
from coire_agent.harness import Harness, UnverifiedWriteError
from coire_api import evaluations
from coire_api.auth import ADMIN
from coire_api.db import EntitlementRow, ModelRow, ModelVariantRow, TrainingAdapterRow, UserRow
from coire_api.gateway.targets import resolve_target
from coire_api.routes.admin_evaluations import evaluation_target
from coire_api.run_executor import RunCommandExecutor
from coire_api.run_tokens import InvalidRunToken, authenticate_run_token, mint_run_token
from coire_api.runs import RunConflict, create_run, run_target
from coire_core.models.adapters import InferenceTarget
from coire_core.models.harness import (
    CategoryScores,
    EvaluationVerdict,
    HarnessEvaluationSubmission,
    HarnessRunRequest,
    ProfileName,
)
from coire_core.models.runs import AgentRunCreate, RunTokenScope
from coire_core.settings import Settings

pytestmark = pytest.mark.integration
pytest_plugins = ("integration.test_exact_adapter_resolution",)


async def test_persisted_base_pass_never_verifies_either_adapter(
    target_session: AsyncSession,
) -> None:
    model_id, variant_id, adapters = await seed(target_session)
    base = (await resolve_target(target_session, model_id, ADMIN)).identity
    assert base and await evaluations.target_is_write_verified(target_session, base)
    for index, adapter_id in enumerate(adapters):
        pair = (await resolve_target(target_session, f"{model_id}@adapter-{index}", ADMIN)).identity
        assert pair and pair.adapter_id == adapter_id
        context = await evaluation_target(variant_id, ADMIN, target_session, adapter_id)
        assert context.target == pair and context.public_selector == f"{model_id}@adapter-{index}"
        assert not await evaluations.target_is_write_verified(target_session, pair)
        stale = pair.model_copy(update={"adapter_manifest_sha256": "f" * 64})
        with pytest.raises(ValueError, match="differs"):
            await evaluations.validate_target(target_session, stale)


async def test_persisted_scorecard_revocation_invalidates_only_subject_run(
    target_session: AsyncSession,
) -> None:
    model_id, variant_id, adapters = await seed(target_session)
    variant = await target_session.get(ModelVariantRow, variant_id)
    model = await target_session.get(ModelRow, model_id)
    assert variant and model
    variant.published = True
    model.tags = ["general"]
    owner = await target_session.scalar(select(UserRow.id))
    assert owner
    subject = (await resolve_target(target_session, f"{model_id}@adapter-0", ADMIN)).identity
    sibling = (await resolve_target(target_session, f"{model_id}@adapter-1", ADMIN)).identity
    assert subject and sibling
    scores = CategoryScores(tool_calling=1, structured_output=1, edit_application=1, long_context=1)
    submission = HarnessEvaluationSubmission(
        variant_id=variant_id,
        target=subject,
        scores=scores,
        verdict=EvaluationVerdict.PASSED,
        harness_version="test",
        engine_version="fake",
    )
    passed = await evaluations.record(target_session, submission)
    await target_session.commit()
    base_run = await create_run(
        target_session,
        AgentRunCreate(
            profile=ProfileName.GENERAL,
            primary_model_id=model_id,
            workspace_ref="base-fixture",
            permitted_model_ids=frozenset({model_id}),
        ),
        requester_user_id=owner,
    )
    base_scope = RunTokenScope.model_validate(base_run.token_scope)
    base_target = run_target(base_run)
    assert base_target and base_target.adapter_id is None and base_target.variant_id == variant_id
    _, base_token = await mint_run_token(target_session, base_run, base_scope, ttl_seconds=60)
    run = await create_run(
        target_session,
        AgentRunCreate(
            profile=ProfileName.GENERAL,
            primary_model_id=model_id,
            primary_target=subject,
            permitted_targets=(subject,),
            workspace_ref="fixture",
            permitted_model_ids=frozenset({model_id}),
        ),
        requester_user_id=owner,
    )
    executor = RunCommandExecutor(Settings())
    await executor._authorize_execution(target_session, run)
    _, token = await mint_run_token(
        target_session, run, RunTokenScope.model_validate(run.token_scope), ttl_seconds=60
    )
    assert (await authenticate_run_token(target_session, token)).permitted_targets == (subject,)
    failed = await evaluations.record(
        target_session, submission.model_copy(update={"verdict": EvaluationVerdict.FAILED})
    )
    await target_session.commit()
    with pytest.raises(InvalidRunToken, match="target_unverified"):
        await authenticate_run_token(target_session, token)
    assert (await authenticate_run_token(target_session, base_token)).permitted_targets == (
        base_target,
    )
    with pytest.raises(RunConflict, match="harness-verified"):
        await executor._authorize_execution(target_session, run)
    assert variant.harness_verified
    assert not await evaluations.target_is_write_verified(target_session, sibling)
    assert not await evaluations.target_is_write_verified(target_session, subject)
    assert len(await evaluations.list_for_variant(target_session, variant_id, adapters[0])) == 2
    assert await evaluations.list_for_variant(target_session, variant_id) == []
    old = await evaluations.get(target_session, passed.id)
    assert old and old.verdict is EvaluationVerdict.PASSED and old.target == subject
    adapter = await target_session.get(TrainingAdapterRow, adapters[0])
    assert adapter and adapter.evaluation_id == failed.id and not adapter.verified


async def test_independent_scorecards_revoke_only_evaluated_pair(
    target_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id, variant_id, adapters = await seed(target_session)
    variant = await target_session.get(ModelVariantRow, variant_id)
    model = await target_session.get(ModelRow, model_id)
    assert variant and model
    variant.published = True
    model.tags = ["general"]
    subject = (await resolve_target(target_session, f"{model_id}@adapter-0", ADMIN)).identity
    other = (await resolve_target(target_session, f"{model_id}@adapter-1", ADMIN)).identity
    assert subject and other
    owner = await target_session.scalar(select(UserRow.id))
    assert owner
    for entitlement in ("base-access", "adapter-access"):
        target_session.add(EntitlementRow(user_id=owner, name=entitlement, granted_by=owner))
    await target_session.flush()

    def run_request() -> AgentRunCreate:
        return AgentRunCreate(
            profile=ProfileName.GENERAL,
            primary_model_id=model_id,
            primary_target=subject,
            permitted_targets=(subject,),
            workspace_ref="fixture",
            permitted_model_ids=frozenset({model_id}),
        )

    with pytest.raises(RunConflict, match="harness-verified"):
        await create_run(target_session, run_request(), requester_user_id=owner)
    scores = CategoryScores(tool_calling=1, structured_output=1, edit_application=1, long_context=1)
    passed = await evaluations.record(
        target_session,
        HarnessEvaluationSubmission(
            variant_id=variant_id,
            target=subject,
            scores=scores,
            verdict=EvaluationVerdict.PASSED,
            harness_version="test",
            engine_version="fake",
        ),
    )
    await target_session.commit()
    assert await evaluations.target_is_write_verified(target_session, subject)
    assert not await evaluations.target_is_write_verified(target_session, other)
    assert variant.harness_verified
    run = await create_run(target_session, run_request(), requester_user_id=owner)
    assert run_target(run) == subject and run.primary_adapter_id == adapters[0]
    executor = RunCommandExecutor(Settings())
    await executor._authorize_execution(target_session, run)
    scope = RunTokenScope.model_validate(run.token_scope)
    _, token = await mint_run_token(target_session, run, scope, ttl_seconds=60)
    principal = await authenticate_run_token(target_session, token)
    assert principal.permitted_targets == (subject,)
    resolved = await resolve_target(target_session, f"{model_id}@adapter-0", principal, variant_id)
    assert resolved.identity == subject
    requests: list[dict[str, object]] = []

    def fake_engine(wire: httpx.Request) -> httpx.Response:
        requests.append(json.loads(wire.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"answer":"exact"}'}}]}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "coire_agent.gateway_model.httpx.AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(fake_engine)),
    )

    class Output(BaseModel):
        answer: str

    async def live_verified(candidate: InferenceTarget) -> bool:
        return await evaluations.target_is_write_verified(target_session, candidate)

    transport = GatewayTransport(
        gateway_url="http://gateway/v1",
        token=token,
        model_id=f"{model_id}@adapter-0",
        target=subject,
    )
    harness = Harness(transport, verify_target=live_verified)
    harness_request = HarnessRunRequest.model_validate(
        {
            "profile": "general",
            "variant_id": variant_id,
            "target": subject,
            "task_class": "write",
            "task": "Answer",
            "capability_profile": {},
            "context_window": 4096,
        }
    )
    result = await harness.run_structured(harness_request, Output)
    assert result.target == subject and requests[0]["coire_variant_id"] == str(variant_id)
    assert requests[0]["model"] == f"{model_id}@adapter-0"
    failed = await evaluations.record(
        target_session,
        HarnessEvaluationSubmission(
            variant_id=variant_id,
            target=subject,
            scores=scores,
            verdict=EvaluationVerdict.FAILED,
            harness_version="test",
            engine_version="fake",
        ),
    )
    await target_session.commit()
    assert failed.id != passed.id
    assert not await evaluations.target_is_write_verified(target_session, subject)
    with pytest.raises(InvalidRunToken, match="target_unverified"):
        await authenticate_run_token(target_session, token)
    with pytest.raises(RunConflict, match="harness-verified"):
        await executor._authorize_execution(target_session, run)
    with pytest.raises(UnverifiedWriteError):
        await harness.run_structured(harness_request, Output)
    assert len(requests) == 1
    assert variant.harness_verified
    assert not await evaluations.target_is_write_verified(target_session, other)
    assert len(await evaluations.list_for_variant(target_session, variant_id, adapters[0])) == 2
    assert await evaluations.list_for_variant(target_session, variant_id) == []
    old = await evaluations.get(target_session, passed.id)
    assert old and old.verdict is EvaluationVerdict.PASSED and old.target == subject
    with pytest.raises(RunConflict, match="harness-verified"):
        await create_run(target_session, run_request(), requester_user_id=owner)
    adapter = await target_session.get(TrainingAdapterRow, adapters[0])
    assert adapter and adapter.evaluation_id == failed.id and not adapter.verified
