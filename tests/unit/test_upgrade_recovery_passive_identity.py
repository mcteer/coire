"""Physical passive actor checks, no paths/ports/models or ownership adoption."""

import platform
from types import SimpleNamespace

import pytest
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerTransfer
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault", [None, "module", "core", "physical_core", "wrong_studio", "architecture"]
)
def test_passive_support_identity_requires_exact_interpreter_module_node(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_support as worker

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    support = transfer.intent.support_release.manifest
    runtime = transfer.legacy_guard.bootstrap_runtime.model_copy(
        update={
            "environment_id": support.environment_id,
            "environment_sha256": support.environment_sha256,
        }
    )
    envs = tmp_path / "envs"
    python = envs / runtime.environment_id / "bin/python3"
    settings = Settings(
        node_name="coire-core" if fault == "core" else "coire-edge-a",
        upgrades_enabled=True,
        upgrade_environment_root=str(envs),
    )
    monkeypatch.setattr(
        worker,
        "sys",
        SimpleNamespace(
            platform="darwin", prefix=str(python.parent.parent), executable=str(python)
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
    monkeypatch.setattr(worker, "os", SimpleNamespace(getpid=lambda: transfer.successor.pid))
    monkeypatch.setattr(
        worker,
        "psutil",
        SimpleNamespace(
            Error=RuntimeError,
            Process=lambda pid: SimpleNamespace(
                create_time=lambda: transfer.successor.create_time_s
            ),
        ),
    )
    monkeypatch.setattr(worker, "verify_prepared_runtime", lambda *args: python)
    monkeypatch.setattr(
        worker, "_owned_native_process", lambda *args: None if fault == "module" else object()
    )
    if fault:
        with pytest.raises(UpgradeUnavailable):
            worker.verify_passive_support_identity(settings, transfer.intent, runtime)
    else:
        assert (
            worker.verify_passive_support_identity(settings, transfer.intent, runtime)
            == transfer.successor
        )
