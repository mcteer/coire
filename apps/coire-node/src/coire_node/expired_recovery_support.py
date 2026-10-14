"""Passive installed support identity capture, no legacy ownership/effects."""

import os
import platform
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psutil
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySupportProcess,
    UpgradeExpiredRecoverySupportStartup,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity, PreparedNativeRuntime
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.native_platform import is_darwin
from coire_node.upgrade_guard import NativeGuardTransport, _owned_native_process
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.recovery_support")


def run_configured_support(settings: Settings, stopped: threading.Event) -> None:
    """Installed fixed guard mode, never legacy recovery/delegate on failure."""
    if stopped.is_set():
        return
    if (
        not settings.upgrades_enabled
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or settings.upgrade_recovery_id is None
    ):
        raise UpgradeUnavailable("Configured support mode is not authorized")
    with tracer.start_as_current_span("coire.node.recovery_support_start"):
        bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(runs.path(settings.upgrade_recovery_id), create=False)
        startup = run.read("startup.json", UpgradeExpiredRecoverySupportStartup)
        if startup is None or startup.intent.recovery_id != settings.upgrade_recovery_id:
            raise UpgradeUnavailable("Configured support startup is absent or differs")
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5.0) as client:
            if (
                settings.upgrade_recovery_succession_id is None
                and not run.path("owner.claim.json").exists()
                and not run.path("cold.claim.json").exists()
            ):
                from coire_node.expired_recovery_startup_wait import wait_for_launcher_retirement

                if not wait_for_launcher_retirement(
                    settings, startup, run, NativeGuardTransport(settings, client), stopped
                ):
                    return
            if settings.upgrade_recovery_succession_id is not None:
                from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySuccessorClaim
                from coire_node.expired_recovery_successor_controller import run_successor_holding

                succession_id = settings.upgrade_recovery_succession_id
                if settings.upgrade_recovery_succession_mode == "initial":
                    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
                    from coire_node.expired_recovery_successor_session import (
                        run_initial_successor_session,
                    )

                    previous = run.read("owner.claim.json", UpgradeExpiredRecoveryOwnerClaim)
                    if (
                        previous is None
                        or previous.consumption.transfer.intent != startup.intent
                        or previous.runtime != startup.runtime
                    ):
                        raise UpgradeUnavailable("Configured initial successor predecessor differs")
                    run_initial_successor_session(
                        settings,
                        previous,
                        succession_id,
                        NativeGuardTransport(settings, client),
                        stopped,
                    )
                    return
                claim = run.read(
                    f"successor.{succession_id}.claim.json", UpgradeExpiredRecoverySuccessorClaim
                )
                if (
                    claim is None
                    or claim.grant.observation.succession_id != succession_id
                    or claim.grant.observation.previous_owner.consumption.transfer.intent
                    != startup.intent
                    or claim.runtime != startup.runtime
                ):
                    raise UpgradeUnavailable("Configured successor preimage is absent or differs")
                run_successor_holding(
                    settings, claim, NativeGuardTransport(settings, client), stopped
                )
                return
            run_support_mode(
                settings,
                startup.intent,
                startup.runtime,
                NativeGuardTransport(settings, client),
                stopped,
            )


def prepare_support_startup(
    settings: Settings,
    intent: UpgradeExpiredRecoveryIntent,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoverySupportStartup:
    """Retain exact verified installed context, never install/launch or activate.

    Only existing verified support bytes are eligible. Source bootstrap selectors,
    journals and claims remain untouched; new scope is created after fresh gates.
    """
    intent = UpgradeExpiredRecoveryIntent.model_validate_json(intent.model_dump_json())
    if (
        not settings.upgrades_enabled
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or settings.node_name != intent.original_plan.operation.node
    ):
        raise UpgradeUnavailable("Recovery startup scope differs")
    support = intent.support_release.manifest
    if (runtime.environment_id, runtime.environment_sha256) != (
        support.environment_id,
        support.environment_sha256,
    ):
        raise UpgradeConflict("Recovery startup support artifact differs")
    if control.verify_support_context(intent).sha256() != intent.sha256():
        raise UpgradeUnavailable("Recovery startup Core context differs")
    envs = Path(settings.upgrade_environment_root)
    verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    with bootstrap.ownership("recovery-support-startup.lock"):
        if control.verify_support_context(intent).sha256() != intent.sha256():
            raise UpgradeUnavailable("Recovery startup Core context changed")
        verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
        captured = UpgradeExpiredRecoverySupportStartup(
            intent=intent, runtime=runtime, prepared_at=datetime.now(UTC)
        )
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"))
        run = PrivateUpgradeStore(runs.path(intent.recovery_id))
        previous = run.read("startup.json", UpgradeExpiredRecoverySupportStartup)
        if previous is not None:
            if previous.intent != intent or previous.runtime != runtime:
                raise UpgradeConflict("Retained support startup context differs")
            return previous
        run.write("startup.json", captured, replace=False)
        return captured


def run_support_mode(
    settings: Settings,
    intent: UpgradeExpiredRecoveryIntent,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> None:
    """Select scoped support session, never legacy delegation on error.

    Owner claim selects explicit accepted/unaccepted probe inside the controller;
    absence alone grants nothing: initial session must obtain live Core consumed
    authority before retirement. Missing/corrupt namespace cannot be recreated.
    """
    if intent.request.recovery_mode == "cold":
        from coire_node.expired_recovery_cold_controller import run_cold_controller

        run_cold_controller(settings, intent, runtime, control, stopped)
        return
    from coire_node.expired_recovery_controller import run_holding_controller

    intent = UpgradeExpiredRecoveryIntent.model_validate_json(intent.model_dump_json())
    if stopped.is_set():
        return
    if (
        not settings.upgrades_enabled
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or settings.node_name != intent.original_plan.operation.node
    ):
        raise UpgradeUnavailable("Recovery support mode scope differs")
    if control.verify_support_context(intent).sha256() != intent.sha256():
        raise UpgradeUnavailable("Recovery support mode Core intent differs")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(intent.recovery_id), create=False)
    owner = run.read("owner.claim.json", UpgradeExpiredRecoveryOwnerClaim)
    if owner is not None:
        if owner.consumption.transfer.intent != intent or owner.runtime != runtime:
            raise UpgradeConflict("Retained support owner differs from startup context")
        run_holding_controller(settings, owner, control, stopped)
    else:
        run_initial_support_session(settings, intent, runtime, control, stopped)


def run_initial_support_session(
    settings: Settings,
    intent: UpgradeExpiredRecoveryIntent,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> None:
    """Initial managed handover only, never fallback from an accepted owner.

    Each producer independently repeats fresh authority/physical gates. Uncertain
    retirement refuses rather than retrying a command. Activation exclusion stays
    held into the entire bounded controller lifetime; no release/relock gap.
    Already accepted owners must use the explicit owner-state/resume controller.
    """
    from coire_node.expired_recovery_controller import run_held_controller
    from coire_node.expired_recovery_owner import activated_recovery_owner
    from coire_node.expired_recovery_stop_reconciliation import ensure_initial_retirement

    receipt = wait_for_consumed_transfer(settings, intent, runtime, control, stopped)
    if receipt is None or stopped.is_set():
        return
    ensure_initial_retirement(settings, receipt, runtime, control, stopped=stopped)
    if stopped.is_set():
        return  # Original fences remain; no invented completed transfer/serving.
    with activated_recovery_owner(settings, receipt, runtime, control) as owner:
        run_held_controller(settings, owner, control, stopped)


def wait_for_consumed_transfer(
    settings: Settings,
    intent: UpgradeExpiredRecoveryIntent,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> UpgradeExpiredRecoveryTransferConsumption | None:
    """Passive startup handshake only; no stop, ownership or serving side effect.

    Every poll requires live authenticated original context. A received receipt
    must bind exact captured intent and this actual process; returned authority
    is independently authenticated before any later producer acts on it.
    """
    if stopped.is_set():
        return None
    deadline_tick = time.monotonic() + max(
        0.0, min(300.0, (intent.deadline_at - datetime.now(UTC)).total_seconds())
    )

    def remaining_budget() -> float:
        return min(
            (intent.deadline_at - datetime.now(UTC)).total_seconds(),
            deadline_tick - time.monotonic(),
        )

    passive = capture_support_process(settings, intent, runtime, control)
    if stopped.is_set():
        return None
    from coire_node.expired_recovery_prepare import prepare_local_transfer

    while not stopped.is_set():
        current_intent = control.verify_support_context(intent)
        if stopped.is_set():
            return None
        if current_intent.sha256() != intent.sha256():
            raise UpgradeUnavailable("Passive transfer context changed")
        remaining = remaining_budget()
        if remaining <= 0:
            raise UpgradeUnavailable("Passive transfer wait exhausted captured budget")
        state = control.probe_recovery_consumption(intent, passive.process)
        if stopped.is_set():
            return None
        if state.intent_sha256 != intent.sha256():
            raise UpgradeUnavailable("Passive delivery intent differs")
        if remaining_budget() <= 0:
            raise UpgradeUnavailable("Passive delivery exhausted captured budget")
        receipt = state.consumption
        if receipt is not None:
            if (
                receipt.transfer.intent.sha256() != intent.sha256()
                or receipt.consumer != passive.process
            ):
                raise UpgradeConflict("Passive transfer consumption belongs to another actor")
            current = control.verify_recovery_consumption(receipt)
            if current.sha256() != receipt.sha256():
                raise UpgradeUnavailable("Passive transfer consumption response differs")
            if stopped.is_set():
                return None
            if remaining_budget() <= 0:
                raise UpgradeUnavailable("Passive verification exhausted captured budget")
            prepared, _ = prepare_local_transfer(settings, current, runtime, control)
            if stopped.is_set():
                return None
            if prepared.sha256() != current.sha256():
                raise UpgradeUnavailable("Passive local preparation differs")
            if remaining_budget() <= 0:
                raise UpgradeUnavailable("Passive preparation exhausted captured budget")
            return prepared
        remaining = remaining_budget()
        if remaining <= 0:
            raise UpgradeUnavailable("Passive delivery exhausted captured budget")
        stopped.wait(min(1.0, remaining))
    return None


def verify_passive_support_identity(
    settings: Settings,
    intent: UpgradeExpiredRecoveryIntent,
    runtime: PreparedNativeRuntime,
) -> NodeAgentProcessIdentity:
    support = intent.support_release.manifest
    if (
        not is_darwin(sys.platform)
        or platform.machine() != "arm64"
        or platform.node().lower().split(".", 1)[0] != settings.node_name
        or not settings.upgrades_enabled
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or settings.node_name != intent.original_plan.operation.node
        or (runtime.environment_id, runtime.environment_sha256)
        != (support.environment_id, support.environment_sha256)
        or not intent.accepted_at <= datetime.now(UTC) < intent.deadline_at
    ):
        raise UpgradeUnavailable("Passive recovery support context differs")
    try:
        envs = Path(settings.upgrade_environment_root)
        python = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
        identity = NodeAgentProcessIdentity(
            pid=os.getpid(),
            create_time_s=psutil.Process(os.getpid()).create_time(),
            environment_sha256=runtime.environment_sha256,
        )
        if (
            Path(sys.prefix).resolve(strict=True)
            != (envs / runtime.environment_id).resolve(strict=True)
            or Path(sys.executable).resolve(strict=True) != python.resolve(strict=True)
            or _owned_native_process(identity, python, "coire_node.upgrade_guard") is None
            or datetime.now(UTC) >= intent.deadline_at
        ):
            raise UpgradeUnavailable("Passive recovery support interpreter or module differs")
        return identity
    except (OSError, ValueError, psutil.Error):
        raise UpgradeUnavailable("Passive recovery support identity is uncertain") from None


def capture_support_process(
    settings: Settings,
    intent: UpgradeExpiredRecoveryIntent,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoverySupportProcess:
    """Capture once after fresh Core scope and actual installed runtime checks.

    Creates only a distinct private recovery namespace. No legacy journal,
    bootstrap selector/status, signals, ports, model loads or ownership claims.
    """
    with tracer.start_as_current_span("coire.node.recovery_support_capture"):
        if control.verify_support_context(intent).sha256() != intent.sha256():
            raise UpgradeUnavailable("Passive recovery support Core context differs")
        process = verify_passive_support_identity(settings, intent, runtime)
        bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
        with bootstrap.ownership("recovery-support.lock"):
            if control.verify_support_context(intent).sha256() != intent.sha256():
                raise UpgradeUnavailable("Passive recovery support context changed")
            if verify_passive_support_identity(settings, intent, runtime) != process:
                raise UpgradeUnavailable("Passive recovery support process changed")
            runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"))
            run = PrivateUpgradeStore(runs.path(intent.recovery_id))
            previous = run.read("support-process.json", UpgradeExpiredRecoverySupportProcess)
            if previous is not None:
                if (previous.intent, previous.runtime, previous.process) != (
                    intent,
                    runtime,
                    process,
                ):
                    raise UpgradeConflict("Retained passive support process differs")
                return previous
            captured = UpgradeExpiredRecoverySupportProcess(
                intent=intent, runtime=runtime, process=process, observed_at=datetime.now(UTC)
            )
            run.write("support-process.json", captured, replace=False)
            return captured
