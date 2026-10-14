"""Exact no-effect private provenance and positive actor retirement, no signals."""

from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_retained_source import continuation_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoverySupportLaunchIntent,
    UpgradeExpiredRecoverySupportProcess,
    UpgradeExpiredRecoverySupportStartup,
)
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault", [None, "effect", "launcher_live", "support_live", "foreign_receipt", "missing_launch"]
)
def test_unstarted_custody_observes_only_exact_retired_no_effect_namespace(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_unstarted_lineage as worker

    _, _, _, settings, owner, bootstrap, _, _, _, _ = continuation_fixture(tmp_path, monkeypatch)
    intent = owner.intent.model_copy(update={"recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAZ"})
    run = PrivateUpgradeStore(bootstrap.path("expired-recovery") / intent.recovery_id)
    startup = UpgradeExpiredRecoverySupportStartup(
        intent=intent, runtime=owner.runtime, prepared_at=intent.accepted_at
    )
    launcher = owner.process.model_copy(update={"pid": owner.process.pid + 1})
    launch = UpgradeExpiredRecoverySupportLaunchIntent(
        startup=startup, launcher=launcher, started_at=intent.accepted_at
    )
    receipt = UpgradeExpiredRecoverySupportProcess(
        intent=intent, runtime=owner.runtime, process=owner.process, observed_at=intent.accepted_at
    )
    run.write("startup.json", startup)
    run.write("support.launch.intent.json", launch)
    run.write("support.launch.receipt.json", receipt)
    if fault == "effect":
        run.write("cold.claim.json", owner)
    elif fault == "foreign_receipt":
        run.write(
            "support.launch.receipt.json", receipt.model_copy(update={"intent": owner.intent})
        )
    elif fault == "missing_launch":
        run.path("support.launch.intent.json").unlink()

    def retired(identity):
        if (fault == "launcher_live" and identity == launcher) or (
            fault == "support_live" and identity == owner.process
        ):
            raise UpgradeUnavailable("recorded actor still live")

    checked = Mock(side_effect=retired)
    monkeypatch.setattr(worker, "verify_retired_identity", checked)
    with run.ownership("support.launch.lock"):
        pass
    before = {p.name: p.read_bytes() for p in run.root.iterdir()}
    if fault is not None:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            worker.observe_unstarted_custodian(settings, intent)
    else:
        assert worker.observe_unstarted_custodian(settings, intent) == receipt
        assert checked.call_args_list[0].args == (launcher,)
        assert checked.call_args_list[1].args == (owner.process,)
    assert {p.name: p.read_bytes() for p in run.root.iterdir()} == before
