"""Preflight proves identities but never signals or claims from process absence."""

import platform
from types import SimpleNamespace
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


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing_legacy",
        "foreign_successor",
        "wrong_module",
        "physical_core",
        "wrong_studio",
        "architecture",
    ],
)
def test_local_preflight_has_no_owner_effects(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_preflight as worker

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(),
        transfer=transfer,
        consumer=transfer.successor,
        consumed_at=transfer.issued_at,
    )
    guard = transfer.legacy_guard
    root = PrivateUpgradeStore(tmp_path / "bootstrap")
    root.write("status.json", guard)
    before = (root.root / "status.json").read_bytes()
    envs = tmp_path / "envs"
    environment = envs / transfer.intent.support_release.manifest.environment_id
    settings = Settings(
        node_name="coire-edge-a",
        upgrade_environment_root=str(envs),
        upgrade_bootstrap_root=str(root.root),
    )
    monkeypatch.setattr(
        worker,
        "verify_prepared_runtime",
        lambda envs, runtimes, expected: envs / expected.environment_id / "bin/python3",
    )
    monkeypatch.setattr(
        worker,
        "sys",
        SimpleNamespace(
            platform="darwin", prefix=str(environment), executable=str(environment / "bin/python3")
        ),
    )
    monkeypatch.setattr(
        worker,
        "os",
        SimpleNamespace(
            getpid=lambda: receipt.consumer.pid + (1 if fault == "foreign_successor" else 0)
        ),
    )
    monkeypatch.setattr(
        worker,
        "psutil",
        SimpleNamespace(
            Error=RuntimeError,
            Process=lambda pid: SimpleNamespace(create_time=lambda: receipt.consumer.create_time_s),
        ),
    )
    monkeypatch.setattr(
        worker,
        "_owned_native_process",
        lambda identity, *args: (
            None
            if (fault == "missing_legacy" and identity == guard.process)
            or (fault == "wrong_module" and identity == receipt.consumer)
            else object()
        ),
    )
    monkeypatch.setattr(
        platform,
        "node",
        lambda: (
            "coire-core"
            if fault == "physical_core"
            else ("coire-edge-b" if fault == "wrong_studio" else "coire-edge-a.lab")
        ),
    )
    monkeypatch.setattr(
        platform, "machine", lambda: "x86_64" if fault == "architecture" else "arm64"
    )
    runtime = guard.selected.runtime.model_copy(
        update={
            "environment_id": environment.name,
            "environment_sha256": receipt.consumer.environment_sha256,
        }
    )
    if fault:
        with pytest.raises(UpgradeUnavailable):
            worker.verify_local_transfer_preflight(settings, receipt, runtime)
    else:
        worker.verify_local_transfer_preflight(settings, receipt, runtime)
    if fault is None:
        from datetime import timedelta

        from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim

        active_root = tmp_path / "active"
        claim = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(active_root))
        active_envs = active_root / "envs"
        settings = settings.model_copy(update={"upgrade_environment_root": str(active_envs)})
        monkeypatch.setattr(worker.sys, "prefix", str(active_envs / claim.runtime.environment_id))
        monkeypatch.setattr(
            worker.sys,
            "executable",
            str(active_envs / claim.runtime.environment_id / "bin/python3"),
        )
        monkeypatch.setattr(
            worker,
            "datetime",
            SimpleNamespace(
                now=lambda zone: claim.consumption.transfer.expires_at + timedelta(seconds=1)
            ),
        )
        assert worker.verify_local_active_owner_identity(settings, claim) == active_envs
        with pytest.raises(UpgradeUnavailable):
            worker.verify_local_successor_identity(settings, claim.consumption, claim.runtime)
    if fault == "missing_legacy":
        # A positive successor check after retirement must not require the old
        # process alive, nor treat its absence as ownership permission.
        assert worker.verify_local_successor_identity(settings, receipt, runtime) == envs
    assert before == (root.root / "status.json").read_bytes()
