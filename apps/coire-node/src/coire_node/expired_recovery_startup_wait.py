"""Read-only startup barrier; the launcher must retire before cold ownership."""

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoverySupportLaunchIntent,
    UpgradeExpiredRecoverySupportProcess,
    UpgradeExpiredRecoverySupportStartup,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_support import verify_passive_support_identity
from coire_node.upgrade_guard import NativeGuardTransport
from coire_node.upgrade_inventory import native_process_entries


def wait_for_launcher_retirement(
    settings: Settings,
    startup: UpgradeExpiredRecoverySupportStartup,
    run: PrivateUpgradeStore,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> bool:
    """No signal, claim, selector change or portable lease is produced here."""
    launch = run.read("support.launch.intent.json", UpgradeExpiredRecoverySupportLaunchIntent)
    if launch is None or launch.startup != startup:
        raise UpgradeConflict("Recovery startup has no exact recorded launcher")
    actor = verify_passive_support_identity(settings, startup.intent, startup.runtime)
    if actor.pid == launch.launcher.pid:
        raise UpgradeConflict("Recovery child cannot be its own launcher")
    deadline = min(startup.intent.deadline_at, launch.started_at + timedelta(seconds=30))
    if stopped.is_set():
        return False
    if datetime.now(UTC) >= deadline:
        raise UpgradeUnavailable(
            "Recovery startup exhausted its captured launcher retirement window"
        )
    retained = None
    retained_guard = None
    if startup.intent.request.recovery_mode == "cold":
        grant = control.read_cold_recovery_continuation(startup.intent, deadline_at=deadline)
        if grant is not None:
            from coire_node.expired_recovery_cold_continuation import previous_source
            from coire_node.upgrade_guard import _owned_node_process
            from coire_node.upgrade_runtime import verify_prepared_runtime

            _previous, retained = previous_source(settings, grant.binding)
            retained_guard = (
                grant.binding.post_handback.source_helper
                if grant.binding.post_handback is not None
                else None
            )
            envs = Path(settings.upgrade_environment_root)
            source_python = verify_prepared_runtime(
                envs, envs.parent / "runtimes", grant.binding.original_native.source.runtime
            )
            child = _owned_node_process(retained.process, source_python)
            if child is None or child.cmdline() != [
                str(source_python),
                "-I",
                "-B",
                "-m",
                "coire_node",
            ]:
                raise UpgradeUnavailable("Cold startup retained Source physical identity differs")
    tick = time.monotonic() + max(0, (deadline - datetime.now(UTC)).total_seconds())
    while not stopped.is_set() and datetime.now(UTC) < deadline and time.monotonic() < tick:
        if control.verify_support_context(startup.intent) != startup.intent:
            raise UpgradeUnavailable("Recovery startup Core context changed")
        receipt = run.read("support.launch.receipt.json", UpgradeExpiredRecoverySupportProcess)
        if receipt is not None and (
            receipt.intent != startup.intent
            or receipt.runtime != startup.runtime
            or receipt.process != actor
        ):
            raise UpgradeConflict("Recovery startup child differs from the recorded launch")
        try:
            parent = psutil.Process(launch.launcher.pid)
            retired = parent.create_time() != launch.launcher.create_time_s
            if not retired and parent.uids().real != os.getuid():
                raise UpgradeUnavailable("Recovery launcher owner is uncertain")
        except psutil.NoSuchProcess:
            retired = True
        except psutil.Error:
            raise UpgradeUnavailable("Recovery launcher retirement cannot be observed") from None
        if retired and receipt is not None:
            # SSH's command shell can outlive the launcher briefly. Waiting for
            # physical scope clearance grants no authority: the cold producer
            # must still obtain all locks, recheck Core and prove old retirement.
            entries = (
                native_process_entries(
                    "cold-recovery", (str(Path(settings.upgrade_environment_root).parent),)
                )
                if startup.intent.request.recovery_mode == "cold"
                else ()
            )
            allowed = {(actor.pid, actor.create_time_s)}
            if retained is not None:
                allowed.add((retained.process.pid, retained.process.create_time_s))
            if retained_guard is not None:
                from coire_core.models.upgrades import NativeGuardStatus
                from coire_node.expired_recovery_cold_handback import source_writer_lock_held
                from coire_node.upgrade_guard import _owned_native_process

                root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
                current = root.read("status.json", NativeGuardStatus)
                keeper = _owned_native_process(
                    retained_guard.process, source_python, "coire_node.upgrade_guard"
                )
                if (
                    current is None
                    or current.process != retained_guard.process
                    or current.selected != retained_guard.selected
                    or current.bootstrap_runtime != retained_guard.bootstrap_runtime
                    or not current.ready
                    or not 0 <= (datetime.now(UTC) - current.observed_at).total_seconds() <= 5
                    or keeper is None
                    or keeper.cmdline()
                    != [str(source_python), "-I", "-B", "-m", "coire_node.upgrade_guard"]
                    or not source_writer_lock_held(root)
                ):
                    raise UpgradeUnavailable("Cold startup retained Source writer identity differs")
                allowed.add((retained_guard.process.pid, retained_guard.process.create_time_s))
            if all((entry.process_id, entry.process_created_at) in allowed for entry in entries):
                return True
        stopped.wait(min(0.1, max(0, tick - time.monotonic())))
    if stopped.is_set():
        return False
    raise UpgradeUnavailable("Recovery startup exhausted its captured launcher retirement window")
