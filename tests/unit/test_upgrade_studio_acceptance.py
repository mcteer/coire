"""Accepted Studio intent is atomic with Source readiness and artifact verification."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from upgrade_fixtures import plan

from coire_api.auth import Principal
from coire_api.upgrades import acceptance
from coire_core.errors import UpgradeConflict, UpgradeNotFound
from coire_core.models.upgrades import (
    UpgradeAction,
    UpgradeKind,
    UpgradePhase,
    UpgradeRollbackRequest,
    UpgradeStartRequest,
    UpgradeTargetList,
    UpgradeTargetStatus,
)
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "replay", "source", "bootstrap", "artifact", "missing"])
async def test_studio_start_checks_current_source_and_exact_provenance_before_plan(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    value = plan()
    session = AsyncMock()
    session.scalar.return_value = (
        None if fault == "missing" else SimpleNamespace(id=value.source.release_id)
    )
    accept = AsyncMock(return_value=(value.operation, fault != "replay"))
    monkeypatch.setattr(acceptance, "accept", accept)
    statuses = AsyncMock(
        return_value=UpgradeTargetList(
            items=(
                UpgradeTargetStatus(
                    kind=UpgradeKind.STUDIO,
                    node="coire-edge-a",
                    release_id=value.source.release_id,
                    readiness="not_ready" if fault == "bootstrap" else "ready",
                    observed_sha256="f" * 64 if fault == "source" else value.source.manifest_sha256,
                    desired_sha256="f" * 64 if fault == "source" else value.source.manifest_sha256,
                    bootstrap_ready=fault != "bootstrap",
                    observed_at=datetime.now(UTC),
                ),
            )
        )
    )
    monkeypatch.setattr(acceptance, "target_statuses", statuses)
    monkeypatch.setattr(
        acceptance, "get_release", AsyncMock(side_effect=[value.source, value.target])
    )
    artifact = Mock(
        side_effect=[value.source if fault != "artifact" else value.target, value.target]
    )
    monkeypatch.setattr(acceptance, "verify_prepared_release", artifact)
    prepare = AsyncMock()
    prepare.return_value = value
    monkeypatch.setattr(acceptance, "prepare_plan", prepare)
    import coire_api.upgrades.host_hold as host_hold

    hold = Mock()
    monkeypatch.setattr(host_hold, "operation_hold", hold)
    monkeypatch.setattr(host_hold, "reconcile_orphan_hold", AsyncMock())
    request = UpgradeStartRequest(
        release_id=value.target.release_id,
        expected_source_sha256=value.source.manifest_sha256,
        idempotency_key=value.operation.idempotency_key,
        acknowledge_maintenance=True,
    )
    if fault in {"source", "bootstrap", "artifact", "missing"}:
        with pytest.raises(UpgradeNotFound if fault == "missing" else UpgradeConflict):
            await acceptance.accept_studio(
                session, Principal(), "coire-edge-a", request, Settings()
            )
        prepare.assert_not_awaited()
    else:
        result, created = await acceptance.accept_studio(
            session, Principal(), "coire-edge-a", request, Settings()
        )
        assert result == value.operation
        assert created is (fault != "replay")
        if fault == "replay":
            statuses.assert_not_awaited()
            artifact.assert_not_called()
            prepare.assert_not_awaited()
            hold.assert_not_called()
        else:
            prepare.assert_awaited_once()
            assert artifact.call_count == 2
            hold.assert_called_once()


@pytest.mark.parametrize("fault", [None, "nonterminal", "core", "revoked"])
async def test_rollback_reverses_only_successful_parent_through_live_acceptance(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_core.errors import UpgradeForbidden

    value = plan()
    now = datetime.now(UTC)
    parent = value.operation.model_copy(
        update={
            "phase": UpgradePhase.ACCEPTED if fault == "nonterminal" else UpgradePhase.SUCCEEDED,
            "updated_at": now,
            "finished_at": None if fault == "nonterminal" else now,
            **(
                {"kind": UpgradeKind.CONTROL_PLANE, "node": None, "cohort": ("coire-api",)}
                if fault == "core"
                else {}
            ),
        }
    )
    session = AsyncMock()
    authorize = AsyncMock(side_effect=UpgradeForbidden() if fault == "revoked" else None)
    monkeypatch.setattr(acceptance, "authorize", authorize)
    monkeypatch.setattr(acceptance, "operation_lock", AsyncMock())
    lookup = AsyncMock(return_value=SimpleNamespace(payload=parent.model_dump(mode="json")))
    monkeypatch.setattr(acceptance, "get_operation", lookup)
    accepted = AsyncMock(return_value=(value.operation, True))
    monkeypatch.setattr(acceptance, "accept_studio", accepted)
    core_accepted = AsyncMock(return_value=(parent, True))
    monkeypatch.setattr(acceptance, "accept_core", core_accepted)
    request = UpgradeRollbackRequest(
        expected_source_sha256=value.target.manifest_sha256,
        idempotency_key=value.operation.idempotency_key,
        acknowledge_maintenance=True,
    )
    if fault in {"nonterminal", "revoked"}:
        with pytest.raises(UpgradeForbidden if fault == "revoked" else UpgradeConflict):
            await acceptance.accept_rollback(
                session, Principal(), parent.job_id, request, Settings()
            )
        accepted.assert_not_awaited()
        core_accepted.assert_not_awaited()
    elif fault == "core":
        await acceptance.accept_rollback(session, Principal(), parent.job_id, request, Settings())
        accepted.assert_not_awaited()
        core_accepted.assert_awaited_once()
        call = core_accepted.await_args
        assert call.args[2].release_id == parent.source_release_id
        assert call.args[2].expected_source_sha256 == request.expected_source_sha256
        assert call.args[2].idempotency_key == request.idempotency_key
        assert call.kwargs == {"action": UpgradeAction.ROLLBACK, "parent_job_id": parent.job_id}
    else:
        await acceptance.accept_rollback(session, Principal(), parent.job_id, request, Settings())
        call = accepted.await_args
        assert call.args[2] == parent.node
        assert call.args[3].release_id == parent.source_release_id
        assert call.args[3].expected_source_sha256 == request.expected_source_sha256
        assert call.args[3].idempotency_key == request.idempotency_key
        assert call.kwargs == {"action": UpgradeAction.ROLLBACK, "parent_job_id": parent.job_id}
