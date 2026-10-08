"""Evaluation contracts reject forged, ambiguous and unbounded execution inputs."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from coire_core.models.adapters import InferenceTarget
from coire_core.models.evaluation import (
    EvaluationGatewayResponse,
    EvaluationGeneration,
    EvaluationIdentityRequest,
    EvaluationSubmission,
    EvaluationSuiteRegistration,
    JudgePairwiseOutput,
    JudgeRubricOutput,
)
from coire_core.models.registry import EngineBackend


def test_engine_identity_preserves_historical_text_wire_and_binds_visual_backend() -> None:
    old = {
        "target": {
            "model_id": str(uuid.uuid4()),
            "variant_id": str(uuid.uuid4()),
            "base_manifest_sha256": "a" * 64,
            "adapter_id": None,
            "adapter_manifest_sha256": None,
        },
        "variant_slug": "fixture",
        "template_override": None,
        "capability_profile": {},
    }
    historical = EvaluationIdentityRequest.model_validate(old)
    rendered = historical.model_dump(mode="json")
    assert "engine_backend" not in rendered
    assert historical.engine_backend is EngineBackend.MLX_LM
    visual = EvaluationIdentityRequest.model_validate({**old, "engine_backend": "mlx_vlm"})
    assert visual.model_dump(mode="json")["engine_backend"] == "mlx_vlm"
    assert EvaluationIdentityRequest.model_validate(rendered).model_dump(mode="json") == rendered
    with pytest.raises(ValidationError):
        EvaluationIdentityRequest.model_validate({**old, "engine_backend": "mflux"})


def test_bare_engine_response_accepts_bounded_cache_and_reasoning_metadata() -> None:
    message: dict[str, object] = {
        "role": "assistant",
        "content": "answer",
        "reasoning": "private reasoning",
        "reasoning_content": "private reasoning",
        "tool_call_id": None,
        "name": None,
    }
    response = {
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
            "prompt_tokens_details": {"cached_tokens": 8},
        },
    }
    parsed = EvaluationGatewayResponse.model_validate(response)
    assert parsed.choices[0].message.content == "answer"
    assert parsed.usage.prompt_tokens == 12 and parsed.usage.completion_tokens == 4
    for extra in (
        {"tool_calls": [{"name": "execute"}]},
        {"reasoning": "x" * 65537},
        {"reasoning_content": "x" * 65537},
        {"tool_call_id": "execute"},
        {"name": "execute"},
        {"unknown_metadata": None},
    ):
        with pytest.raises(ValidationError):
            EvaluationGatewayResponse.model_validate(
                {**response, "choices": [{"index": 0, "message": {**message, **extra}}]}
            )


def test_generation_bounds_and_unknown_engine_options() -> None:
    for payload in (
        {"temperature": float("nan")},
        {"seed": -1},
        {"max_tokens": 4097},
        {"remote_code": True},
        {"stop": ["x"] * 5},
    ):
        with pytest.raises(ValidationError):
            EvaluationGeneration.model_validate(payload)


def test_suite_judge_binding_is_required_and_non_judge_cannot_bind_one() -> None:
    judge = {"model_id": str(uuid.uuid4()), "variant_id": str(uuid.uuid4())}
    base = {"suite_id": "test", "version": 1, "template_version": 1}
    with pytest.raises(ValidationError):
        EvaluationSuiteRegistration.model_validate({**base, "template_id": "judge-rubric"})
    with pytest.raises(ValidationError):
        EvaluationSuiteRegistration.model_validate(
            {**base, "template_id": "task-coding-instructions", "judge": judge}
        )
    assert (
        EvaluationSuiteRegistration.model_validate(
            {**base, "template_id": "judge-rubric", "judge": judge}
        ).judge
        is not None
    )


def test_submission_binds_unique_targets_and_accepts_no_authoritative_scores() -> None:
    subject = {"model_id": str(uuid.uuid4()), "variant_id": str(uuid.uuid4())}
    request = {"suite_id": "task", "suite_version": 1, "subjects": [subject]}
    assert EvaluationSubmission.model_validate(request).subjects[0].adapter_id is None
    changes: tuple[dict[str, object], ...] = (
        {"subjects": [subject, subject]},
        {"subjects": []},
        {"score": 1},
        {"engine_path": "/tmp/model"},
    )
    for changed in changes:
        with pytest.raises(ValidationError):
            EvaluationSubmission.model_validate({**request, **changed})


def test_judge_output_scores_are_strict_and_have_fixed_dimensions() -> None:
    good = {"correctness": 4, "instruction_adherence": 3, "clarity": 2}
    assert JudgeRubricOutput.model_validate(good).correctness == 4
    for changed in (
        {"correctness": True},
        {"correctness": 5},
        {"correctness": "4"},
        {"invented_dimension": 1},
    ):
        with pytest.raises(ValidationError):
            JudgeRubricOutput.model_validate({**good, **changed})
    with pytest.raises(ValidationError):
        JudgePairwiseOutput.model_validate({"winner": "candidate"})
    assert JudgePairwiseOutput(winner="tie").winner == "tie"


def test_exact_target_requires_adapter_artifact_pair() -> None:
    with pytest.raises(ValidationError):
        InferenceTarget(
            model_id=uuid.uuid4(),
            variant_id=uuid.uuid4(),
            adapter_id=uuid.uuid4(),
            base_manifest_sha256="a" * 64,
        )


def test_workspace_digest_version_phase_and_bounds() -> None:
    from pathlib import Path

    from coire_core.models.evaluation import (
        EvaluationWorkload,
        EvaluationWorkspacePrepare,
        canonical_digest,
    )

    request = EvaluationWorkload.model_validate_json(
        (
            Path(__file__).resolve().parents[3] / "tests/fixtures/evaluations/workload.json"
        ).read_bytes()
    )
    assert (
        EvaluationWorkspacePrepare(
            workload=request, request_sha256=canonical_digest(request)
        ).workload
        == request
    )
    with pytest.raises(ValidationError):
        EvaluationWorkspacePrepare(workload=request, request_sha256="0" * 64)
    for changed in (
        {"workload_version": 2},
        {"fence": 0},
        {"phase": "judge"},
        {"remote_plugin": "unsafe"},
        {
            "previous_outputs": [
                {
                    "case_id": "case-1",
                    "subject_index": 0,
                    "text": "x",
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                }
            ]
        },
    ):
        with pytest.raises(ValidationError):
            EvaluationWorkload.model_validate({**request.model_dump(), **changed})


def test_output_utf8_bounds_and_case_scores_are_finite() -> None:
    from coire_core.models.evaluation import EvaluationCaseScore, EvaluationOutput

    with pytest.raises(ValidationError):
        EvaluationOutput(
            case_id="case-1",
            subject_index=0,
            text="é" * 10_000,
            prompt_tokens=0,
            completion_tokens=0,
        )
    for invalid in (float("nan"), float("inf"), -0.1, 1.1):
        with pytest.raises(ValidationError):
            EvaluationCaseScore(case_id="case-1", subject_index=0, score=invalid)


def test_result_failure_has_no_numeric_aggregate_or_harness_verdict() -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    from coire_core.models.evaluation import (
        EvaluationContamination,
        EvaluationResult,
        EvaluationWorkload,
    )

    request = EvaluationWorkload.model_validate_json(
        (
            Path(__file__).resolve().parents[3] / "tests/fixtures/evaluations/workload.json"
        ).read_bytes()
    )
    payload = {
        "id": request.evaluation_id,
        "run_id": request.evaluation_id,
        "outcome": "failed",
        "reason": "model_unavailable",
        "subjects": [request.target],
        "suite": request.suite,
        "aggregates": [None],
        "contamination": EvaluationContamination(status="unavailable", reason="input_missing"),
        "started_at": datetime.now(UTC),
        "finished_at": datetime.now(UTC),
        "result_sha256": "a" * 64,
    }
    assert EvaluationResult.model_validate(payload).aggregates == [None]
    changes: tuple[dict[str, object], ...] = (
        {"aggregates": [0.5]},
        {"harness_verdict": "passed"},
        {"aggregates": []},
    )
    for changed in changes:
        with pytest.raises(ValidationError):
            EvaluationResult.model_validate({**payload, **changed})


def test_event_cannot_include_raw_content_or_invalid_cursor() -> None:
    from datetime import UTC, datetime

    from coire_core.models.evaluation import EvaluationEvent

    payload = {
        "sequence": 1,
        "evaluation_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "kind": "state",
        "state": "queued",
        "version": 1,
        "created_at": datetime.now(UTC),
    }
    assert EvaluationEvent.model_validate(payload).sequence == 1
    for changed in ({"sequence": 0}, {"prompt": "raw"}, {"score": 0.9}, {"completed_cases": 65}):
        with pytest.raises(ValidationError):
            EvaluationEvent.model_validate({**payload, **changed})
