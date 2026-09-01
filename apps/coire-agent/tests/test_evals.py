from __future__ import annotations

import pytest

from coire_agent.evals import EvaluationEvidence, score
from coire_core.models.harness import EvaluationVerdict


def test_all_categories_must_meet_threshold() -> None:
    scores, verdict = score(EvaluationEvidence(4, 5, 5, 5, 4, 5, 9, 10))
    assert scores.long_context == 0.9
    assert verdict is EvaluationVerdict.PASSED

    _, verdict = score(EvaluationEvidence(3, 5, 5, 5, 5, 5, 5, 5))
    assert verdict is EvaluationVerdict.FAILED


def test_empty_or_invalid_case_sets_are_rejected() -> None:
    with pytest.raises(ValueError):
        score(EvaluationEvidence(0, 0, 1, 1, 1, 1, 1, 1))
