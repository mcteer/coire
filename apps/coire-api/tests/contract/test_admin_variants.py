from __future__ import annotations

from coire_api.app import create_app
from coire_core.settings import Settings


def test_variant_comparison_and_publication_are_typed_and_guarded() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    path = document["paths"]["/api/v1/admin/models/{model_id}/variants"]
    assert path["get"]["security"]
    item = path["get"]["responses"]["200"]["content"]["application/json"]["schema"]["items"]
    assert item["$ref"].endswith("ModelVariant")
    patch = document["paths"]["/api/v1/admin/models/{model_id}/variants/{variant_id}"]["patch"]
    assert patch["security"]
    assert patch["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "VariantPublication"
    )
    assert any(parameter["name"] == "If-Match" for parameter in patch["parameters"])
