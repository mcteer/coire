"""Rollback and cancellation keep retained destinations and audited recovery intent."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from upgrade_fixtures import core_plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.routes import admin_upgrades
from coire_api.upgrades import acceptance, service
from coire_api.upgrades.authorization import require_upgrade_admin
from coire_core.errors import CoireError, UpgradeConflict, UpgradeForbidden
from coire_core.models.auth import UserRole
from coire_core.models.upgrades import UpgradeAction, UpgradeOperation, UpgradePhase
from coire_core.settings import Settings


def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AsyncMock]:
    app = FastAPI()
    app.state.settings = Settings(upgrades_enabled=True)
    app.include_router(admin_upgrades.router)

    async def admin() -> Principal:
        return Principal(kind=PrincipalKind.ADMIN, user_id=uuid4(), role=UserRole.ADMIN)

    async def problem(_request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, CoireError)
        return JSONResponse(error.to_problem().model_dump(mode="json"), status_code=error.status)

    app.dependency_overrides[require_upgrade_admin] = admin
    app.add_exception_handler(CoireError, problem)
    session = AsyncMock(spec=AsyncSession)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session

    monkeypatch.setattr(admin_upgrades, "session_scope", sessions)
    return TestClient(app), session


@pytest.mark.parametrize(
    "phase",
    [UpgradePhase.ACCEPTED, UpgradePhase.DRAINING, UpgradePhase.ACTIVATING, UpgradePhase.VERIFYING],
)
def test_core_cancel_records_intent_once_without_calling_a_workload_stop(
    monkeypatch: pytest.MonkeyPatch, phase: UpgradePhase
) -> None:
    api, session = client(monkeypatch)
    plan = core_plan()
    original = plan.operation.model_copy(update={"phase": phase})
    row = SimpleNamespace(
        payload=original.model_dump(mode="json"), execution_plan=plan.model_dump(mode="json")
    )
    monkeypatch.setattr(service, "get_operation", AsyncMock(return_value=row))
    monkeypatch.setattr(service, "authorize", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(service, "write_principal_audit", audit)
    route = f"/api/v1/admin/upgrades/{original.job_id}/cancel"
    response = api.post(route, json={"reason": "operator_requested_recovery"})
    assert response.status_code == 202
    cancelled = UpgradeOperation.model_validate(response.json())
    assert cancelled.cancel_requested and cancelled.phase is phase
    assert cancelled.source_release_id == original.source_release_id
    assert cancelled.target_release_id == original.target_release_id
    assert cancelled.receipt_sequence == original.receipt_sequence
    assert api.post(route, json={"reason": "retry"}).json() == cancelled.model_dump(mode="json")
    audit.assert_awaited_once()
    assert audit.call_args.kwargs["action"] == "upgrades.cancel.request"
    assert api.post(route, json={"command": "kill"}).status_code == 422
    session.add.assert_not_called()


@pytest.mark.parametrize("fault", ["revoked", "commit_boundary"])
def test_cancel_refusal_does_not_rewrite_the_retained_operation(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    api, _ = client(monkeypatch)
    original = core_plan().operation.model_copy(
        update={"phase": UpgradePhase.COMMITTING, "cancellation_allowed": False}
    )
    row = SimpleNamespace(payload=original.model_dump(mode="json"))
    monkeypatch.setattr(service, "get_operation", AsyncMock(return_value=row))
    monkeypatch.setattr(
        service,
        "authorize",
        AsyncMock(side_effect=UpgradeForbidden() if fault == "revoked" else None),
    )
    audit = AsyncMock()
    monkeypatch.setattr(service, "write_principal_audit", audit)
    response = api.post(f"/api/v1/admin/upgrades/{original.job_id}/cancel", json={})
    assert response.status_code == (403 if fault == "revoked" else 409)
    assert row.payload == original.model_dump(mode="json")
    audit.assert_not_awaited()


@pytest.mark.parametrize("compatible", [False, True])
def test_rollback_reverses_only_the_retained_successful_destination(
    monkeypatch: pytest.MonkeyPatch, compatible: bool
) -> None:
    api, _ = client(monkeypatch)
    plan = core_plan()
    parent = plan.operation.model_copy(
        update={
            "phase": UpgradePhase.SUCCEEDED,
            "finished_at": datetime.now(UTC),
            "cancellation_allowed": False,
        }
    )
    monkeypatch.setattr(
        acceptance,
        "get_operation",
        AsyncMock(return_value=SimpleNamespace(payload=parent.model_dump(mode="json"))),
    )
    monkeypatch.setattr(acceptance, "authorize", AsyncMock())
    fresh = AsyncMock(
        return_value=(parent, False),
        side_effect=None
        if compatible
        else UpgradeConflict("Retained source cannot read current formats"),
    )
    monkeypatch.setattr(acceptance, "accept_core", fresh)
    body = {
        "idempotency_key": str(uuid4()),
        "expected_source_sha256": plan.target.manifest_sha256,
        "acknowledge_maintenance": True,
    }
    response = api.post(f"/api/v1/admin/upgrades/{parent.job_id}/rollback", json=body)
    assert response.status_code == (200 if compatible else 409)
    reversed_request = fresh.call_args.args[2]
    assert reversed_request.release_id == parent.source_release_id
    assert reversed_request.expected_source_sha256 == plan.target.manifest_sha256
    assert fresh.call_args.kwargs == {
        "action": UpgradeAction.ROLLBACK,
        "parent_job_id": parent.job_id,
    }


@pytest.mark.parametrize(
    "fault", [None, "unreadable_schema", "schema_downgrade", "old_journal", "old_privacy"]
)
def test_retained_rollback_plan_cannot_cross_persistent_format_boundaries(
    fault: str | None,
) -> None:
    from pydantic import ValidationError

    from coire_core.models.upgrades import UpgradeExecutionPlan, UpgradeReleaseManifest

    forward = core_plan(schema_changed=True)
    operation = forward.operation.model_dump(mode="json") | {
        "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAA",
        "action": "rollback",
        "parent_job_id": forward.operation.job_id,
        "source_release_id": str(forward.target.release_id),
        "target_release_id": str(forward.source.release_id),
        "source_sha256": forward.target.manifest_sha256,
        "target_sha256": forward.source.manifest_sha256,
    }
    payload = forward.model_dump(mode="json") | {
        "operation": operation,
        "source": forward.target.model_dump(mode="json"),
        "target": forward.source.model_dump(mode="json"),
        "maintenance": forward.maintenance.model_dump(mode="json")
        | {"job_id": operation["job_id"]},
        "initial_schema_head": "0035_candidate",
        "schema_head_after": "0035_candidate",
        "backup": None,
    }
    if fault == "unreadable_schema":
        payload["initial_schema_head"] = payload["schema_head_after"] = "0036_unknown"
    elif fault == "schema_downgrade":
        payload["schema_head_after"] = "0034_upgrades"
    elif fault in {"old_journal", "old_privacy"}:
        destination = payload["target"]
        compatibility = destination["manifest"]["compatibility"]
        if fault == "old_journal":
            compatibility["native_journal_versions"] = [2]
            destination["manifest_sha256"] = UpgradeReleaseManifest.model_validate(
                destination["manifest"]
            ).sha256()
            operation["target_sha256"] = destination["manifest_sha256"]
        else:
            compatibility["privacy_semantics"] = 0
    if fault is not None:
        reason = {
            "unreadable_schema": "source cannot read deployed schema",
            "schema_downgrade": "rollback retains the deployed schema",
            "old_journal": "source compatibility absent",
            "old_privacy": "Input should be 1",
        }[fault]
        with pytest.raises(ValidationError, match=reason):
            UpgradeExecutionPlan.model_validate(payload)
    else:
        rollback = UpgradeExecutionPlan.model_validate(payload)
        assert rollback.target == forward.source
        assert not rollback.needs_migration
        assert rollback.schema_head_after == "0035_candidate"
