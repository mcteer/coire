"""Durable execution routes use bounded typed admin contracts and mutation keys."""

from coire_api.app import create_app
from coire_core.settings import Settings


def test_execution_routes_are_registered_and_authenticated() -> None:
    paths = create_app(Settings(_secrets_dir="/nonexistent")).openapi()["paths"]  # type: ignore[call-arg]
    for path, method in (
        ("/api/v1/admin/evaluations", "post"),
        ("/api/v1/admin/evaluations", "get"),
        ("/api/v1/admin/evaluation-comparisons", "get"),
        ("/api/v1/admin/evaluations/{run_id}", "get"),
        ("/api/v1/admin/evaluations/{run_id}/events", "get"),
        ("/api/v1/admin/evaluations/{run_id}/cancel", "post"),
        ("/api/v1/admin/evaluations/{run_id}/rerun", "post"),
        ("/api/v1/admin/evaluations/{run_id}/evidence/{evidence_id}", "get"),
        ("/api/v1/admin/evaluation-groups/{group_id}", "get"),
        ("/api/v1/admin/evaluation-groups/{group_id}/events", "get"),
        ("/api/v1/admin/evaluation-measurements", "post"),
        ("/api/v1/admin/evaluation-measurements/{measurement_id}", "get"),
        ("/api/v1/admin/evaluation-measurements/{measurement_id}/cancel", "post"),
    ):
        operation = paths[path][method]
        assert operation["security"]
        if method == "post":
            assert any(
                item["name"].lower() == "idempotency-key" and item["required"]
                for item in operation["parameters"]
            )
