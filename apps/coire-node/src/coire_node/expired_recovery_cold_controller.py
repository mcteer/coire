"""Explicit cold pipeline inside one actual kernel ownership lifetime."""

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdSourceProcess,
    UpgradeExpiredRecoveryIntent,
)
from coire_core.models.upgrades import PreparedNativeRuntime
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_lease import require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.cold_recovery_controller")


def wait_for_cold_source(
    settings: Settings,
    source: UpgradeExpiredRecoveryColdSourceProcess,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> bool:
    """Poll fixed readiness only; unavailable observation never repeats a launch."""
    from coire_node.expired_recovery_cold_launch import require_live_cold_source
    from coire_node.expired_recovery_cold_source import stores

    owner = source.launch.source.projection.owner
    bootstrap, _, _ = stores(settings, owner)
    deadline = min(owner.intent.deadline_at, source.launch.started_at + timedelta(seconds=30))
    tick = time.monotonic() + max(0, (deadline - datetime.now(UTC)).total_seconds())
    while not stopped.is_set() and datetime.now(UTC) < deadline and time.monotonic() < tick:
        require_recovery_lease(bootstrap, owner.sha256())
        if control.verify_cold_recovery_owner(owner) != owner:
            raise UpgradeUnavailable("Cold Source startup authority changed")
        require_live_cold_source(settings, source)
        try:
            status = control.status(owner.intent.original_plan, deadline_at=deadline)
        except UpgradeUnavailable:
            stopped.wait(min(0.2, max(0, tick - time.monotonic())))
            continue
        if status.runtime_sha256 is not None and (
            status.runtime_sha256 != owner.original_native.source.runtime.environment_sha256
            or status.release_sha256 != owner.intent.request.expected_source_sha256
        ):
            raise UpgradeConflict("Cold Source startup observed another physical runtime")
        if (
            status.bootstrap_ready or recovery_writer_ready(settings, source)
        ) and status.runtime_sha256 == owner.original_native.source.runtime.environment_sha256:
            return True
        stopped.wait(min(0.2, max(0, tick - time.monotonic())))
    if stopped.is_set():
        return False
    raise UpgradeUnavailable("Cold Source readiness exhausted its captured startup window")


def recovery_writer_ready(
    settings: Settings, source: UpgradeExpiredRecoveryColdSourceProcess
) -> bool:
    """The legacy flag describes ordinary Source-helper ownership, not this lease."""
    from coire_core.models.upgrades import NativeGuardStatus
    from coire_node.expired_recovery_cold_source import stores

    owner = source.launch.source.projection.owner
    if (
        owner.intent.local_recovery_maintenance is None
        and owner.intent.inherited_local_maintenance is None
    ):
        return False
    bootstrap, _, _ = stores(settings, owner)
    require_recovery_lease(bootstrap, owner.sha256())
    current = bootstrap.read("status.json", NativeGuardStatus)
    return current is not None and (
        current.process == owner.process
        and current.bootstrap_runtime == owner.runtime
        and current.selected == source.launch.source.projection.source_selection
        and current.ready
        and 0 <= (datetime.now(UTC) - current.observed_at).total_seconds() <= 5
    )


def run_cold_controller(
    settings: Settings,
    intent: UpgradeExpiredRecoveryIntent,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> None:
    from coire_node.expired_recovery_cold_owner import initial_cold_owner, resumed_cold_owner

    if stopped.is_set():
        return
    if (
        intent.request.recovery_mode != "cold"
        or settings.upgrade_recovery_succession_id is not None
    ):
        raise UpgradeUnavailable("Cold pipeline has no explicit supported initial/resume scope")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(intent.recovery_id), create=False)
    retained = run.read("cold.claim.json", UpgradeExpiredRecoveryColdClaim)
    if retained is None:
        with initial_cold_owner(settings, intent, runtime, control) as owner:
            run_held_cold_controller(settings, owner, control, stopped)
    else:
        if retained.intent != intent or retained.runtime != runtime:
            raise UpgradeConflict("Cold pipeline retained startup owner differs")
        with resumed_cold_owner(settings, retained, control) as owner:
            run_held_cold_controller(settings, owner, control, stopped)


@tracer.start_as_current_span("coire.node.cold_recovery_pipeline")
def run_held_cold_controller(
    settings: Settings,
    owner: UpgradeExpiredRecoveryColdClaim,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> None:
    from coire_node.expired_recovery_cold_archive import archive_cold_collectives
    from coire_node.expired_recovery_cold_closure import close_cold_source, resolve_closed_source
    from coire_node.expired_recovery_cold_handback import handback_cold_source
    from coire_node.expired_recovery_cold_heartbeat import cold_readiness_heartbeat
    from coire_node.expired_recovery_cold_launch import launch_cold_source
    from coire_node.expired_recovery_cold_pair import qualify_cold_pair
    from coire_node.expired_recovery_cold_smoke import run_cold_source_smoke
    from coire_node.expired_recovery_cold_source import (
        project_cold_source,
        publish_cold_source_readiness,
        stores,
    )
    from coire_node.expired_recovery_cold_supervision import supervise_cold_source
    from coire_node.expired_recovery_cold_verification import verify_cold_source
    from coire_node.link_probes import LinkProbeRunner

    bootstrap, run, _ = stores(settings, owner)

    def boundary() -> bool:
        require_recovery_lease(bootstrap, owner.sha256())
        return not stopped.is_set()

    # Unsupported modes refuse before selecting Source or spawning a process.
    # They cannot borrow a live-preservation controller or a different rank.
    if owner.supervision != "fixture" or settings.node_name != "coire-edge-a":
        raise UpgradeUnavailable("Cold pipeline requires supported fixture rank-zero handback")
    if not owner.intent.request.original_scope_sha256s:
        raise UpgradeUnavailable("Cold pipeline pre-dispatch archive producer is not yet available")
    if not boundary():
        return
    selection = project_cold_source(settings, owner, control)
    if not boundary():
        return
    publish_cold_source_readiness(settings, selection, control)
    with cold_readiness_heartbeat(settings, selection):
        source = launch_cold_source(settings, selection, control)
        if not wait_for_cold_source(settings, source, control, stopped):
            return
        if not boundary():
            return
        archive = archive_cold_collectives(settings, source, control)
        if not boundary():
            return
        smoke = run_cold_source_smoke(settings, source, control)
        if not boundary():
            return
        plan = owner.intent.original_plan
        pair = (
            qualify_cold_pair(
                settings,
                smoke,
                archive,
                control,
                LinkProbeRunner(
                    settings,
                    diagnostic_store=PrivateUpgradeStore(run.path("pair-diagnostics")),
                ),
            )
            if plan.source.manifest.paired_required or plan.target.manifest.paired_required
            else None
        )
    if not boundary():
        return
    verification = verify_cold_source(settings, smoke, pair, control)
    if not boundary():
        return
    handback = handback_cold_source(settings, verification, control)
    if not boundary():
        return
    supervision = supervise_cold_source(settings, handback, control)
    if not boundary():
        return
    closure = close_cold_source(settings, supervision, control)
    if not boundary():
        return
    resolve_closed_source(settings, closure, control)
