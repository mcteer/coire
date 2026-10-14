"""Write-ahead fixed Source launch and positive lost-response reconciliation."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psutil
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdSourceLaunch,
    UpgradeExpiredRecoveryColdSourceProcess,
    UpgradeExpiredRecoveryColdSourceReceipt,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity
from coire_core.settings import Settings
from coire_node.expired_recovery_cold_source import (
    publish_cold_source_readiness,
    stores,
    verify_projection_boundary,
)
from coire_node.expired_recovery_cold_telemetry import record_cold_effect
from coire_node.upgrade_guard import NativeGuardTransport, _owned_node_process
from coire_node.upgrade_inventory import native_process_entries
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.cold_recovery")


def require_live_cold_source(
    settings: Settings, source: UpgradeExpiredRecoveryColdSourceProcess
) -> None:
    _, run, _ = stores(settings, source.launch.source.projection.owner)
    if (
        run.read(
            f"cold.source.process.{source.launch.attempt}.json",
            UpgradeExpiredRecoveryColdSourceProcess,
        )
        != source
        or observed_source_process(settings, source.launch) != source.process
    ):
        raise UpgradeUnavailable("Cold Source has no exact retained live process")


def observed_source_process(
    settings: Settings, launch: UpgradeExpiredRecoveryColdSourceLaunch
) -> NodeAgentProcessIdentity | None:
    """At most one exact Source process born under this recorded launch epoch."""
    effect = launch.source.projection
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(
        envs, envs.parent / "runtimes", effect.source_selection.runtime
    )
    if launch.retained_process is not None:
        from coire_node.expired_recovery_cold_continuation import previous_source

        binding = effect.owner.continuation
        if binding is None:
            raise UpgradeConflict("Cold retained Source has no immutable continuation")
        _owner, retained = previous_source(settings, binding)
        if launch.retained_process != retained.process:
            raise UpgradeConflict("Cold retained Source differs from predecessor")
        child = _owned_node_process(retained.process, python)
        if child is None:
            return None
        if child.cmdline() != [str(python), "-I", "-B", "-m", "coire_node"]:
            raise UpgradeUnavailable("Cold retained Source argv differs")
        return retained.process
    matches = []
    for entry in native_process_entries("cold-recovery", (str(envs.parent),)):
        if (entry.process_id, entry.process_created_at) == (
            effect.owner.process.pid,
            effect.owner.process.create_time_s,
        ):
            continue
        if entry.process_id is None or entry.process_created_at is None:
            raise UpgradeUnavailable("Cold launch inventory has no positive identity")
        if (
            not launch.started_at.timestamp()
            <= entry.process_created_at
            < effect.owner.intent.deadline_at.timestamp()
        ):
            raise UpgradeConflict("Cold launch inventory contains an older owner")
        identity = NodeAgentProcessIdentity(
            pid=entry.process_id,
            create_time_s=entry.process_created_at,
            environment_sha256=effect.source_selection.runtime.environment_sha256,
        )
        child = _owned_node_process(identity, python)
        if child is None:
            continue
        if child.cmdline() != [str(python), "-I", "-B", "-m", "coire_node"]:
            raise UpgradeUnavailable("Cold launch process does not select the exact Source argv")
        matches.append(identity)
    if len(matches) > 1:
        raise UpgradeUnavailable("Cold launch has multiple live Source processes")
    return matches[0] if matches else None


@tracer.start_as_current_span("coire.node.cold_recovery_launch_source")
def launch_cold_source(
    settings: Settings,
    source: UpgradeExpiredRecoveryColdSourceReceipt,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoveryColdSourceProcess:
    """Spawn once, or reconcile retained intent; no absence-triggered retry.

    A retained launch with no live process requires an explicit recorded failure
    and retry protocol. An exception never resets the epoch or spawns another
    process. Process evidence alone grants no serving or maintenance release.
    """
    effect = source.projection
    _, run, _ = stores(settings, effect.owner)
    with run.ownership("cold.source.launch.lock"):
        verify_projection_boundary(settings, effect, control)
        if run.read("cold.source.receipt.json", UpgradeExpiredRecoveryColdSourceReceipt) != source:
            raise UpgradeConflict("Cold launch has no committed Source projection")
        launch = run.read("cold.source.launch.0.json", UpgradeExpiredRecoveryColdSourceLaunch)
        receipt = run.read("cold.source.process.0.json", UpgradeExpiredRecoveryColdSourceProcess)
        if receipt is not None:
            if receipt.launch != launch or receipt.launch.source != source:
                raise UpgradeConflict("Cold Source process history differs")
            if observed_source_process(settings, receipt.launch) != receipt.process:
                raise UpgradeUnavailable("Cold Source process receipt is no longer physically live")
            return receipt
        if launch is None and effect.owner.continuation is not None:
            from coire_node.expired_recovery_cold_continuation import previous_source

            _previous, retained = previous_source(settings, effect.owner.continuation)
            launch = UpgradeExpiredRecoveryColdSourceLaunch(
                source=source,
                attempt=0,
                started_at=datetime.now(UTC),
                retained_process=retained.process,
            )
            run.write("cold.source.launch.0.json", launch, replace=False)
            verify_projection_boundary(settings, effect, control)
            if observed_source_process(settings, launch) != retained.process:
                raise UpgradeUnavailable("Cold retained Source is no longer physically live")
            identity = retained.process
        elif launch is None:
            launch = UpgradeExpiredRecoveryColdSourceLaunch(
                source=source, attempt=0, started_at=datetime.now(UTC)
            )
            run.write("cold.source.launch.0.json", launch, replace=False)
            verify_projection_boundary(settings, effect, control)
            publish_cold_source_readiness(settings, source, control)
            envs = Path(settings.upgrade_environment_root)
            python = verify_prepared_runtime(
                envs, envs.parent / "runtimes", effect.source_selection.runtime
            )
            # Final authority check precedes spawn; heartbeat is refreshed after
            # graph verification so the sealed Source sees a fresh guard.
            verify_projection_boundary(settings, effect, control)
            publish_cold_source_readiness(settings, source, control)
            environment = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "PYTHONPATH",
                    "PYTHONHOME",
                    "DYLD_INSERT_LIBRARIES",
                    "LD_PRELOAD",
                    "UPGRADE_RECOVERY_ID",
                    "UPGRADE_RECOVERY_SUPERVISION_FD",
                    "UPGRADE_RECOVERY_CORE_API_PORT",
                    "UPGRADE_RECOVERY_SUCCESSION_ID",
                    "UPGRADE_RECOVERY_SUCCESSION_MODE",
                }
            }
            environment.update(
                {
                    "NODE_NAME": settings.node_name,
                    "NODE_STATE_DIR": settings.node_state_dir,
                    "NODE_STORE_DIR": settings.node_store_dir,
                    "UPGRADE_ENVIRONMENT_ROOT": settings.upgrade_environment_root,
                    "UPGRADE_BOOTSTRAP_ROOT": settings.upgrade_bootstrap_root,
                    "UPGRADES_ENABLED": "true",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                }
            )
            try:
                child = subprocess.Popen(
                    [str(python), "-I", "-B", "-m", "coire_node"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                    env=environment,
                )
                birth = psutil.Process(child.pid).create_time()
            except (OSError, psutil.Error):
                raise UpgradeUnavailable(
                    "Cold Source launch acknowledgement is uncertain"
                ) from None
            identity = NodeAgentProcessIdentity(
                pid=child.pid,
                create_time_s=birth,
                environment_sha256=effect.source_selection.runtime.environment_sha256,
            )
        else:
            if launch.source != source:
                raise UpgradeConflict("Cold Source launch belongs to another projection")
            observed = observed_source_process(settings, launch)
            if observed is None:
                raise UpgradeUnavailable(
                    "Retained cold Source launch requires explicit failed-launch reconciliation"
                )
            identity = observed
        if observed_source_process(settings, launch) != identity:
            raise UpgradeUnavailable("Cold Source launch physical identity differs")
        verify_projection_boundary(settings, effect, control)
        receipt = UpgradeExpiredRecoveryColdSourceProcess(
            launch=launch, process=identity, observed_at=datetime.now(UTC)
        )
        run.write("cold.source.process.0.json", receipt, replace=False)
        record_cold_effect(effect.owner, "source_launched")
        return receipt
