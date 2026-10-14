"""Preserve a positively retired fixture child that never published startup state."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_native_terminal import UpgradeNativeFixtureUnstartedChild
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeRuntimeSelection,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    UpgradeRelease,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_definition import read_definition_file
from coire_node.recovery_identity import verify_retired_identity
from coire_node.upgrade_inventory import read_owner_json
from coire_node.upgrade_runtime import verify_prepared_runtime


def capture_retired_unstarted_child(
    settings: Settings, job_id: str
) -> UpgradeNativeFixtureUnstartedChild:
    base = Path(settings.upgrade_bootstrap_root).parent
    if (
        Path(settings.node_state_dir) != base / "state"
        or Path(settings.upgrade_environment_root) != base / "envs"
    ):
        raise UpgradeConflict("Unstarted fixture roots differ")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    records = PrivateUpgradeStore(base / "fixture-records", create=False)
    operation = PrivateUpgradeStore(bootstrap.root / "operations" / job_id, create=False)
    native = PrivateUpgradeStore(operation.root / "guard", create=False).read(
        "intent.json", NativeRecoveryIntent
    )
    claim = bootstrap.read("claim.json", NativeUpgradeClaim)
    original = PrivateUpgradeStore(base / "state/upgrades", create=False).read(
        "node-process.json", NativeNodeProcess
    )
    status = bootstrap.read("status.json", NativeGuardStatus)
    pointer = PrivateUpgradeStore(operation.root / "pointer", create=False).read(
        "selection.json", NativeRuntimeSelection
    )
    children = read_owner_json(records.path("fixture-processes.json"))
    parent = read_owner_json(records.path("fixture-supervisor.json"))
    if native is None or claim is None or original is None or status is None or pointer is None:
        raise UpgradeConflict("Unstarted fixture retained records are absent")
    if (
        not isinstance(children, dict)
        or set(children) != {"guard", "node"}
        or not isinstance(parent, dict)
        or set(parent) != {"pid", "create_time"}
    ):
        raise UpgradeConflict("Unstarted fixture ownership is incomplete")
    child = children["node"]
    guard = children["guard"]
    if (
        not isinstance(child, dict)
        or set(child) != {"pid", "create_time"}
        or not isinstance(guard, dict)
        or set(guard) != {"pid", "create_time"}
    ):
        raise UpgradeConflict("Unstarted fixture child ownership differs")
    if (
        (native.job_id, native.generation, native.plan_sha256)
        != (job_id, claim.generation, claim.plan_sha256)
        or claim.job_id != job_id
        or claim.state != "active"
        or native.phase != "recovery_required"
        or native.activation_started_at is None
        or native.recovery_deadline_at is None
        or datetime.now(UTC) < native.recovery_deadline_at
        or original.process != native.source_process
        or original.selected.runtime != native.source.runtime
        or original.selected.release_sha256 != native.source.release_sha256
        or pointer.runtime != native.candidate
        or (pointer.job_id, pointer.generation) != (claim.job_id, claim.generation)
        or status.ready
        or (guard["pid"], guard["create_time"])
        != (status.process.pid, status.process.create_time_s)
        or (base / "envs/current").resolve(strict=True)
        != base / "envs" / native.candidate.environment_id
    ):
        raise UpgradeConflict("Unstarted fixture is not the exact expired candidate")
    source = records.read("source.json", UpgradeRelease)
    target = records.read("target.json", UpgradeRelease)
    if (
        source is None
        or target is None
        or source.manifest_sha256 != native.source.release_sha256
        or target.manifest_sha256 != pointer.release_sha256
    ):
        raise UpgradeConflict("Unstarted fixture prepared release inputs differ")
    candidate = NodeAgentProcessIdentity(
        pid=child["pid"],
        create_time_s=child["create_time"],
        environment_sha256=native.candidate.environment_sha256,
    )
    supervisor = NodeAgentProcessIdentity(
        pid=parent["pid"],
        create_time_s=parent["create_time"],
        environment_sha256=native.source.runtime.environment_sha256,
    )
    if (
        candidate == original.process
        or candidate.create_time_s
        < max(supervisor.create_time_s, native.activation_started_at.timestamp())
        or candidate.create_time_s > datetime.now(UTC).timestamp()
    ):
        raise UpgradeConflict("Unstarted fixture child birth differs")
    for identity in (original.process, candidate, supervisor, status.process):
        verify_retired_identity(identity)
    for runtime in (native.source.runtime, native.candidate):
        verify_prepared_runtime(base / "envs", base / "runtimes", runtime)
    definition = read_definition_file(base / "fixture-supervisor.py", os.getuid())
    proof = UpgradeNativeFixtureUnstartedChild(
        claim=claim,
        native=native,
        original_node=original,
        candidate_process=candidate,
        supervisor=supervisor,
        definition_sha256=hashlib.sha256(definition).hexdigest(),
        observed_at=datetime.now(UTC),
    )
    with (
        bootstrap.ownership("control.lock"),
        records.ownership("fixture-supervisor.lock"),
        bootstrap.ownership("lifetime.lock"),
    ):
        if (
            bootstrap.read("claim.json", NativeUpgradeClaim) != claim
            or PrivateUpgradeStore(operation.root / "guard", create=False).read(
                "intent.json", NativeRecoveryIntent
            )
            != native
            or PrivateUpgradeStore(base / "state/upgrades", create=False).read(
                "node-process.json", NativeNodeProcess
            )
            != original
            or bootstrap.read("status.json", NativeGuardStatus) != status
            or read_owner_json(records.path("fixture-processes.json")) != children
            or read_owner_json(records.path("fixture-supervisor.json")) != parent
            or hashlib.sha256(
                read_definition_file(base / "fixture-supervisor.py", os.getuid())
            ).hexdigest()
            != proof.definition_sha256
        ):
            raise UpgradeConflict("Unstarted fixture preimages changed during capture")
        for identity in (original.process, candidate, supervisor, status.process):
            verify_retired_identity(identity)
        key = "retired-unstarted-" + job_id + ".json"
        retained = records.read(key, UpgradeNativeFixtureUnstartedChild)
        if retained is None:
            records.write(key, proof, replace=False)
        elif retained.model_copy(update={"observed_at": proof.observed_at}) != proof:
            raise UpgradeConflict("Unstarted fixture retained provenance changed")
        else:
            proof = retained
    return proof
