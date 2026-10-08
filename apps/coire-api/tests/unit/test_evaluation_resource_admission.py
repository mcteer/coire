"""Suite subject counts and self-judge refusal precede worker allocation."""

from pathlib import Path

import pytest

from coire_api.evaluation.service import validate_subjects
from coire_core.errors import EvaluationValidationError
from coire_core.models.evaluation import EvaluationWorkload

FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


def test_suite_counts_refuse_missing_pair_or_multiple_harness_subjects() -> None:
    workload = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    validate_subjects(workload.suite, [workload.target])
    harness = workload.suite.model_copy(
        update={
            "template": workload.suite.template.model_copy(
                update={"kind": "harness", "mode": "capability"}
            )
        }
    )
    with pytest.raises(EvaluationValidationError):
        validate_subjects(harness, [workload.target, workload.target])
    pair = workload.suite.model_copy(
        update={
            "template": workload.suite.template.model_copy(
                update={"kind": "judge", "mode": "pairwise"}
            )
        }
    )
    with pytest.raises(EvaluationValidationError):
        validate_subjects(pair, [workload.target])


async def test_oversized_training_scan_cannot_allocate_an_unaccounted_sandbox(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock

    from evaluation_input_fixtures import staged_workload

    from coire_api.db import EvaluationAttemptRow, EvaluationRunRow
    from coire_core.settings import Settings
    from coire_scheduler.evaluation_admission import reserve_phase

    work = staged_workload(tmp_path)
    assert work.training is not None
    expanded = work.training.mixture.model_copy(
        update={"epoch_samples": 16_000_000, "replacement": True}
    )
    work = work.model_copy(
        update={"training": work.training.model_copy(update={"mixture": expanded})}
    )
    attempt = EvaluationAttemptRow(id=work.attempt_id, workload=work.model_dump(mode="json"))
    session = AsyncMock()
    admitted = await reserve_phase(
        session,
        EvaluationRunRow(id=work.evaluation_id),
        attempt,
        work.target,
        Settings(run_default_memory_bytes=4 * 1024**3, run_max_memory_bytes=4 * 1024**3),
    )
    assert not admitted
    session.execute.assert_not_awaited()
    session.add.assert_not_called()
