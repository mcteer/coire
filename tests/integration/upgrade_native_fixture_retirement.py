"""Retire positively owned acceptance children without changing native authority.

Only private development fixtures are eligible. The recorded adopted supervisor
keeps its canonical history; an immutable current-child snapshot supplies later
cold recovery provenance after ordinary upgrades have changed child PIDs.
"""

from __future__ import annotations

import hashlib
import os
import platform
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psutil

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_native_terminal import (
    UpgradeNativeFixtureRetirement,
    UpgradeNativeTerminalSupervisorReceipt,
)
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    UpgradeExecutionPlan,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_definition import read_definition_file
from coire_node.maintenance import REQUIRED_OWNERS
from coire_node.recovery_identity import verify_retired_identity
from coire_node.upgrade_guard import NativeGuardTransport, _owned_native_process
from coire_node.upgrade_runtime import verify_prepared_runtime


def retire_adopted_fixture(settings: Settings, job_id: str) -> UpgradeNativeFixtureRetirement:
    base = Path(settings.upgrade_bootstrap_root).parent
    if (
        platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or not base.is_absolute()
        or base.resolve(strict=True) != base
        or base.parent.name != "coire-019-acceptance"
        or not base.name.startswith("native-upgrade-lab-")
        or base.stat().st_uid != os.getuid()
        or base.stat().st_mode & 0o077
        or os.getuid() == 0
        or Path(settings.node_state_dir) != base / "state"
        or Path(settings.upgrade_environment_root) != base / "envs"
    ):
        raise UpgradeUnavailable("Fixture retirement requires its owned isolated native root")
    bootstrap = PrivateUpgradeStore(base / "upgrade-bootstrap", create=False)
    records = PrivateUpgradeStore(base / "fixture-records", create=False)
    state = PrivateUpgradeStore(base / "state/upgrades", create=False)
    operation = PrivateUpgradeStore(bootstrap.root / "operations" / job_id, create=False)
    guard_store = PrivateUpgradeStore(operation.root / "guard", create=False)
    plan = operation.read("plan.json", UpgradeExecutionPlan)
    claim = bootstrap.read("claim.json", NativeUpgradeClaim)
    native = guard_store.read("intent.json", NativeRecoveryIntent)
    guard = bootstrap.read("status.json", NativeGuardStatus)
    node = state.read("node-process.json", NativeNodeProcess)
    if any(value is None for value in (plan, claim, native, guard, node)):
        raise UpgradeUnavailable("Fixture retirement original records are incomplete")
    assert plan is not None and claim is not None and native is not None
    assert guard is not None and node is not None
    if (
        claim.job_id != job_id
        or claim.plan_sha256 != plan.authority_sha256()
        or settings.node_name != plan.operation.node
    ):
        raise UpgradeConflict("Fixture retirement belongs to another accepted operation")
    key = "retired-adopted-" + job_id + ".json"
    retained = records.read(key, UpgradeNativeFixtureRetirement)
    if retained is not None and retained.retired_at is not None:
        for identity in (
            retained.adoption.process,
            retained.guard.process,
            retained.node_process.process,
        ):
            verify_retired_identity(identity)
        return retained
    if retained is None:
        roots = list((bootstrap.root / "expired-recovery").iterdir())
        if len(roots) > 128:
            raise UpgradeUnavailable("Fixture adoption history exceeds its bound")
        matches = []
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                raise UpgradeUnavailable("Fixture adoption history is foreign")
            receipt = PrivateUpgradeStore(root, create=False).read(
                "native-terminal.supervisor.receipt.json", UpgradeNativeTerminalSupervisorReceipt
            )
            if receipt is None:
                continue
            python = verify_prepared_runtime(
                base / "envs", base / "runtimes", receipt.intent.runtime
            )
            process = _owned_native_process(
                receipt.process, python, "coire_node.upgrade_native_terminal_supervisor"
            )
            if process is not None:
                lock = records.path("fixture-supervisor.lock")
                if not any(Path(item.path) == lock for item in process.open_files()):
                    raise UpgradeUnavailable("Adopted fixture owner has no inherited restart lock")
                matches.append(receipt)
        if len(matches) != 1:
            raise UpgradeUnavailable("Fixture retirement requires one exact live adopted owner")
        definition = read_definition_file(base / "fixture-supervisor.py", os.getuid())
        with httpx.Client(trust_env=False, timeout=5) as client:
            inventory = NativeGuardTransport(settings, client).inventory(
                plan,
                native.source.runtime.environment_sha256,
                deadline_at=datetime.now(UTC) + timedelta(seconds=5),
            )
        if not inventory.idle(REQUIRED_OWNERS):
            raise UpgradeUnavailable("Fixture retirement has active or unknown Source work")
        retained = UpgradeNativeFixtureRetirement(
            claim=claim,
            native=native,
            guard=guard,
            node_process=node,
            adoption=matches[0],
            definition_sha256=hashlib.sha256(definition).hexdigest(),
            observed_at=datetime.now(UTC),
        )
        records.write(key, retained, replace=False)
    targets = [
        (
            "supervisor",
            retained.adoption.process,
            retained.adoption.intent.runtime,
            "coire_node.upgrade_native_terminal_supervisor",
        ),
        (
            "guard",
            retained.guard.process,
            retained.guard.bootstrap_runtime,
            "coire_node.upgrade_guard",
        ),
        (
            "node",
            retained.node_process.process,
            retained.node_process.selected.runtime,
            "coire_node",
        ),
    ]
    for role, identity, runtime, module in targets:
        python = verify_prepared_runtime(base / "envs", base / "runtimes", runtime)
        process = _owned_native_process(identity, python, module)
        marker = "fixture-retirement-" + job_id + "-" + role + ".json"
        if process is not None:
            if records.read(marker, NodeAgentProcessIdentity) is not None:
                raise UpgradeUnavailable("Fixture retirement signal needs positive reconciliation")
            records.write(marker, identity, replace=False)
            process.terminate()
            try:
                process.wait(timeout=15)
            except psutil.TimeoutExpired:
                raise UpgradeUnavailable("Owned fixture child has not retired") from None
        verify_retired_identity(identity)
    with records.ownership("fixture-supervisor.lock"), bootstrap.ownership("lifetime.lock"):
        if (
            bootstrap.read("claim.json", NativeUpgradeClaim) != retained.claim
            or guard_store.read("intent.json", NativeRecoveryIntent) != retained.native
            or state.read("node-process.json", NativeNodeProcess) != retained.node_process
        ):
            raise UpgradeConflict("Fixture retirement original authority changed")
        completed = retained.model_copy(update={"retired_at": datetime.now(UTC)})
        records.write(key, completed)
        return completed
