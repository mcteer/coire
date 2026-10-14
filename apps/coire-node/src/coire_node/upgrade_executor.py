"""Owned native upgrade staging resources; user assets are never staging collateral."""

from __future__ import annotations

import fcntl
import logging
import os
import platform
import shutil
import stat
import struct
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from opentelemetry import metrics, trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeRuntimeSelection,
    NodeUpgradeCommand,
    NodeUpgradeInventory,
    NodeUpgradeStatus,
    PreparedNativeRuntime,
    UpgradeDiskHold,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMaintenance,
    UpgradeReleaseRegister,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import verify_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore

if TYPE_CHECKING:
    from coire_node.upgrade_guard import NativeGuardJournal

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.node.upgrade_executor")
reserved_bytes = metrics.get_meter("coire.node.upgrade_executor").create_gauge(
    "coire_upgrade_disk_reserved_bytes"
)


def activate_native_candidate(
    settings: Settings,
    journal: NativeGuardJournal,
    inventory: NodeUpgradeInventory,
    peer_inventory: NodeUpgradeInventory | None = None,
    *,
    refresh_inventory: Callable[[], NodeUpgradeInventory] | None = None,
    refresh_peer_inventory: Callable[[], NodeUpgradeInventory] | None = None,
) -> NativeRecoveryIntent:
    """Perform one recorded native selection after positive fenced workload drain.

    The independent guard calls this; HTTP handlers only deliver accepted authority.
    A failed or interrupted effect keeps activation intent and maintenance owned.
    """
    from coire_node import upgrade_guard, upgrade_runtime
    from coire_node.maintenance import REQUIRED_OWNERS

    plan = journal.plan
    operation = plan.operation
    if (
        settings.node_name != operation.node
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or sys.platform != "darwin"
        or platform.machine() != "arm64"
    ):
        raise UpgradeUnavailable("Native activation requires the intended Studio")
    with (
        tracer.start_as_current_span(
            "coire.node.upgrade_activate", record_exception=False, set_status_on_exception=False
        ),
        journal.store.ownership("intent.lock"),
    ):
        current = journal.current()
        if current.phase not in {"activation_intent", "candidate_selected"}:
            raise UpgradeUnavailable("Native activation intent is unavailable")
        command = journal.store.read("activation.json", NodeUpgradeCommand)
        if (
            command is None
            or command.activation_grant is None
            or command.activation_receipt is None
            or (
                command.authority_sha256() != current.plan_sha256
                or command.activation_grant.grant_id != current.activation_grant_id
            )
        ):
            raise UpgradeUnavailable("Native activation lacks consumed authority")
        receipts = journal.receipts.page(command.activation_receipt.sequence - 1)
        if not receipts or receipts[0] != command.activation_receipt:
            raise UpgradeUnavailable("Native activation intent receipt is unreconciled")
        if command.peer_inventory is not None:
            if peer_inventory is not None and peer_inventory != command.peer_inventory:
                raise UpgradeConflict("Native activation peer differs from consumed authority")
            peer_inventory = command.peer_inventory
        deadline = current.readiness_deadline_at
        if deadline is None or datetime.now(UTC) >= deadline:
            raise UpgradeUnavailable("Native activation readiness deadline elapsed")
        envs = Path(settings.upgrade_environment_root)
        interpreters = envs.parent / "runtimes"
        source_python = upgrade_runtime.verify_prepared_runtime(
            envs, interpreters, current.source.runtime
        )
        upgrade_runtime.verify_prepared_runtime(envs, interpreters, current.candidate)
        pointer = envs / "current"
        if current.phase == "candidate_selected":
            if not pointer.is_symlink() or pointer.resolve(strict=True) != (
                envs / current.candidate.environment_id
            ).resolve(strict=True):
                raise UpgradeUnavailable("Native selected candidate reality differs")
            return current
        # Full graphs are verified before fetching effect-boundary observations.
        # Hashing a large retained runtime must not make a prior inventory stale.
        if refresh_inventory is not None:
            inventory = refresh_inventory()
        if refresh_peer_inventory is not None:
            peer_inventory = refresh_peer_inventory()
        node_store = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")

        def check_authority(*, source_stopped: bool = False) -> None:
            now = datetime.now(UTC)
            fence = node_store.read("maintenance.json", UpgradeMaintenance)
            if fence is None or not fence.matches_plan(plan) or operation.node not in fence.targets:
                raise UpgradeUnavailable("Native activation maintenance epoch differs")
            if (
                (inventory.job_id, inventory.generation, inventory.node, inventory.runtime_sha256)
                != (
                    operation.job_id,
                    operation.generation,
                    operation.node,
                    current.source.runtime.environment_sha256,
                )
                or (
                    not source_stopped
                    and not 0 <= (now - inventory.observed_at).total_seconds() <= 5
                )
                or not inventory.idle(REQUIRED_OWNERS)
            ):
                raise UpgradeUnavailable("Native activation requires fresh positive workload drain")
            if plan.target.manifest.paired_required or plan.peer_runtime_sha256 is not None:
                peer = "coire-edge-b" if operation.node == "coire-edge-a" else "coire-edge-a"
                if (
                    peer_inventory is None
                    or peer not in fence.targets
                    or (
                        peer_inventory.job_id,
                        peer_inventory.generation,
                        peer_inventory.node,
                        peer_inventory.runtime_sha256,
                    )
                    != (operation.job_id, operation.generation, peer, plan.peer_runtime_sha256)
                    or not 0 <= (now - peer_inventory.observed_at).total_seconds() <= 5
                    or not peer_inventory.idle(REQUIRED_OWNERS)
                ):
                    raise UpgradeUnavailable("Native activation requires a fresh fenced peer")
            if now >= deadline:
                raise UpgradeUnavailable("Native activation readiness deadline elapsed")

        check_authority()
        journal.store.write("activation-source-inventory.json", inventory)
        if peer_inventory is not None:
            journal.store.write("activation-peer-inventory.json", peer_inventory)
        source_record = node_store.read("node-process.json", NativeNodeProcess)
        if (
            source_record is None
            or source_record.process != current.source_process
            or (
                source_record.selected.node != current.source.node
                or source_record.selected.runtime != current.source.runtime
                or source_record.selected.release_sha256 != current.source.release_sha256
            )
        ):
            raise UpgradeUnavailable("Native activation source process registration changed")
        if not pointer.is_symlink() or pointer.resolve(strict=True) != (
            envs / current.source.runtime.environment_id
        ).resolve(strict=True):
            raise UpgradeUnavailable("Native activation source selection requires reconciliation")
        if not upgrade_guard.stop_owned_node_process(
            current.source_process, source_python, deadline
        ):
            raise UpgradeUnavailable("Native activation source stop remains unconfirmed")
        if refresh_peer_inventory is not None:
            peer_inventory = refresh_peer_inventory()
            journal.store.write("activation-peer-inventory.json", peer_inventory)
        # The positively stopped source cannot admit work. Its last idle snapshot
        # remains bound by the unchanged persistent fence; a required peer stays live
        # and must still supply a fresh observation after the stop.
        check_authority(source_stopped=True)
        selected = NativeRuntimeSelection(
            node=current.source.node,
            release_sha256=plan.target.manifest_sha256,
            runtime=current.candidate,
            job_id=operation.job_id,
            generation=operation.generation,
            selected_at=datetime.now(UTC),
        )
        selection_store = PrivateUpgradeStore(journal.root.root / "pointer")
        selection_store.write("selection.json", selected)

        def final_boundary() -> None:
            nonlocal peer_inventory
            if refresh_peer_inventory is not None:
                peer_inventory = refresh_peer_inventory()
                journal.store.write("activation-peer-inventory.json", peer_inventory)
            check_authority(source_stopped=True)

        upgrade_runtime.reconcile_native_selection(
            envs, interpreters, selection_store.root, selected.node, before_replace=final_boundary
        )
        value = NativeRecoveryIntent.model_validate(
            current.model_dump(mode="json")
            | {
                "phase": "candidate_selected",
                "updated_at": datetime.now(UTC),
            }
        )
        journal.store.write("intent.json", value)
        journal._record("candidate_selected")
        return value


def restore_native_source(
    settings: Settings,
    journal: NativeGuardJournal,
    candidate_inventory: NodeUpgradeInventory | None = None,
    *,
    refresh_candidate_inventory: Callable[[], NodeUpgradeInventory] | None = None,
) -> NativeRecoveryIntent:
    """Restore the retained source; only a positively idle, exact candidate may be stopped.

    Recovery needs no renewed human grant, never alters data, and keeps maintenance
    closed until subsequent source health/generation and cleanup verification.
    """
    from coire_node import upgrade_guard, upgrade_runtime
    from coire_node.maintenance import REQUIRED_OWNERS

    operation = journal.plan.operation
    if (
        settings.node_name != operation.node
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or sys.platform != "darwin"
        or platform.machine() != "arm64"
    ):
        raise UpgradeUnavailable("Native restoration requires the intended Studio")
    with (
        tracer.start_as_current_span(
            "coire.node.upgrade_restore", record_exception=False, set_status_on_exception=False
        ),
        journal.store.ownership("intent.lock"),
    ):
        current = journal.current()
        if (
            current.phase not in {"restoring", "source_selected", "recovery_required"}
            or current.recovery_deadline_at is None
        ):
            raise UpgradeUnavailable("Native restoration requires a captured recovery intent")
        envs = Path(settings.upgrade_environment_root)
        interpreters = envs.parent / "runtimes"
        upgrade_runtime.verify_prepared_runtime(envs, interpreters, current.source.runtime)
        pointer = envs / "current"
        if current.phase == "source_selected":
            if not pointer.is_symlink() or pointer.resolve(strict=True) != (
                envs / current.source.runtime.environment_id
            ).resolve(strict=True):
                raise UpgradeUnavailable("Native restored source selection differs")
            return current
        if datetime.now(UTC) >= current.recovery_deadline_at:
            raise UpgradeUnavailable("Native source restoration deadline elapsed")
        recovery_deadline = current.recovery_deadline_at
        node_store = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")
        fence = node_store.read("maintenance.json", UpgradeMaintenance)
        if (
            fence is None
            or not fence.matches_plan(journal.plan)
            or operation.node not in fence.targets
        ):
            raise UpgradeUnavailable("Native restoration maintenance ownership differs")
        record = node_store.read("node-process.json", NativeNodeProcess)
        if record is None:
            raise UpgradeUnavailable("Native restoration node process ownership is unknown")
        source = (
            record.selected.runtime == current.source.runtime
            and record.selected.release_sha256 == current.source.release_sha256
        )
        candidate = (
            record.selected.runtime == current.candidate
            and record.selected.release_sha256 == journal.plan.target.manifest_sha256
        )
        if record.selected.node != operation.node or not (source or candidate):
            raise UpgradeUnavailable("Native restoration has an unrelated node process")
        if candidate:
            if (record.selected.job_id, record.selected.generation) != (
                operation.job_id,
                operation.generation,
            ):
                raise UpgradeUnavailable("Native candidate process belongs to another operation")
            # Restoration must survive a broken candidate package graph. The
            # retained interpreter path and exact PID birth still bind the stop.
            python = interpreters / current.candidate.interpreter_id / "bin/coire-node-python"
            alive = upgrade_guard._owned_node_process(record.process, python)
            if alive is not None:
                interpreter = interpreters / current.candidate.interpreter_id
                details = interpreter.lstat()
                if (
                    not stat.S_ISDIR(details.st_mode)
                    or details.st_uid != os.getuid()
                    or details.st_mode & 0o277
                    or not python.resolve(strict=True).is_relative_to(
                        interpreter.resolve(strict=True)
                    )
                ):
                    raise UpgradeUnavailable("Native candidate interpreter ownership differs")
                now = datetime.now(UTC)
                if refresh_candidate_inventory is not None:
                    candidate_inventory = refresh_candidate_inventory()
                    now = datetime.now(UTC)
                if (
                    candidate_inventory is None
                    or (
                        candidate_inventory.job_id,
                        candidate_inventory.generation,
                        candidate_inventory.node,
                        candidate_inventory.runtime_sha256,
                    )
                    != (
                        operation.job_id,
                        operation.generation,
                        operation.node,
                        current.candidate.environment_sha256,
                    )
                    or not 0 <= (now - candidate_inventory.observed_at).total_seconds() <= 5
                    or not candidate_inventory.idle(REQUIRED_OWNERS)
                ):
                    raise UpgradeUnavailable(
                        "Native candidate restoration stop lacks positive drain"
                    )
                if not upgrade_guard.stop_owned_node_process(
                    record.process, python, current.recovery_deadline_at
                ):
                    raise UpgradeUnavailable("Native candidate restoration stop remains uncertain")
        else:
            python = upgrade_runtime.verify_prepared_runtime(
                envs, interpreters, current.source.runtime
            )
            # A healthy source stays running. Reading its exact identity never signals it.
            upgrade_guard._owned_node_process(record.process, python)

        def restoration_boundary() -> None:
            latest_fence = node_store.read("maintenance.json", UpgradeMaintenance)
            if (
                latest_fence is None
                or not latest_fence.matches_plan(journal.plan)
                or datetime.now(UTC) >= recovery_deadline
                or journal.current() != current
            ):
                raise UpgradeUnavailable("Native restoration ownership or deadline changed")

        restoration_boundary()
        selected = NativeRuntimeSelection(
            node=current.source.node,
            release_sha256=current.source.release_sha256,
            runtime=current.source.runtime,
            job_id=operation.job_id,
            generation=operation.generation,
            selected_at=datetime.now(UTC),
        )
        selection_store = PrivateUpgradeStore(journal.root.root / "pointer")
        selection_store.write("selection.json", selected)
        upgrade_runtime.reconcile_native_selection(
            envs,
            interpreters,
            selection_store.root,
            selected.node,
            before_replace=restoration_boundary,
        )
        value = NativeRecoveryIntent.model_validate(
            current.model_dump(mode="json")
            | {"phase": "source_selected", "updated_at": datetime.now(UTC)}
        )
        journal.store.write("intent.json", value)
        journal._record("source_selected")
        return value


def prepare_native_candidate(
    settings: Settings,
    plan: UpgradeExecutionPlan,
    source_status: NodeUpgradeStatus,
    inventory: NodeUpgradeInventory,
) -> PreparedNativeRuntime:
    """Verify operator-staged graphs without selecting them or releasing any fence.

    Status and inventory must come from the authenticated source agent. The
    caller must refresh inventory again before requesting activation authority.
    """
    from coire_node import upgrade_runtime
    from coire_node.maintenance import REQUIRED_OWNERS

    operation = plan.operation
    source = plan.source.manifest
    now = datetime.now(UTC)
    if (
        operation.kind is not UpgradeKind.STUDIO
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or settings.node_name != operation.node
        or sys.platform != "darwin"
        or platform.machine() != "arm64"
        or not source_status.bootstrap_ready
        or source_status.node != operation.node
        or source_status.release_sha256 != plan.source.manifest_sha256
        or source_status.runtime_sha256 != source.environment_sha256
        or source_status.environment_id != source.environment_id
        or not 0 <= (now - source_status.observed_at).total_seconds() <= 5
        or (inventory.job_id, inventory.generation, inventory.node, inventory.runtime_sha256)
        != (operation.job_id, operation.generation, operation.node, source.environment_sha256)
        or not 0 <= (now - inventory.observed_at).total_seconds() <= 5
        or not inventory.idle(REQUIRED_OWNERS)
    ):
        raise UpgradeUnavailable("Native candidate requires a fresh, ready, idle Studio source")

    fence_store = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades")

    def verify_fence() -> None:
        fence = fence_store.read("maintenance.json", UpgradeMaintenance)
        if (
            fence is None
            or not fence.matches_plan(plan)
            or operation.node not in fence.targets
            or fence.state == "released"
        ):
            raise UpgradeUnavailable("Native preparation maintenance ownership is unavailable")

    with tracer.start_as_current_span(
        "coire.node.upgrade_prepare", record_exception=False, set_status_on_exception=False
    ):
        verify_fence()
        environments = Path(settings.upgrade_environment_root)
        interpreters = environments.parent / "runtimes"
        identities = PrivateUpgradeStore(environments / ".identities")
        runtimes: list[PreparedNativeRuntime] = []
        for release in (plan.source, plan.target):
            catalog_release = verify_prepared_release(
                Path(settings.upgrade_prepared_dir),
                UpgradeReleaseRegister(
                    prepared_artifact_id=release.prepared_artifact_id,
                    expected_sha256=release.manifest_sha256,
                ),
            )
            if catalog_release != release:
                raise UpgradeConflict("Native prepared release authority differs")
            environment_id = release.manifest.environment_id
            if environment_id is None:
                raise UpgradeUnavailable("Native release lacks a prepared environment")
            runtime = identities.read(environment_id + ".json", PreparedNativeRuntime)
            if (
                runtime is None
                or runtime.environment_id != environment_id
                or runtime.environment_sha256 != release.manifest.environment_sha256
            ):
                raise UpgradeUnavailable("Native prepared environment identity differs")
            upgrade_runtime.verify_prepared_runtime(environments, interpreters, runtime)
            runtimes.append(runtime)
        candidate = runtimes[1]
        verify_fence()
        # Create the catalog itself privately before its per-operation store.
        # mkdir(parents=True) on the leaf otherwise creates this parent with
        # the process umask, which independent unclaimed-resource checks reject.
        reservation_catalog = PrivateUpgradeStore(environments / ".upgrade-reservations")
        ledger = NativeDiskReservation(reservation_catalog.path(operation.job_id), plan)
        held = ledger.reserve(
            plan.target.manifest.staged_bytes + settings.upgrade_disk_margin_bytes
        )
        if held.state != "held":
            raise UpgradeUnavailable("Native staging reservation is no longer active")
        verify_fence()
        try:
            upgrade_runtime.smoke(environments / candidate.environment_id / "bin/python3")
        except (OSError, subprocess.SubprocessError):
            raise UpgradeUnavailable("Native candidate import verification failed") from None
        for runtime in runtimes:
            upgrade_runtime.verify_prepared_runtime(environments, interpreters, runtime)
        verify_fence()
        logger.info(
            "native upgrade candidate prepared",
            extra={
                "job_id": operation.job_id,
                "generation": operation.generation,
                "node": operation.node,
            },
        )
        return candidate


def preallocate_disk(descriptor: int, required_bytes: int) -> int:
    """Reserve real extents, then persist their logical size; never claim sparse bytes."""
    if sys.platform == "darwin":
        # Darwin sys/fcntl.h: F_PREALLOCATE=42, F_ALLOCATEALL=4,
        # F_PEOFPOSMODE=3; fstore_t is unsigned-int, int, three off_t fields.
        request = struct.pack("@Iiqqq", 4, 3, 0, required_bytes, 0)
        result = fcntl.fcntl(descriptor, 42, request)
        if struct.unpack("@Iiqqq", result)[4] < required_bytes:
            raise UpgradeUnavailable("Native staging space was not fully allocated")
    elif sys.platform == "linux":
        os.posix_fallocate(descriptor, 0, required_bytes)
    else:
        raise UpgradeUnavailable("Native staging allocation is unsupported")
    os.ftruncate(descriptor, required_bytes)
    os.fsync(descriptor)
    allocated = os.fstat(descriptor).st_blocks * 512
    if allocated < required_bytes:
        raise UpgradeUnavailable("Native staging reservation has sparse allocation")
    return allocated


class NativeDiskReservation:
    def __init__(self, root: Path, plan: UpgradeExecutionPlan) -> None:
        if plan.operation.kind is not UpgradeKind.STUDIO:
            raise UpgradeConflict("Native staging requires a Studio operation")
        self.plan = plan
        self.store = PrivateUpgradeStore(root)

    def current(self) -> UpgradeDiskHold | None:
        value = self.store.read("hold.json", UpgradeDiskHold)
        if value is not None and (value.job_id, value.generation, value.plan_sha256) != (
            self.plan.operation.job_id,
            self.plan.operation.generation,
            self.plan.authority_sha256(),
        ):
            raise UpgradeConflict("Native staging reservation belongs to another operation")
        return value

    def _file(self, hold: UpgradeDiskHold) -> os.stat_result | None:
        if any(
            path.name not in {"hold.json", "space.lock", hold.resource_key}
            for path in self.store.root.iterdir()
        ):
            raise UpgradeUnavailable("Native staging contains unreconciled data")
        try:
            details = self.store.path(hold.resource_key).lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise UpgradeUnavailable("Native staging file cannot be inspected safely") from None
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or details.st_nlink != 1
            or (details.st_dev, details.st_ino) != (hold.file_device, hold.file_inode)
        ):
            raise UpgradeUnavailable("Native staging file ownership is uncertain")
        return details

    def _record(self, hold: UpgradeDiskHold) -> None:
        self.store.write("hold.json", hold)
        reserved_bytes.set(
            max(hold.requested_bytes, hold.allocated_bytes) if hold.state != "released" else 0,
            {"node": self.plan.operation.node or "unknown"},
        )
        logger.info(
            "native upgrade disk ownership persisted",
            extra={
                "job_id": hold.job_id,
                "generation": hold.generation,
                "node": self.plan.operation.node,
                "state": hold.state,
            },
        )

    def reserve(self, requested_bytes: int) -> UpgradeDiskHold:
        with (
            tracer.start_as_current_span(
                "coire.node.upgrade_disk_reserve",
                record_exception=False,
                set_status_on_exception=False,
            ),
            self.store.ownership("space.lock"),
        ):
            previous = self.current()
            if previous is not None:
                if previous.requested_bytes != requested_bytes:
                    raise UpgradeConflict("Native staging byte reservation changed on replay")
                if previous.state == "releasing":
                    raise UpgradeConflict("Native staging cleanup cannot become a new allocation")
                details = self._file(previous)
                if previous.state == "released":
                    if details is not None:
                        raise UpgradeUnavailable("Released native staging has unreconciled files")
                    return previous
                if (
                    details is None
                    or details.st_size != requested_bytes
                    or details.st_blocks * 512 < requested_bytes
                ):
                    raise UpgradeUnavailable("Native staging allocation needs owned reconciliation")
                if previous.state == "held":
                    return previous
                restored = UpgradeDiskHold.model_validate(
                    previous.model_dump(mode="json")
                    | {
                        "state": "held",
                        "allocated_bytes": details.st_blocks * 512,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self._record(restored)
                return restored
            if shutil.disk_usage(self.store.root).free < requested_bytes:
                raise UpgradeUnavailable("Native staging has insufficient free disk space")
            value = UpgradeDiskHold(
                job_id=self.plan.operation.job_id,
                generation=self.plan.operation.generation,
                plan_sha256=self.plan.authority_sha256(),
                resource_key="disk-" + uuid4().hex + ".reserve",
                state="allocating",
                requested_bytes=requested_bytes,
                updated_at=datetime.now(UTC),
            )
            self._record(value)
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    self.store.path(value.resource_key),
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                details = os.fstat(descriptor)
                value = UpgradeDiskHold.model_validate(
                    value.model_dump(mode="json")
                    | {
                        "file_device": details.st_dev,
                        "file_inode": details.st_ino,
                    }
                )
                self._record(value)
                allocated = preallocate_disk(descriptor, requested_bytes)
                value = UpgradeDiskHold.model_validate(
                    value.model_dump(mode="json")
                    | {
                        "state": "held",
                        "allocated_bytes": allocated,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self._record(value)
                return value
            except OSError:
                raise UpgradeUnavailable(
                    "Native staging allocation failed; owned cleanup is required"
                ) from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def release(self) -> UpgradeDiskHold | None:
        with (
            tracer.start_as_current_span(
                "coire.node.upgrade_disk_release",
                record_exception=False,
                set_status_on_exception=False,
            ),
            self.store.ownership("space.lock"),
        ):
            value = self.current()
            if value is None:
                return None
            details = self._file(value)
            if value.state == "released":
                if details is not None:
                    raise UpgradeUnavailable("Released native staging has unreconciled files")
                return value
            if details is None and value.file_inode is not None and value.state != "releasing":
                raise UpgradeUnavailable("Native staging disappeared before owned cleanup")
            releasing = UpgradeDiskHold.model_validate(
                value.model_dump(mode="json")
                | {
                    "state": "releasing",
                    "updated_at": datetime.now(UTC),
                }
            )
            if value.state != "releasing":
                self._record(releasing)
            value = releasing
            if details is not None:
                latest = self._file(value)
                if latest is None or (latest.st_mtime_ns, latest.st_ctime_ns) != (
                    details.st_mtime_ns,
                    details.st_ctime_ns,
                ):
                    raise UpgradeUnavailable("Native staging file changed before cleanup")
                self.store.path(value.resource_key).unlink()
                self.store.sync()
            elif value.file_inode is None:
                # Before creation, absence proves there is no allocated artifact.
                self.store.sync()
            released = UpgradeDiskHold.model_validate(
                value.model_dump(mode="json")
                | {
                    "state": "released",
                    "updated_at": datetime.now(UTC),
                }
            )
            self._record(released)
            return released
