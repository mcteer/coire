from __future__ import annotations

from coire_api.app import create_app
from coire_core.settings import Settings


def _document() -> dict:  # type: ignore[type-arg]
    return create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]


def test_acquisition_routes_are_typed_and_admin_guarded() -> None:
    document = _document()
    paths = document["paths"]
    expected = {
        "/api/v1/admin/models/acquisitions": ("post", "AcquisitionWorkflow"),
        "/api/v1/admin/acquisitions/{workflow_id}": ("get", "AcquisitionWorkflow"),
        "/api/v1/admin/acquisitions/{workflow_id}/retry": ("post", "AcquisitionWorkflow"),
    }
    for path, (method, schema) in expected.items():
        operation = paths[path][method]
        assert operation["security"]
        success = operation["responses"]["200" if method == "get" else "202"]
        assert success["content"]["application/json"]["schema"]["$ref"].endswith(schema)


def test_no_route_can_upload_or_publish_to_hugging_face() -> None:
    for path in _document()["paths"]:
        lowered = path.lower()
        assert "huggingface" not in lowered
        assert "upload" not in lowered
        assert "push" not in lowered
