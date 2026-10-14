"""Compatibility instance exposes only authenticated recovery and fixed readiness."""

import pytest
from fastapi.testclient import TestClient

from coire_core.errors import UpgradeForbidden
from coire_core.settings import Settings


def test_compatibility_api_keeps_incumbent_surface_and_other_failures_out_of_scope(monkeypatch):
    from coire_api.upgrades.recovery_support import create_recovery_support
    from coire_api.upgrades.recovery_support_scope import require_original, scope

    job = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    app = create_recovery_support(
        Settings(
            upgrades_enabled=True,
            upgrade_recovery_support_job_id=job,
            upgrade_recovery_support_generation=57,
            upgrade_recovery_support_source_sha256="a" * 64,
        )
    )
    client = TestClient(app)
    assert client.get("/ready").status_code == 200
    for route in (
        "/v1/models",
        "/v1/chat/completions",
        "/api/v1/admin/upgrades/releases",
        "/openapi.json",
        "/api/v1/admin/runs",
    ):
        assert client.get(route).status_code == 404
    response = client.post("/api/v1/admin/upgrades/recoveries", json={})
    assert response.status_code == 401
    assert (
        client.post(
            "/api/v1/admin/upgrades/recoveries/01ARZ3NDEKTSV4RRFFQ69G5FAW/host-hold", json={}
        ).status_code
        == 401
    )
    assert client.get(f"/api/v1/admin/upgrades/recovery-schema/{job}").status_code == 401
    token = scope.set((job, 57, "a" * 64))
    try:
        require_original(job, 57, "a" * 64)
        for foreign in (
            (job, 58, "a" * 64),
            (job, 57, "b" * 64),
            ("01ARZ3NDEKTSV4RRFFQ69G5FAW", 57, "a" * 64),
        ):
            with pytest.raises(UpgradeForbidden):
                require_original(*foreign)
    finally:
        scope.reset(token)
    assert scope.get() is None


@pytest.mark.parametrize(
    "fields",
    [
        {"upgrade_recovery_support_job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        {
            "upgrade_recovery_support_job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "upgrade_recovery_support_generation": 57,
            "upgrade_recovery_support_source_sha256": "a" * 64,
            "upgrades_enabled": False,
        },
    ],
)
def test_compatibility_settings_require_complete_enabled_scope(fields):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(**fields)
