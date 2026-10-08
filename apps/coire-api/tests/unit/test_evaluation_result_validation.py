"""A collected result must match its complete exact workload and fence."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coire_api.evaluation.validation import validate_worker_result
from coire_core.errors import EvaluationValidationError
from coire_core.evaluation_suites import cases
from coire_core.models.evaluation import (
    EvaluationCaseScore,
    EvaluationOutput,
    EvaluationWorkerResult,
    EvaluationWorkload,
    canonical_digest,
)


def test_private_worker_shape_errors_have_content_free_messages() -> None:
    from coire_api.evaluation.validation import parse_worker_result

    with pytest.raises(EvaluationValidationError) as failure:
        parse_worker_result({"private_output": "private candidate text"})
    assert "private candidate" not in str(failure.value)
    assert failure.value.__suppress_context__


def test_complete_result_and_foreign_incomplete_or_forged_evidence() -> None:
    workload = EvaluationWorkload.model_validate_json(
        (
            Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"
        ).read_bytes()
    )
    result = EvaluationWorkerResult(
        evaluation_id=workload.evaluation_id,
        attempt_id=workload.attempt_id,
        run_id=workload.run_id,
        fence=workload.fence,
        phase=workload.phase,
        request_sha256=canonical_digest(workload),
        suite_sha256=workload.suite.content_sha256,
        cases_sha256=workload.suite.template.cases_sha256,
        runtime=workload.target.runtime,
        target=workload.target.target,
        outcome="succeeded",
        outputs=[
            EvaluationOutput(
                case_id=case.id,
                subject_index=0,
                text="answer",
                prompt_tokens=5,
                completion_tokens=3,
            )
            for case in cases(workload.suite.template.template_id)
        ],
        scores=[
            EvaluationCaseScore(case_id=case.id, subject_index=0, passed=False, score=0)
            for case in cases(workload.suite.template.template_id)
        ],
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    validate_worker_result(workload, result)
    for changed in (
        result.model_copy(update={"fence": 2}),
        result.model_copy(update={"outputs": result.outputs[:-1]}),
        result.model_copy(update={"scores": result.scores[:-1]}),
        result.model_copy(update={"scores": [*result.scores[:-1], result.scores[0]]}),
        result.model_copy(
            update={"runtime": result.runtime.model_copy(update={"engine_version": "wrong"})}
        ),
        result.model_copy(
            update={
                "scores": [result.scores[0].model_copy(update={"passed": True}), *result.scores[1:]]
            }
        ),
    ):
        with pytest.raises(EvaluationValidationError):
            validate_worker_result(workload, changed)


def test_consumed_input_receipt_is_bound_to_training_sources(tmp_path: Path) -> None:
    from evaluation_input_fixtures import staged_workload

    from coire_core.models.evaluation import EvaluationContamination

    workload = staged_workload(tmp_path)
    assert workload.training is not None
    result = EvaluationWorkerResult(
        evaluation_id=workload.evaluation_id,
        attempt_id=workload.attempt_id,
        run_id=workload.run_id,
        fence=workload.fence,
        phase=workload.phase,
        request_sha256=canonical_digest(workload),
        suite_sha256=workload.suite.content_sha256,
        cases_sha256=workload.suite.template.cases_sha256,
        runtime=workload.target.runtime,
        target=workload.target.target,
        outcome="failed",
        reason="internal",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        contamination=EvaluationContamination(
            status="clean", selected_rows=1, checked_cases=16, data_sha256=["f" * 64]
        ),
    )
    with pytest.raises(EvaluationValidationError, match="input"):
        validate_worker_result(workload, result)
