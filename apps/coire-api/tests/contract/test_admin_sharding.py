from __future__ import annotations

from coire_api.app import create_app
from coire_core.settings import Settings


def test_sharding_admin_contract_is_typed_and_guarded() -> None:
    document = create_app(Settings(_secrets_dir="/nonexistent")).openapi()  # type: ignore[call-arg]
    paths = document["paths"]
    for path, method, schema in (
        ("/api/v1/admin/links/studios", "get", "StudioLinkProjection"),
        ("/api/v1/admin/links/studios/probe", "post", "StudioLinkProjection"),
    ):
        operation = paths[path][method]
        assert operation["security"] == [{"HTTPBearer": []}]
        response = operation["responses"]["200" if method == "get" else "202"]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith(f"/{schema}")
    schemas = document["components"]["schemas"]
    assert schemas["LinkProbeRequest"]["additionalProperties"] is False
    assert schemas["StudioLinkProjection"]["additionalProperties"] is False
    for method in ("get", "post"):
        operation = paths["/api/v1/admin/benchmarks"][method]
        assert operation["security"] == [{"HTTPBearer": []}]
    assert schemas["BenchmarkRequest"]["additionalProperties"] is False
    assert schemas["BenchmarkRun"]["additionalProperties"] is False
