"""API-key lifecycle, one-time secret, scope, and usage contracts."""

from coire_api.app import create_app
from coire_core.settings import Settings


def test_key_lifecycle_routes_and_one_time_secret_shape() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    paths = document["paths"]
    assert {"get", "post"} <= paths["/api/v1/admin/users/{user_id}/keys"].keys()
    assert "post" in paths["/api/v1/admin/keys/{key_id}/rotate"]
    assert {"patch", "delete"} <= paths["/api/v1/admin/keys/{key_id}"].keys()
    schemas = document["components"]["schemas"]
    assert "secret" not in schemas["ApiKey"]["properties"]
    assert "secret" in schemas["ApiKeyIssued"]["properties"]


def test_key_contract_exposes_scopes_limits_and_current_usage() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    properties = document["components"]["schemas"]["ApiKey"]["properties"]
    assert {
        "scopes",
        "requests_per_minute",
        "monthly_budget_tokens",
        "tokens_consumed",
        "period_resets_at",
    } <= properties.keys()
    scopes = set(document["components"]["schemas"]["AuthScope"]["enum"])
    assert scopes == {"chat", "images", "images:explicit", "mcp", "admin"}
