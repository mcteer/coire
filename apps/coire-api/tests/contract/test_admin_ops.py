from __future__ import annotations

from typing import Any

from coire_api.app import create_app
from coire_core.settings import Settings


def _document() -> dict[str, Any]:
    app = create_app(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    return app.openapi()


def test_ops_routes_are_present_with_exact_human_and_service_boundaries() -> None:
    paths = _document()["paths"]
    expected = {
        ("/api/v1/admin/ops/conversations", "post"),
        ("/api/v1/admin/ops/conversations/{conversation_id}", "get"),
        ("/api/v1/admin/ops/conversations/{conversation_id}/messages", "post"),
        ("/api/v1/admin/ops/proposals/{proposal_id}", "get"),
        ("/api/v1/admin/ops/proposals/{proposal_id}/confirm", "post"),
        ("/api/v1/admin/ops/proposals/{proposal_id}/decline", "post"),
        ("/api/v1/internal/ops/sessions", "post"),
        ("/api/v1/internal/ops/sessions/{session_id}", "patch"),
        ("/api/v1/internal/ops/proposals", "post"),
    }
    assert expected <= {(path, method) for path, item in paths.items() for method in item}
    for path, method in expected:
        assert paths[path][method]["security"] == [{"HTTPBearer": []}]


def test_confirmation_contract_requires_the_token_and_echoed_exact_action() -> None:
    document = _document()
    schema = document["components"]["schemas"]["OpsConfirmRequest"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"confirm_token", "action"}
    assert schema["properties"]["confirm_token"]["pattern"].startswith("^coire_confirm_")
    action = schema["properties"]["action"]
    assert action["discriminator"]["propertyName"] == "operation"
    assert set(action["discriminator"]["mapping"]) == {
        "instance.unload",
        "run.kill",
        "model.pin",
        "model.unpin",
        "instance.load",
    }


def test_irreversible_operations_are_absent_from_generated_openapi() -> None:
    encoded = str(_document())
    for forbidden in (
        "model.retire",
        "model.acquire",
        "user.delete",
        "shell.exec",
        "route.call",
    ):
        assert forbidden not in encoded
