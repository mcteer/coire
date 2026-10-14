"""Local preparation retains a consumed scope, not ownership or runtime effects."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerTransfer,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize("refused", [False, True])
def test_local_preparation_reauthorizes_and_retains_only_new_namespace(
    tmp_path, monkeypatch, refused
):
    from coire_node import expired_recovery_prepare as worker

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(),
        transfer=transfer,
        consumer=transfer.successor,
        consumed_at=transfer.issued_at,
    )
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    original = bootstrap.root / "original.json"
    original.write_text("retained-original")
    settings = Settings(node_name="coire-edge-a", upgrade_bootstrap_root=str(bootstrap.root))
    gate = Mock(
        side_effect=UpgradeUnavailable("fixture refused") if refused else None, return_value=receipt
    )
    control = SimpleNamespace(verify_recovery_consumption=gate)
    preflight = Mock()
    monkeypatch.setattr(worker, "verify_local_transfer_preflight", preflight)
    runtime = transfer.legacy_guard.selected.runtime
    if refused:
        with pytest.raises(UpgradeUnavailable):
            worker.prepare_local_transfer(settings, receipt, runtime, control)
        preflight.assert_not_called()
        assert not (bootstrap.root / "expired-recovery").exists()
    else:
        assert worker.prepare_local_transfer(settings, receipt, runtime, control) == (receipt, True)
        assert worker.prepare_local_transfer(settings, receipt, runtime, control) == (
            receipt,
            False,
        )
        preflight.assert_called_once()
        assert gate.call_count == 3
    assert original.read_text() == "retained-original"
