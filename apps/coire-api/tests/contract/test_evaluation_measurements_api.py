"""Measurement contracts expose qualification status without client-reported scores."""

from coire_api.app import create_app
from coire_core.settings import Settings


def test_measurement_admin_shapes_and_required_mutation_keys() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    paths = document["paths"]
    for path, method in (
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
    components = document["components"]["schemas"]
    request = components["EvaluationMeasurementRequest"]
    assert request["additionalProperties"] is False
    assert "report" not in request["properties"]
    assert request["properties"]["requests_per_phase"]["minimum"] == 100
    assert "profile_status" in components["EvaluationMeasurementDetail"]["properties"]
