from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from coire_api.db import AgentRunRow, ModelVariantRow, TrainingAdapterRow
from coire_api.evaluations import target_is_write_verified, validate_target
from coire_api.run_tokens import InvalidRunToken, validate_run_scope
from coire_core.models.adapters import InferenceTarget
from coire_core.models.runs import RunTokenScope


def subject() -> InferenceTarget:
    return InferenceTarget(
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        adapter_id=uuid.uuid4(),
        base_manifest_sha256="a" * 64,
        adapter_manifest_sha256="b" * 64,
    )


async def test_verified_base_is_not_adapter_verification() -> None:
    target = subject()
    variant = ModelVariantRow(id=target.variant_id, model_id=target.model_id, harness_verified=True)
    adapter = TrainingAdapterRow(
        id=target.adapter_id,
        model_id=target.model_id,
        base_variant_id=target.variant_id,
        base_manifest_sha256="a" * 64,
        manifest_sha256="b" * 64,
        state="ready",
        verified=False,
    )
    session = AsyncMock()

    async def get(row_type: object, identifier: object) -> object:
        return variant if row_type is ModelVariantRow else adapter

    session.get.side_effect = get
    session.scalars.return_value.all = lambda: ["a" * 64]
    assert not await target_is_write_verified(session, target)
    adapter.verified = True
    adapter.evaluation_id = uuid.uuid4()
    assert await target_is_write_verified(session, target)
    assert variant.harness_verified
    adapter.manifest_sha256 = "c" * 64
    assert not await target_is_write_verified(session, target)


async def test_parent_or_variant_substitution_is_refused_before_scorecard() -> None:
    target = subject()
    session = AsyncMock()
    session.get.return_value = ModelVariantRow(
        id=target.variant_id, model_id=uuid.uuid4(), harness_verified=True
    )
    with pytest.raises(LookupError, match="exact"):
        await validate_target(session, target)
    session.scalars.assert_not_called()


def test_token_parent_scope_does_not_authorize_an_adapter_or_another_variant() -> None:
    target = subject()
    run = AgentRunRow(
        primary_model_id=target.model_id,
        primary_variant_id=target.variant_id,
        primary_adapter_id=target.adapter_id,
    )
    legacy = RunTokenScope(permitted_model_ids=frozenset({target.model_id}), spend_limit_tokens=100)
    with pytest.raises(InvalidRunToken, match="exact"):
        validate_run_scope(run, legacy)
    exact = legacy.model_copy(update={"permitted_targets": (target,)})
    validate_run_scope(run, exact)
    for mutation in ({"adapter_id": uuid.uuid4()}, {"variant_id": uuid.uuid4()}):
        wrong = legacy.model_copy(
            update={"permitted_targets": (target.model_copy(update=mutation),)}
        )
        with pytest.raises(InvalidRunToken, match="primary exact"):
            validate_run_scope(run, wrong)
