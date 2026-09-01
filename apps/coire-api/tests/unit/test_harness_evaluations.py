from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from coire_api.evaluations import record
from coire_core.models.harness import (
    CategoryScores,
    EvaluationVerdict,
    HarnessEvaluationSubmission,
)


def submission(verdict: EvaluationVerdict) -> HarnessEvaluationSubmission:
    return HarnessEvaluationSubmission(
        variant_id=uuid.uuid4(),
        scores=CategoryScores(
            tool_calling=1, structured_output=0.8, edit_application=0.9, long_context=1
        ),
        verdict=verdict,
        harness_version="0.1.0",
        engine_version="mlx-lm-test",
    )


async def test_pass_verifies_exact_variant_and_appends_scorecard() -> None:
    variant = SimpleNamespace(harness_verified=False, harness_verified_at=None)
    session = AsyncMock()
    session.add = Mock()
    session.get.return_value = variant
    result = await record(session, submission(EvaluationVerdict.PASSED))
    assert result.overall_score == 0.925
    assert variant.harness_verified is True
    session.add.assert_called_once()
    session.flush.assert_awaited_once()


async def test_failure_revokes_but_infrastructure_error_preserves_verification() -> None:
    verified_at = object()
    variant = SimpleNamespace(harness_verified=True, harness_verified_at=verified_at)
    session = AsyncMock()
    session.add = Mock()
    session.get.return_value = variant
    await record(session, submission(EvaluationVerdict.INFRASTRUCTURE_ERROR))
    assert variant.harness_verified is True
    assert variant.harness_verified_at is verified_at

    regressed = SimpleNamespace(harness_verified=True, harness_verified_at=object())
    session.get.return_value = regressed
    await record(session, submission(EvaluationVerdict.FAILED))
    assert regressed.harness_verified is False
    assert regressed.harness_verified_at is None
