"""Evaluated recipes refuse default-off admission and unsupported Studio workers early."""

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_core.errors import EvaluationDisabled, TrainingUnavailable
from coire_core.models.auth import UserRole
from coire_core.models.training import TrainingSpecV2
from coire_core.models.training_node import NodeTrainingCapabilities
from coire_core.settings import Settings

WORKLOAD_FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


def recipe() -> TrainingSpecV2:
    path = (
        Path(__file__).resolve().parents[4]
        / "tests/fixtures/evaluations/legacy_training/recipe.json"
    )
    value = json.loads(path.read_bytes())
    value["schema_version"] = 2
    value["eval"]["suites"] = [{"suite_id": "task-recovery", "suite_version": 1}]
    return TrainingSpecV2.model_validate(value)


@pytest.mark.parametrize("blocked_by", ["disabled", "old_worker"])
async def test_evaluated_recipe_refusal_precedes_catalog_or_resource_writes(
    monkeypatch: pytest.MonkeyPatch, blocked_by: str
) -> None:
    from coire_api.evaluation.training import resolve_declarations

    session = AsyncMock()
    session.add = Mock(side_effect=AssertionError("refused recipe allocated work"))
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.health.return_value = SimpleNamespace(training_capabilities=NodeTrainingCapabilities())
    monkeypatch.setattr("coire_api.evaluation.training.NodeClient", lambda settings: client)
    owner = uuid.uuid4()
    monkeypatch.setattr(
        "coire_api.evaluation.training.authorize_live_evaluation_action",
        AsyncMock(return_value=owner),
    )
    principal = Principal(kind=PrincipalKind.USER, user_id=owner, role=UserRole.ADMIN)
    with pytest.raises(EvaluationDisabled if blocked_by == "disabled" else TrainingUnavailable):
        await resolve_declarations(
            session,
            principal,
            recipe(),
            base_manifest_sha256="a" * 64,
            settings=Settings(evaluations_enabled=blocked_by != "disabled"),
        )
    session.add.assert_not_called()
    session.scalar.assert_not_awaited()


@pytest.mark.parametrize(
    "condition", ["valid", "judge_drift", "self_judge", "base_drift", "harness"]
)
async def test_declared_suites_freeze_exact_runtime_without_allocating(
    monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    from datetime import UTC, datetime

    from coire_api.evaluation import training as module
    from coire_api.evaluation.catalog import build_suite
    from coire_core.errors import EvaluationSelfJudge, EvaluationValidationError, TrainingConflict
    from coire_core.models.evaluation import (
        EvaluationSubject,
        EvaluationSuiteRegistration,
        EvaluationWorkload,
    )

    base = EvaluationWorkload.model_validate_json(WORKLOAD_FIXTURE.read_bytes()).target
    judge_model = uuid.uuid4()
    judge = base.model_copy(
        update={
            "public_selector": judge_model,
            "target": base.target.model_copy(
                update={
                    "model_id": judge_model,
                    "variant_id": uuid.uuid4(),
                    "base_manifest_sha256": "b" * 64,
                }
            ),
        }
    )
    if condition == "self_judge":
        judge = base
    owner = uuid.uuid4()
    suite = build_suite(
        EvaluationSuiteRegistration(
            suite_id="task-recovery",
            version=1,
            template_id="harness-capability" if condition == "harness" else "judge-rubric",
            judge=None
            if condition == "harness"
            else EvaluationSubject(
                model_id=judge.target.model_id,
                variant_id=judge.target.variant_id,
            ),
        ),
        owner=owner,
        judge=None if condition == "harness" else judge,
        now=datetime.now(UTC),
    )
    actual_judge = (
        judge.model_copy(
            update={"runtime": judge.runtime.model_copy(update={"engine_version": "changed"})}
        )
        if condition == "judge_drift"
        else judge
    )
    resolver = AsyncMock(side_effect=[base, actual_judge])
    monkeypatch.setattr(module, "resolve_evaluation_target", resolver)
    monkeypatch.setattr(module, "authorize_live_evaluation_action", AsyncMock(return_value=owner))
    monkeypatch.setattr(module, "get_row", AsyncMock(return_value=object()))
    monkeypatch.setattr(module, "project", lambda row: suite)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.health.return_value = SimpleNamespace(
        training_capabilities=NodeTrainingCapabilities(
            spec_versions=[1, 2],
            evaluation_checkpoint_ack_versions=[1],
        )
    )
    client.evaluation_capabilities.return_value = SimpleNamespace(workload_versions=[1])
    monkeypatch.setattr(module, "NodeClient", lambda settings: client)
    session = AsyncMock()
    session.add = Mock(side_effect=AssertionError("resolution allocated work"))
    principal = Principal(kind=PrincipalKind.USER, user_id=owner, role=UserRole.ADMIN)
    settings = Settings(evaluations_enabled=True)
    expected = {
        "judge_drift": TrainingConflict,
        "base_drift": TrainingConflict,
        "self_judge": EvaluationSelfJudge,
        "harness": EvaluationValidationError,
    }
    manifest = "c" * 64 if condition == "base_drift" else base.target.base_manifest_sha256
    if condition == "valid":
        resolved_base, declarations = await module.resolve_declarations(
            session, principal, recipe(), base_manifest_sha256=manifest, settings=settings
        )
        assert resolved_base == base
        assert declarations[0].suite == suite
        assert declarations[0].schedule == recipe().eval.suites[0]
        assert client.health.await_count == 2
    else:
        with pytest.raises(expected[condition]):
            await module.resolve_declarations(
                session, principal, recipe(), base_manifest_sha256=manifest, settings=settings
            )
    session.add.assert_not_called()
