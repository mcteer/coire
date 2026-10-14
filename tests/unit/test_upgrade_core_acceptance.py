"""Core cannot obtain activation authority from stale ownership or catalog metadata alone."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from upgrade_fixtures import core_plan

from coire_api.auth import Principal
from coire_api.upgrades import acceptance, host_hold
from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    UpgradeKind,
    UpgradeStartRequest,
    UpgradeTargetList,
    UpgradeTargetStatus,
)
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault",
    [None, "owner", "stale", "future", "identity", "source", "bootstrap", "artifact", "replay"],
)
async def test_core_acceptance_requires_current_independent_owner_and_exact_source(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    value = core_plan()
    now = datetime.now(UTC)
    session = AsyncMock()
    session.scalar.return_value = SimpleNamespace(id=value.source.release_id)
    session.get.return_value = (
        None
        if fault == "owner"
        else SimpleNamespace(
            bootstrap_ready=True,
            identity="wrong-owner" if fault == "identity" else "coire-upgrade-executor",
            source_release_id=value.source.release_id,
            heartbeat_at=now + timedelta(seconds=1)
            if fault == "future"
            else now - timedelta(seconds=11)
            if fault == "stale"
            else now,
        )
    )
    monkeypatch.setattr(host_hold, "reconcile_orphan_hold", AsyncMock())
    hold = Mock()
    monkeypatch.setattr(host_hold, "operation_hold", hold)
    monkeypatch.setattr(
        acceptance, "accept", AsyncMock(return_value=(value.operation, fault != "replay"))
    )
    monkeypatch.setattr(
        acceptance, "get_release", AsyncMock(side_effect=[value.source, value.target])
    )
    artifact = Mock(
        side_effect=[value.target if fault == "artifact" else value.source, value.target]
    )
    monkeypatch.setattr(acceptance, "verify_prepared_release", artifact)
    prepare = AsyncMock(return_value=value)
    monkeypatch.setattr(acceptance, "prepare_plan", prepare)
    statuses = AsyncMock(
        return_value=UpgradeTargetList(
            items=(
                UpgradeTargetStatus(
                    kind=UpgradeKind.CONTROL_PLANE,
                    release_id=value.source.release_id,
                    desired_sha256="f" * 64 if fault == "source" else value.source.manifest_sha256,
                    observed_sha256="f" * 64 if fault == "source" else value.source.manifest_sha256,
                    readiness="not_ready" if fault == "bootstrap" else "ready",
                    bootstrap_ready=fault != "bootstrap",
                    observed_at=now,
                ),
            )
        )
    )
    monkeypatch.setattr(acceptance, "target_statuses", statuses)
    request = UpgradeStartRequest(
        release_id=value.target.release_id,
        expected_source_sha256=value.source.manifest_sha256,
        idempotency_key=value.operation.idempotency_key,
        acknowledge_maintenance=True,
    )
    if fault not in {None, "replay"}:
        with pytest.raises(UpgradeConflict):
            await acceptance.accept_core(session, Principal(), request, Settings())
        prepare.assert_not_awaited()
        hold.assert_not_called()
    else:
        result, created = await acceptance.accept_core(session, Principal(), request, Settings())
        assert result == value.operation
        assert created is (fault != "replay")
        if fault == "replay":
            statuses.assert_not_awaited()
            session.get.assert_not_awaited()
            artifact.assert_not_called()
            prepare.assert_not_awaited()
            hold.assert_not_called()
        else:
            assert artifact.call_count == 2
            prepare.assert_awaited_once()
            hold.assert_called_once()
