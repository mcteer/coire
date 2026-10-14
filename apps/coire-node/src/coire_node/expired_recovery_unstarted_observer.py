"""Read-only installed observation of an expired, unstarted recovery attempt."""

import logging
import os
import platform
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psutil
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoverySupportProcess,
    UpgradeExpiredRecoveryUnstartedObservation,
    UpgradeExpiredRecoveryUnstartedObserverInput,
    UpgradeExpiredRecoveryUnstartedRead,
)
from coire_core.models.upgrades import (
    NodeAgentProcessIdentity,
    PreparedNativeRuntime,
    UpgradeMaintenance,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_cold_owner import retained_cold_preimages
from coire_node.expired_recovery_native_data import capture_native_data
from coire_node.native_platform import is_darwin
from coire_node.recovery_identity import verify_retired_identity
from coire_node.upgrade_guard import NativeGuardTransport, _owned_native_process
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.unstarted_recovery")
logger = logging.getLogger(__name__)


@tracer.start_as_current_span("coire.node.unstarted_recovery_observe", record_exception=False)
def observe_unstarted(
    settings: Settings,
    body: UpgradeExpiredRecoveryUnstartedObserverInput,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoveryUnstartedObservation:
    intent = body.intent
    # This bounds metadata reads only. It grants no recovery execution window,
    # native effects, ownership transfer or original maintenance release.
    io_deadline = datetime.now(UTC) + timedelta(seconds=15)

    def context() -> None:
        retained = control._read(
            f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-unstarted-context",
            UpgradeExpiredRecoveryIntent,
            io_deadline,
            core=True,
            method="POST",
            payload=UpgradeExpiredRecoveryUnstartedRead(intent_sha256=intent.sha256()),
        )
        if retained != intent:
            raise UpgradeConflict("Unstarted recovery metadata context differs")

    runtime = body.observer_runtime
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    actor = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process().create_time(),
        environment_sha256=runtime.environment_sha256,
    )
    if (
        not settings.upgrades_enabled
        or not is_darwin(sys.platform)
        or platform.machine() != "arm64"
        or platform.node().lower().split(".", 1)[0] != settings.node_name
        or settings.node_name != intent.original_plan.operation.node
        or Path(sys.prefix).resolve(strict=True)
        != (envs / runtime.environment_id).resolve(strict=True)
        or Path(sys.executable).resolve(strict=True) != python.resolve(strict=True)
        or _owned_native_process(actor, python, "coire_node.expired_recovery_unstarted_observer")
        is None
    ):
        raise UpgradeUnavailable("Unstarted recovery observer is not the fixed installed module")
    context()
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(intent.recovery_id), create=False)
    records = PrivateUpgradeStore(bootstrap.root.parent / "fixture-records", create=False)
    if intent.request.supervision != "fixture":
        raise UpgradeUnavailable("Unstarted observation requires the supported fixture exclusion")
    with (
        records.ownership("fixture-supervisor.lock"),
        bootstrap.ownership("lifetime.lock"),
        bootstrap.ownership("recovery-transfer.lock"),
    ):
        context()
        startup = run.read("support.launch.receipt.json", UpgradeExpiredRecoverySupportProcess)
        if startup is None or startup.intent != intent:
            raise UpgradeConflict("Unstarted recovery has no exact recorded startup actor")
        verify_retired_identity(startup.process)
        if any(
            run.path(key).exists() or run.path(key).is_symlink()
            for key in (
                "cold.claim.json",
                "cold.activation.json",
                "cold.source.intent.json",
                "owner.claim.json",
                "transfer.json",
                "consumption.json",
            )
        ):
            raise UpgradeConflict("Unstarted recovery retains a native ownership/effect preimage")
        original = retained_cold_preimages(settings, intent)

        def selected_pointer() -> PreparedNativeRuntime:
            selected = (envs / "current").resolve(strict=True)
            known = [
                candidate
                for candidate in (original[1].source.runtime, original[1].candidate)
                if (envs / candidate.environment_id).resolve(strict=True) == selected
            ]
            if len(known) != 1:
                raise UpgradeConflict("Unstarted recovery pointer is outside original authority")
            return known[0]

        pointer = selected_pointer()
        bootstrap_runtime = bootstrap.read("runtime.json", PreparedNativeRuntime)
        if bootstrap_runtime is None:
            raise UpgradeUnavailable("Unstarted recovery bootstrap identity is absent")
        for runtime_proof in (pointer, bootstrap_runtime, startup.runtime):
            verify_prepared_runtime(envs, envs.parent / "runtimes", runtime_proof)
        upgrades = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
        maintenance = upgrades.read("maintenance.json", UpgradeMaintenance)
        if maintenance is None or not maintenance.matches_plan(intent.original_plan):
            raise UpgradeConflict("Unstarted recovery original native maintenance changed")
        data = capture_native_data(settings, intent.original_plan, deadline_at=io_deadline)
        if (
            original != retained_cold_preimages(settings, intent)
            or pointer != selected_pointer()
            or bootstrap_runtime != bootstrap.read("runtime.json", PreparedNativeRuntime)
        ):
            raise UpgradeConflict("Unstarted recovery original native preimages changed")
        verify_retired_identity(startup.process)
        context()
        claim, native, guard, node = original
        proof = UpgradeExpiredRecoveryUnstartedObservation(
            intent=intent,
            startup=startup,
            observer_release=body.observer_release,
            observer_runtime=body.observer_runtime,
            observer=actor,
            native=native,
            claim=claim,
            guard=guard,
            node=node,
            pointer=pointer,
            bootstrap_runtime=bootstrap_runtime,
            native_data=data,
            observed_at=datetime.now(UTC),
        )
        # Retain exact metadata proof in the separate attempt namespace. The
        # admin audit stores its digest and safe closure summary, never nested
        # authorization-shaped fields or a bypass of audit redaction.
        run.write(f"unstarted.observation.{proof.sha256()}.json", proof, replace=False)
        accepted = control._read(
            f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-unstarted-observation",
            UpgradeExpiredRecoveryUnstartedObservation,
            io_deadline,
            core=True,
            method="POST",
            payload=proof,
        )
        if accepted != proof:
            raise UpgradeUnavailable("Unstarted recovery metadata acknowledgment differs")
        return proof


def main() -> None:
    from coire_core.logging import StructuredJSONFormatter
    from coire_node.keychain import NODE_TOKEN_ITEM, read_item
    from coire_node.otel import configure_node_telemetry

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        settings = Settings()
        configure_node_telemetry(
            "unstarted-recovery-observer",
            settings.otlp_endpoint,
            diagnostics_enabled=settings.diagnostics_enabled,
        )
        if not settings.node_token.get_secret_value():
            settings.node_token = read_item(NODE_TOKEN_ITEM)
        data = sys.stdin.buffer.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            raise UpgradeUnavailable("Unstarted observer input exceeds its bound")
        body = UpgradeExpiredRecoveryUnstartedObserverInput.model_validate_json(data)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5.0) as client:
            proof = observe_unstarted(settings, body, NativeGuardTransport(settings, client))
        print(proof.model_dump_json())
    except CoireError as error:
        logger.error(
            "Unstarted recovery observation refused",
            extra={"coire_code": error.code, "reason": error.detail},
        )
        raise SystemExit(
            "Unstarted recovery observation refused; original obligations remain held"
        ) from None
    except (OSError, ValueError, psutil.Error):
        raise SystemExit(
            "Unstarted recovery observation refused; original obligations remain held"
        ) from None
    finally:
        provider = metrics.get_meter_provider()
        if isinstance(provider, MeterProvider):
            provider.shutdown(timeout_millis=1000)


if __name__ == "__main__":
    main()
