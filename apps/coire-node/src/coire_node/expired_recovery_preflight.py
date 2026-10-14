"""Local transfer identity preflight only; no signals, writes or ownership claim."""

import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

import psutil
from opentelemetry import trace

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.models.upgrades import NativeGuardStatus, PreparedNativeRuntime
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_guard import _owned_native_process
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.recovery_preflight")


def verify_local_active_owner_identity(
    settings: Settings, claim: UpgradeExpiredRecoveryOwnerClaim
) -> Path:
    """Same live owner under captured recovery budget; fresh Core gate required.

    This never changes the initial transfer epoch or authorizes another process.
    """
    claim = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(claim.model_dump_json())
    return verify_local_successor_identity(
        settings, claim.consumption, claim.runtime, active_owner=claim
    )


def verify_local_successor_identity(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    runtime: PreparedNativeRuntime,
    *,
    active_owner: UpgradeExpiredRecoveryOwnerClaim | None = None,
) -> Path:
    """Verify live fixed-module successor without inferring legacy retirement.

    This is suitable after positive retirement, when requiring the old process
    alive would be incorrect. It alone grants no ownership or resource effect.
    """
    receipt = UpgradeExpiredRecoveryTransferConsumption.model_validate_json(
        receipt.model_dump_json()
    )
    transfer = receipt.transfer
    support = transfer.intent.support_release.manifest
    deadline = transfer.expires_at
    if active_owner is not None:
        if active_owner.consumption != receipt or active_owner.runtime != runtime:
            raise UpgradeUnavailable("Active owner differs from initial consumed successor")
        deadline = transfer.intent.deadline_at
    now = datetime.now(UTC)
    if (
        sys.platform != "darwin"
        or platform.machine() != "arm64"
        or platform.node().lower().split(".", 1)[0] != settings.node_name
        or settings.node_name != transfer.legacy_guard.node
        or not receipt.consumed_at <= now < deadline
        or (runtime.environment_id, runtime.environment_sha256)
        != (support.environment_id, support.environment_sha256)
        or os.getpid() != receipt.consumer.pid
    ):
        raise UpgradeUnavailable("Local recovery successor or epoch differs")
    try:
        if psutil.Process(os.getpid()).create_time() != receipt.consumer.create_time_s:
            raise UpgradeUnavailable("Local recovery successor birth differs")
        envs = Path(settings.upgrade_environment_root)
        python = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
        if (
            Path(sys.prefix).resolve(strict=True)
            != (envs / runtime.environment_id).resolve(strict=True)
            or Path(sys.executable).resolve(strict=True) != python.resolve(strict=True)
            or _owned_native_process(receipt.consumer, python, "coire_node.upgrade_guard") is None
            or datetime.now(UTC) >= deadline
        ):
            raise UpgradeUnavailable("Local recovery successor interpreter or module differs")
        return envs
    except (OSError, psutil.Error, ValueError):
        raise UpgradeUnavailable("Local recovery successor identity is uncertain") from None


def verify_local_transfer_preflight(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    runtime: PreparedNativeRuntime,
) -> None:
    """Requires separate fresh authenticated Core gate and launchd/exclusion proof.

    Successful preflight is not completed handover or serving qualification. Missing
    legacy process is uncertainty, never authorization to take its ownership.
    """
    with tracer.start_as_current_span("coire.node.recovery_transfer_preflight"):
        receipt = UpgradeExpiredRecoveryTransferConsumption.model_validate_json(
            receipt.model_dump_json()
        )
        transfer = receipt.transfer
        envs = verify_local_successor_identity(settings, receipt, runtime)
        now = datetime.now(UTC)
        try:
            legacy = transfer.legacy_guard
            old_python = verify_prepared_runtime(
                envs, envs.parent / "runtimes", legacy.bootstrap_runtime
            )
            current = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False).read(
                "status.json", NativeGuardStatus
            )
            if (
                current is None
                or (current.node, current.process, current.bootstrap_runtime, current.selected)
                != (legacy.node, legacy.process, legacy.bootstrap_runtime, legacy.selected)
                or not 0 <= (now - current.observed_at).total_seconds() <= 5
            ):
                raise UpgradeUnavailable("Local legacy guard observation differs")
            if (
                _owned_native_process(legacy.process, old_python, "coire_node.upgrade_guard")
                is None
            ):
                raise UpgradeUnavailable("Legacy guard absence does not authorize ownership")
        except (OSError, psutil.Error, ValueError):
            raise UpgradeUnavailable("Local recovery identity is uncertain") from None
