"""Installed catalog routes expose bounded typed metadata behind admin authorization."""

from coire_api.app import create_app
from coire_core.settings import Settings


def test_catalog_routes_have_security_and_typed_mutations() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    paths = document["paths"]
    for path in (
        "/api/v1/admin/evaluation-suite-templates",
        "/api/v1/admin/evaluation-suites",
        "/api/v1/admin/evaluation-suites/{suite_id}/versions/{version}",
    ):
        assert paths[path]["get"]["security"]
    mutation = paths["/api/v1/admin/evaluation-suites"]["post"]
    assert mutation["security"]
    assert any(
        p["name"].lower() == "idempotency-key" and p["required"] for p in mutation["parameters"]
    )
    assert mutation["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "EvaluationSuite"
    )
    assert paths["/api/v1/admin/evaluation-suites/{suite_id}/versions/{version}/retire"]["post"][
        "security"
    ]
