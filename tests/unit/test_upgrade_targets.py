"""Admin readiness cannot infer independent ownership from installed version strings."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from upgrade_fixtures import plan

from coire_api.upgrades import targets
from coire_core.models.upgrades import NodeUpgradeStatus, UpgradePhase
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "bootstrap", "runtime", "stale", "catalog", "maintenance"])
async def test_targets_require_verified_current_source_and_independent_bootstrap(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    value = plan()
    monkeypatch.setattr(targets, "deployment_readiness", lambda _: "ready")
    now = datetime.now(UTC)
    session = AsyncMock()
    session.scalars.return_value = SimpleNamespace(
        all=lambda: (
            [SimpleNamespace(payload=value.maintenance.model_dump(mode="json"))]
            if fault == "maintenance"
            else []
        )
    )
    session.scalar.return_value = (
        None
        if fault == "catalog"
        else SimpleNamespace(payload=value.source.model_dump(mode="json"))
    )
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def observe(node):
        return NodeUpgradeStatus(
            node=node,
            release_sha256=value.source.manifest_sha256,
            runtime_sha256="f" * 64
            if fault == "runtime"
            else value.source.manifest.environment_sha256,
            bootstrap_ready=fault != "bootstrap",
            observed_at=now - timedelta(seconds=6) if fault == "stale" else now,
        )

    client.upgrade_status.side_effect = observe
    monkeypatch.setattr(targets, "NodeClient", lambda *_args, **_kwargs: client)
    result = await targets.target_statuses(session, Settings())
    assert len(result.items) == 3
    assert (result.items[0].readiness == "ready") is (fault is None)
    assert result.items[2].readiness == "not_ready"
    assert result.items[2].bootstrap_ready is False
    if fault:
        assert result.items[0].blocking_reasons


@pytest.mark.parametrize("fault", [None, "pending", "identity", "incompatible"])
async def test_rollback_readiness_requires_fresh_retained_source_and_successful_parent(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    value = plan()
    now = datetime.now(UTC)
    parent = value.operation.model_copy(
        update={
            "phase": UpgradePhase.ACCEPTED if fault == "pending" else UpgradePhase.SUCCEEDED,
            "updated_at": now,
            "finished_at": None if fault == "pending" else now,
        }
    )
    session = AsyncMock()
    session.scalars.return_value = SimpleNamespace(all=lambda: [])
    session.scalar.return_value = SimpleNamespace(payload=value.target.model_dump(mode="json"))
    monkeypatch.setattr(targets, "deployment_readiness", lambda _: "ready")
    monkeypatch.setattr(
        targets,
        "get_operation",
        AsyncMock(return_value=SimpleNamespace(payload=parent.model_dump(mode="json"))),
    )
    monkeypatch.setattr(targets, "get_release", AsyncMock(return_value=value.source))
    if fault == "incompatible":
        monkeypatch.setattr(targets, "release_pair_compatible", lambda *_: False)
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def observe(node):
        return NodeUpgradeStatus(
            node=node,
            release_sha256=value.target.manifest_sha256,
            runtime_sha256=value.target.manifest.environment_sha256,
            bootstrap_ready=True,
            observed_at=now,
            retained_source_sha256=(
                "f" * 64 if fault == "identity" else value.source.manifest_sha256
            )
            if node == "coire-edge-a"
            else None,
            retained_job_id=parent.job_id if node == "coire-edge-a" else None,
        )

    client.upgrade_status.side_effect = observe
    monkeypatch.setattr(targets, "NodeClient", lambda *_args, **_kwargs: client)
    result = (await targets.target_statuses(session, Settings())).items[0]
    assert result.rollback_eligible is (fault is None)
    if fault is None:
        assert result.retained_sha256 == value.source.manifest_sha256
        assert result.rollback_job_id == parent.job_id
    elif fault == "identity":
        assert "retained_source_identity_differs" in result.blocking_reasons
    elif fault == "pending":
        assert "retained_operation_not_successful" in result.rollback_blocking_reasons
    else:
        assert "retained_source_incompatible" in result.rollback_blocking_reasons
