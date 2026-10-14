"""Strict administrative recovery revocation, with no installation authority."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_upgrade_admin import client

from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancellation,
    UpgradeExpiredRecoveryCancelRequest,
)

RECOVERY = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
ROUTE = f"/api/v1/admin/upgrades/recoveries/{RECOVERY}/cancel"


@pytest.mark.parametrize("created", [False, True])
def test_cancellation_route_returns_original_retained_receipt(monkeypatch, created):
    from coire_api.upgrades import expired_recovery_cancel as service

    proof = UpgradeExpiredRecoveryCancellation(
        recovery_id=RECOVERY,
        intent_sha256="a" * 64,
        original_job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        original_generation=7,
        cancelled_by=uuid4(),
        cancelled_at=datetime.now(UTC),
    )
    cancel = AsyncMock(return_value=(proof, created))
    record = Mock()
    monkeypatch.setattr(service, "cancel_recovery", cancel)
    monkeypatch.setattr(service, "record_cancellation", record)
    api, session, _ = client(monkeypatch)
    response = api.post(ROUTE, json={"intent_sha256": "a" * 64})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == proof.model_dump(mode="json")
    assert cancel.await_args.args[0] is session
    assert cancel.await_args.args[2] == RECOVERY
    record.assert_called_once_with(proof, created)


@pytest.mark.parametrize("fault", ["disabled", "unauthorized", "extra", "hash", "path"])
def test_cancellation_refuses_before_service(monkeypatch, fault):
    from coire_api.upgrades import expired_recovery_cancel as service

    cancel = AsyncMock()
    record = Mock()
    monkeypatch.setattr(service, "cancel_recovery", cancel)
    monkeypatch.setattr(service, "record_cancellation", record)
    api, _, _ = client(monkeypatch, enabled=fault != "disabled", allowed=fault != "unauthorized")
    body = {"intent_sha256": "a" * 64}
    if fault == "extra":
        body["deadline_at"] = "2099-01-01T00:00:00Z"
    if fault == "hash":
        body["intent_sha256"] = "invalid"
    response = api.post(ROUTE.replace(RECOVERY, "invalid") if fault == "path" else ROUTE, json=body)
    assert response.status_code == {"disabled": 503, "unauthorized": 403}.get(fault, 422)
    cancel.assert_not_awaited()
    record.assert_not_called()


def test_cancellation_models_cannot_clear_maintenance_or_supply_deadline():
    with pytest.raises(ValidationError):
        UpgradeExpiredRecoveryCancelRequest.model_validate(
            {"intent_sha256": "a" * 64, "deadline_at": datetime.now(UTC)}
        )
    with pytest.raises(ValidationError):
        UpgradeExpiredRecoveryCancellation(
            recovery_id=RECOVERY,
            intent_sha256="a" * 64,
            original_job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            original_generation=7,
            cancelled_by=uuid4(),
            cancelled_at=datetime.now(UTC),
            maintenance_retained=False,
        )


def test_failed_commit_does_not_publish_cancellation_metric(monkeypatch):
    from coire_api.routes import admin_upgrades
    from coire_api.upgrades import expired_recovery_cancel as service

    proof = UpgradeExpiredRecoveryCancellation(
        recovery_id=RECOVERY,
        intent_sha256="a" * 64,
        original_job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        original_generation=7,
        cancelled_by=uuid4(),
        cancelled_at=datetime.now(UTC),
    )
    api, session, _ = client(monkeypatch)
    cancel = AsyncMock(return_value=(proof, True))
    record = Mock()
    monkeypatch.setattr(service, "cancel_recovery", cancel)
    monkeypatch.setattr(service, "record_cancellation", record)

    @asynccontextmanager
    async def failed_commit():
        yield session
        raise RuntimeError("fixture commit failed")

    monkeypatch.setattr(admin_upgrades, "session_scope", failed_commit)
    with pytest.raises(RuntimeError, match="fixture commit failed"):
        api.post(ROUTE, json={"intent_sha256": "a" * 64})
    record.assert_not_called()
