"""Entitlement lifecycle and immutable audit query contracts."""

from coire_api.app import create_app
from coire_core.settings import Settings


def test_entitlement_and_audit_routes_are_complete_and_append_only() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    paths = document["paths"]
    entitlement = paths["/api/v1/admin/users/{user_id}/entitlements/{name}"]
    assert set(entitlement) >= {"put", "delete"}
    assert set(paths["/api/v1/admin/audit"]) == {"get"}
    assert set(paths["/api/v1/admin/audit/{audit_id}"]) == {"get"}


def test_audit_list_has_bounded_filters_and_typed_projection() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    operation = document["paths"]["/api/v1/admin/audit"]["get"]
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert {
        "limit",
        "target_id",
        "action",
        "actor",
        "actor_type",
        "outcome",
        "before",
        "before_id",
    } <= parameters.keys()
    assert parameters["limit"]["schema"]["maximum"] == 500
    audit = document["components"]["schemas"]["AuditRecord"]["properties"]
    assert {
        "actor_type",
        "actor_user_id",
        "request_id",
        "before",
        "after",
        "context",
    } <= audit.keys()
