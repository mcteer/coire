from __future__ import annotations

from coire_api.app import create_app
from coire_core.settings import Settings


def test_harness_evaluation_routes_are_typed_and_admin_guarded() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    collection = document["paths"]["/api/v1/admin/harness-evaluations"]
    assert collection["post"]["security"]
    assert collection["get"]["security"]
    assert collection["post"]["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("HarnessEvaluationSubmission")
    assert collection["post"]["responses"]["201"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("HarnessEvaluation")
    assert document["paths"]["/api/v1/admin/harness-evaluations/{evaluation_id}"]["get"]["security"]


async def test_loose_score_submission_is_refused_before_score_or_audit_writes() -> None:
    from unittest.mock import AsyncMock, Mock

    import pytest
    from fastapi import HTTPException

    from coire_api.routes.admin_evaluations import submit_evaluation

    principal = Mock()
    session = AsyncMock()
    with pytest.raises(HTTPException) as failure:
        await submit_evaluation(Mock(), principal, session)
    assert failure.value.status_code == 409
    assert "execution" in str(failure.value.detail).lower()
    session.commit.assert_not_awaited()
    session.add.assert_not_called()
