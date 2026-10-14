"""New executor claims require verified bootstrap; retained recovery cannot widen ownership."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from upgrade_fixtures import core_plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.upgrades import executor
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import UpgradeClaimRequest
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "bootstrap",
        "stale",
        "project",
        "source",
        "owner",
        "cancel",
        "replay",
        "replay_stale",
        "replay_unready",
        "revoked",
    ],
)
async def test_executor_claim_is_project_source_and_bootstrap_bound(
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    plan = core_plan()
    operation = plan.operation.model_copy(update={"cancel_requested": fault == "cancel"})
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR,
        subject="coire-upgrade-executor",
        scopes=frozenset({"upgrades:execute"}),
    )
    row = SimpleNamespace(
        payload=operation.model_dump(mode="json"),
        execution_plan=plan.model_dump(mode="json"),
        executor_identity=(
            "another-owner"
            if fault == "owner"
            else principal.subject
            if fault in {"replay", "replay_stale", "replay_unready"}
            else None
        ),
        user_id=operation.user_id,
        key_id=None,
        credential_version=None,
    )
    owner = SimpleNamespace(
        bootstrap_ready=fault not in {"bootstrap", "replay_unready"},
        identity=principal.subject,
        source_release_id=operation.source_release_id,
        heartbeat_at=datetime.now(UTC)
        - timedelta(seconds=11 if fault in {"stale", "replay_stale"} else 0),
    )
    session = AsyncMock()
    session.get.return_value = owner
    session.scalar.return_value = row
    monkeypatch.setattr(executor, "operation_lock", AsyncMock())
    authorized = AsyncMock(side_effect=UpgradeForbidden() if fault == "revoked" else None)
    monkeypatch.setattr(executor, "authorize", authorized)
    monkeypatch.setattr(executor, "write_principal_audit", AsyncMock())
    request = UpgradeClaimRequest(
        executor_id=principal.subject,
        project="other" if fault == "project" else "coire",
        observed_source_sha256="f" * 64 if fault == "source" else operation.source_sha256,
    )
    before = row.executor_identity
    if fault not in {None, "replay", "replay_stale", "replay_unready", "cancel", "revoked"}:
        with pytest.raises((UpgradeConflict, UpgradeForbidden, UpgradeUnavailable)):
            await executor.claim(session, principal, request, Settings(upgrades_enabled=True))
        assert row.executor_identity == before
        authorized.assert_not_awaited()
    else:
        if fault == "revoked":
            result = await executor.claim(
                session, principal, request, Settings(upgrades_enabled=True)
            )
            assert result.operation.cancel_requested
            assert result.operation.reason == "authorization_revoked"
            assert row.payload["cancel_requested"]
            assert result.authority_sha256() == plan.authority_sha256()
            assert row.executor_identity == principal.subject
            authorized.assert_awaited_once()
            return
        assert await executor.claim(
            session, principal, request, Settings(upgrades_enabled=True)
        ) == plan.model_copy(update={"operation": operation})
        assert row.executor_identity == principal.subject
        assert authorized.await_count == (
            0 if fault in {"replay", "replay_stale", "replay_unready", "cancel"} else 1
        )
