from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from coire_api.app import create_app
from coire_api.console.service import project_core_capacity
from coire_core.models.console import CoreHostCapacity
from coire_core.settings import Settings


def _document() -> dict[str, Any]:
    return create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]


def test_console_routes_are_typed_and_admin_authenticated() -> None:
    document = _document()
    for path, method, response in (
        ("/api/v1/admin/console", "get", "ConsoleSnapshot"),
        ("/api/v1/admin/ops/ask", "post", "AskResponse"),
    ):
        operation = document["paths"][path][method]
        assert operation["security"] == [{"HTTPBearer": []}]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith(f"/{response}")

    activity = document["paths"]["/api/v1/admin/console/activity"]["get"]
    assert activity["security"] == [{"HTTPBearer": []}]
    schema = activity["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/CursorPage_ActivityItem_")
    assert {parameter["name"] for parameter in activity["parameters"]} >= {
        "limit",
        "before",
        "before_id",
    }


def test_console_stream_documents_resume_header() -> None:
    operation = _document()["paths"]["/api/v1/admin/console/events"]["get"]
    assert operation["security"] == [{"HTTPBearer": []}]
    header = next(item for item in operation["parameters"] if item["name"] == "Last-Event-ID")
    assert header["in"] == "header"


def test_console_contracts_forbid_extra_fields_and_mutation_shapes() -> None:
    schemas = _document()["components"]["schemas"]
    for name in ("ConsoleSnapshot", "AskRequest", "AskResponse"):
        assert schemas[name]["additionalProperties"] is False
    ask_response = schemas["AskResponse"]["properties"]
    assert not ({"action", "tool", "confirm_token"} & ask_response.keys())


def test_console_projects_truthful_core_runtime_capacity() -> None:
    observed = project_core_capacity(datetime.now(UTC))
    assert isinstance(observed, CoreHostCapacity)
    assert observed.source == "core-control-plane-runtime"
    assert observed.memory_total_bytes >= observed.memory_free_bytes >= 0
    assert observed.disk_total_bytes >= observed.disk_free_bytes >= 0


def test_model_collection_is_paginated_and_edits_are_versioned() -> None:
    document = _document()
    listing = document["paths"]["/api/v1/admin/models"]["get"]
    assert {item["name"] for item in listing["parameters"]} >= {
        "limit",
        "before",
        "before_id",
    }
    update = document["paths"]["/api/v1/admin/models/{model_id}"]["patch"]
    assert "If-Match" in {item["name"] for item in update["parameters"]}
