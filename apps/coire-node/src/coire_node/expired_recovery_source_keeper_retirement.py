"""Durable retirement of only the positively bound idle Source guard, never its node."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import psutil
from opentelemetry import metrics, trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationGrant,
    UpgradeColdSourceKeeperRetirement,
)
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeUpgradeClaim,
    PreparedNativeRuntime,
    UpgradeMaintenance,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_support import verify_passive_support_identity
from coire_node.recovery_identity import verify_retired_identity
from coire_node.upgrade_guard import (
    NativeGuardTransport,
    _owned_native_process,
    _owned_node_process,
)
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.source_keeper_retirement")
logger = logging.getLogger(__name__)
retirements = metrics.get_meter("coire.node.source_keeper_retirement").create_counter(
    "coire_node_source_keeper_retirements", description="Verified Source guard retirements"
)


@tracer.start_as_current_span("coire.node.source_keeper_retirement")
def retire_source_keeper(
    settings: Settings,
    grant: UpgradeColdContinuationGrant,
    control: NativeGuardTransport,
    runtime: PreparedNativeRuntime,
) -> None:
    late = grant.binding.post_handback
    if late is None:
        return
    intent = grant.intent
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    run = PrivateUpgradeStore(root.path("expired-recovery") / intent.recovery_id, create=False)
    if (
        control.read_cold_recovery_continuation(intent) != grant
        or control.verify_support_context(intent) != intent
    ):
        raise UpgradeUnavailable("Source keeper retirement has no live exact Core grant")
    actor = verify_passive_support_identity(settings, intent, runtime)
    marker = run.read("source.keeper.retirement.intent.json", UpgradeColdSourceKeeperRetirement)
    if marker is None:
        marker = UpgradeColdSourceKeeperRetirement(
            grant=grant, actor=actor, started_at=datetime.now(UTC)
        )
        run.write("source.keeper.retirement.intent.json", marker, replace=False)
    if marker.grant != grant:
        raise UpgradeConflict("Source keeper retirement changes its retained grant")
    if marker.actor != actor:
        verify_retired_identity(marker.actor)
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(
        envs, envs.parent / "runtimes", grant.binding.original_native.source.runtime
    )
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    status = root.read("status.json", NativeGuardStatus)
    current_node = node.read("node-process.json", NativeNodeProcess)
    current_claim = root.read("claim.json", NativeUpgradeClaim)
    fence = node.read("maintenance.json", UpgradeMaintenance)
    if (
        current_node != grant.binding.retained_node
        or current_claim != late.released_claim
        or fence not in (late.released_maintenance, intent.local_recovery_maintenance)
        or status is None
        or status.process != late.source_helper.process
        or status.selected != late.source_helper.selected
        or status.bootstrap_runtime != late.source_helper.bootstrap_runtime
        or _owned_node_process(current_node.process, python) is None
    ):
        raise UpgradeConflict("Source keeper retirement current released preimages changed")
    previous = run.read("source.keeper.retirement.receipt.json", UpgradeColdSourceKeeperRetirement)
    if previous is not None:
        if (
            previous.grant != grant
            or previous.actor != marker.actor
            or previous.started_at != marker.started_at
            or previous.retired_at is None
        ):
            raise UpgradeConflict("Source keeper retirement receipt is uncertain")
        verify_retired_identity(late.source_helper.process)
        return
    guard = _owned_native_process(late.source_helper.process, python, "coire_node.upgrade_guard")
    if guard is not None:
        from coire_node.expired_recovery_cold_handback import source_writer_lock_held

        if (
            guard.cmdline() != [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"]
            or not status.ready
            or not source_writer_lock_held(root)
            or not 0 <= (datetime.now(UTC) - status.observed_at).total_seconds() <= 5
        ):
            raise UpgradeUnavailable("Source keeper is not its recorded idle sealed guard")
        if (
            control.read_cold_recovery_continuation(intent) != grant
            or control.verify_support_context(intent) != intent
        ):
            raise UpgradeUnavailable("Source keeper retirement authority changed before signal")
        guard.terminate()
        try:
            guard.wait(
                timeout=min(
                    5.0, max(0.001, (intent.deadline_at - datetime.now(UTC)).total_seconds())
                )
            )
        except psutil.TimeoutExpired:
            raise UpgradeUnavailable(
                "Source keeper retirement is not positively complete"
            ) from None
    verify_retired_identity(late.source_helper.process)
    receipt = UpgradeColdSourceKeeperRetirement.model_validate(
        marker.model_dump(mode="json") | {"retired_at": datetime.now(UTC)}
    )
    run.write(
        "source.keeper.retirement.receipt.json",
        receipt,
        replace=False,
    )
    retirements.add(1, {"node": settings.node_name})
    logger.info("Source keeper retired", extra={"job_id": intent.recovery_id})
