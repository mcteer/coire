from __future__ import annotations

from typing import Any

from coire_api.app import create_app
from coire_core.settings import Settings


def _document() -> dict[str, Any]:
    return create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]


def test_instance_and_cluster_routes_are_typed_and_authenticated() -> None:
    document = _document()
    paths = document["paths"]
    for path, methods in {
        "/api/v1/instances": ("get", "post"),
        "/api/v1/instances/{instance_id}": ("get", "delete"),
        "/api/v1/instances/{instance_id}/events": ("get",),
        "/api/v1/state": ("get",),
        "/api/v1/admin/nodes": ("post",),
        "/api/v1/admin/nodes/{node_id}/registration-token": ("post", "delete"),
    }.items():
        for method in methods:
            operation = paths[path][method]
            assert operation["security"] == [{"HTTPBearer": []}]


def test_instance_wire_inputs_forbid_caller_engine_arguments() -> None:
    schemas = _document()["components"]["schemas"]
    assert schemas["InstanceCreate"]["additionalProperties"] is False
    assert "engine_argv" not in schemas["InstanceCreate"]["properties"]
    assert schemas["ModelInstance"]["additionalProperties"] is False
    assert schemas["ClusterState"]["additionalProperties"] is False


def test_event_stream_documents_last_event_id() -> None:
    operation = _document()["paths"]["/api/v1/instances/{instance_id}/events"]["get"]
    header = next(item for item in operation["parameters"] if item["name"] == "Last-Event-ID")
    assert header["in"] == "header"
