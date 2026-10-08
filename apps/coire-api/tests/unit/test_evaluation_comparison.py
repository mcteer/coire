"""Deltas require matching execution provenance, while exact subjects may differ."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coire_core.models.evaluation import (
    EvaluationContamination,
    EvaluationResult,
    EvaluationWorkload,
)

FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


def result() -> EvaluationResult:
    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    now = datetime.now(UTC)
    return EvaluationResult(
        id=work.evaluation_id,
        run_id=work.evaluation_id,
        outcome="succeeded",
        subjects=[work.target],
        suite=work.suite,
        aggregates=[0.25],
        contamination=EvaluationContamination(
            status="not_applicable", reason="no_training_context"
        ),
        started_at=now,
        finished_at=now,
        result_sha256="f" * 64,
    )


def test_different_subjects_can_compare_and_failed_results_never_get_delta() -> None:
    from coire_api.evaluation.comparison import compare_results

    left = result()
    right = left.model_copy(
        update={
            "aggregates": [0.75],
            "subjects": [
                left.subjects[0].model_copy(
                    update={
                        "target": left.subjects[0].target.model_copy(
                            update={"variant_id": uuid.uuid4()}
                        )
                    }
                )
            ],
        }
    )
    comparison = compare_results(left, right, left_subject=0, right_subject=0)
    assert comparison.comparable and comparison.delta == 0.5
    failed = right.model_copy(
        update={"outcome": "failed", "reason": "model_unavailable", "aggregates": [None]}
    )
    refused = compare_results(left, failed, left_subject=0, right_subject=0)
    assert not refused.comparable and refused.reasons == ["outcome"] and refused.delta is None


@pytest.mark.parametrize(
    "changed",
    [
        "suite",
        "cases",
        "scorer",
        "decoding",
        "runtime",
        "tokenizer",
        "template",
        "capability",
        "judge",
    ],
)
def test_every_compatibility_dimension_is_checked(changed: str) -> None:
    from coire_api.evaluation.comparison import compare_results

    left = result()
    right = left
    if changed == "suite":
        right = left.model_copy(
            update={"suite": left.suite.model_copy(update={"content_sha256": "a" * 64})}
        )
    elif changed in {"cases", "scorer"}:
        right = left.model_copy(
            update={
                "suite": left.suite.model_copy(
                    update={
                        "template": left.suite.template.model_copy(
                            update={"cases_sha256": "b" * 64}
                            if changed == "cases"
                            else {"scorer_version": "changed"}
                        )
                    }
                )
            }
        )
    elif changed == "decoding":
        right = left.model_copy(
            update={
                "suite": left.suite.model_copy(
                    update={"generation": left.suite.generation.model_copy(update={"seed": 17})}
                )
            }
        )
    elif changed == "judge":
        right = left.model_copy(
            update={"suite": left.suite.model_copy(update={"judge": left.subjects[0]})}
        )
    else:
        field = {
            "runtime": "runtime_sha256",
            "tokenizer": "tokenizer_sha256",
            "template": "template_sha256",
            "capability": "capability_sha256",
        }[changed]
        target = left.subjects[0]
        right = left.model_copy(
            update={
                "subjects": [
                    target.model_copy(
                        update={
                            "runtime": target.runtime.model_copy(
                                update={
                                    field: "e" * 64
                                    if getattr(target.runtime, field) != "e" * 64
                                    else "a" * 64
                                }
                            )
                        }
                    )
                ]
            }
        )
    comparison = compare_results(left, right, left_subject=0, right_subject=0)
    assert not comparison.comparable and changed in comparison.reasons
    assert comparison.delta is None


def test_subject_index_must_exist_and_pairwise_never_claims_quality_delta() -> None:
    from coire_api.evaluation.comparison import compare_results
    from coire_core.errors import EvaluationValidationError
    from coire_core.models.evaluation import SuiteMode

    left = result()
    with pytest.raises(EvaluationValidationError):
        compare_results(left, left, left_subject=1, right_subject=0)
    pairwise = left.model_copy(
        update={
            "suite": left.suite.model_copy(
                update={
                    "template": left.suite.template.model_copy(update={"mode": SuiteMode.PAIRWISE})
                }
            )
        }
    )
    comparison = compare_results(pairwise, pairwise, left_subject=0, right_subject=0)
    assert comparison.comparable and comparison.delta is None
    assert comparison.left_score is None and comparison.right_score is None


async def test_legacy_result_has_explicit_missing_provenance_without_delta() -> None:
    from unittest.mock import AsyncMock

    from coire_api.db import EvaluationResultRow, HarnessEvaluationRow
    from coire_api.evaluation.comparison import compare_stored_results

    current = result()
    legacy = uuid.uuid4()
    session = AsyncMock()
    session.get.side_effect = [
        None,
        HarnessEvaluationRow(id=legacy),
        EvaluationResultRow(result=current.model_dump(mode="json")),
    ]
    comparison = await compare_stored_results(
        session, str(legacy), current.id, left_subject=0, right_subject=0
    )
    assert not comparison.comparable and comparison.reasons == ["legacy_provenance"]
    assert comparison.delta is None and comparison.left_score is None
    assert comparison.left_result_id == legacy
