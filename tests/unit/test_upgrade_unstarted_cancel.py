"""Revoked pre-delivery authority closes only positively absent native effects."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    NodeUpgradeCancel,
    NodeUpgradeCommand,
    UpgradePhase,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_control import NativeUpgradeControl


@pytest.mark.parametrize("fault", [None, "preparation", "disk_hold", "fence", "source"])
def test_unstarted_cancel_is_durable_and_denies_delayed_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    plan, source, _ = native_plan(tmp_path)
    root = PrivateUpgradeStore(tmp_path / "upgrade-bootstrap")
    settings = Settings(
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
        upgrade_bootstrap_root=str(root.root),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    control = NativeUpgradeControl(settings)
    monkeypatch.setattr(
        control,
        "observed",
        Mock(
            return_value=source
            if fault != "source"
            else source.model_copy(update={"release_sha256": "f" * 64})
        ),
    )
    monkeypatch.setattr(control, "_guard_ready", lambda _: True)
    if fault != "fence":
        PrivateUpgradeStore(tmp_path / "state/upgrades").write(
            "maintenance.json", plan.maintenance.model_copy(update={"state": "active"})
        )
    if fault == "preparation":
        PrivateUpgradeStore(root.path("operations")).path(plan.operation.job_id).mkdir(mode=0o700)
    elif fault == "disk_hold":
        PrivateUpgradeStore(tmp_path / "envs/.upgrade-reservations").path(
            plan.operation.job_id
        ).mkdir(mode=0o700)
    command = NodeUpgradeCancel(
        generation=plan.operation.generation,
        reason="authorization_revoked",
        unstarted_plan=plan,
    )
    try:
        if fault is not None:
            with pytest.raises(UpgradeConflict):
                control.cancel_unstarted(plan.operation.job_id, command)
            assert not root.path("unstarted-" + plan.operation.job_id + ".json").exists()
            return
        status = control.cancel(plan.operation.job_id, command)
        assert status.phase is UpgradePhase.CANCELLED_UNCHANGED
        assert status.receipts[0].reason == "authorization_revoked"
        assert control.cancel(plan.operation.job_id, command).receipts == status.receipts
        assert control.status(plan.operation.job_id, 1).receipts == ()
        assert not root.path("operations").exists()
        late = NodeUpgradeCommand.model_validate(
            plan.model_dump()
            | {"authorization_expires_at": datetime.now(UTC) + timedelta(seconds=30)}
        )
        with pytest.raises(UpgradeConflict):
            control.accept(late)
        assert not root.path("operations").exists()
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
