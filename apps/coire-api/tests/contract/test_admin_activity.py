from coire_api.app import create_app
from coire_core.settings import Settings


def test_activity_union_and_stop_routes_are_admin_guarded_and_typed() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    activity = document["paths"]["/api/v1/admin/console/activity"]["get"]
    assert activity["security"] == [{"HTTPBearer": []}]
    schema = activity["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/CursorPage_ActivityItem_")
    cancel = document["paths"]["/api/v1/admin/jobs/{job_id}"]["delete"]
    assert cancel["security"] == [{"HTTPBearer": []}]
    assert "202" in cancel["responses"]
