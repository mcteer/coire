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
