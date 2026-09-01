"""Identity administration and self-projection contract surface."""

from coire_api.app import create_app
from coire_core.settings import Settings


def test_user_and_self_routes_are_published() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    paths = document["paths"]
    assert paths["/api/v1/me"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/User")
    assert {"get", "post"} <= paths["/api/v1/admin/users"].keys()
    assert {"patch", "delete"} <= paths["/api/v1/admin/users/{user_id}"].keys()


def test_user_contract_is_strict_and_role_bounded() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    schemas = document["components"]["schemas"]
    assert schemas["UserCreate"]["additionalProperties"] is False
    assert set(schemas["UserRole"]["enum"]) == {"admin", "user"}
