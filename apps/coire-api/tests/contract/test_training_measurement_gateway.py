"""The internal measured gateway accepts frozen workload bindings, not scores."""

from coire_api.app import create_app
from coire_core.settings import Settings


def test_node_authenticated_measurement_gateway_contract() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    operation = document["paths"][
        "/api/v1/internal/training/measurements/{measurement_id}/generate"
    ]["post"]
    assert operation["security"] == [{"NodeCredential": []}]
    schema = document["components"]["schemas"]["TrainingMeasurementGenerateRequest"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"principal_sha256", "target", "prompt", "max_output_tokens"}
    assert schema["properties"]["max_output_tokens"]["maximum"] == 1024
    assert "report" not in schema["properties"] and "principal" not in schema["properties"]
