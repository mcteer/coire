"""Fixed rank-zero collective within an accepted paired maintenance epoch."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models import LinkObservation, LinkProbeCommand, ProbeTransport
from coire_core.models.upgrade_collective import NodeUpgradeCollectiveResult
from coire_core.models.upgrades import NodeUpgradeCollectiveScope
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.link_probes import LinkProbeRunner
from coire_node.maintenance import NodeMaintenance
from coire_node.upgrade_control import NativeUpgradeControl
from coire_node.upgrade_runtime import running_runtime_sha256

tracer = trace.get_tracer("coire.node.upgrades")


@tracer.start_as_current_span(
    "coire.node.upgrade_collective", record_exception=False, set_status_on_exception=False
)
def run_upgrade_collective(
    settings: Settings,
    scope: NodeUpgradeCollectiveScope,
    gate: NodeMaintenance,
    runner: LinkProbeRunner,
) -> LinkObservation:
    scope = NodeUpgradeCollectiveScope.model_validate(scope.model_dump(mode="json"))
    plan = scope.plan

    def verify() -> None:
        if not settings.upgrades_enabled or settings.node_name != "coire-edge-a":
            raise UpgradeUnavailable("Upgrade collective requires the declared native rank zero")
        if datetime.now(UTC) >= scope.request.deadline_at:
            raise UpgradeUnavailable("Upgrade collective original deadline elapsed")
        fence = gate.current()
        if fence is None or not fence.matches_plan(plan):
            raise UpgradeConflict("Upgrade collective maintenance authority differs")
        control = NativeUpgradeControl(settings)
        observed = control.observed()
        if observed is None or not control._guard_ready(observed):
            raise UpgradeUnavailable("Upgrade collective native recovery owner is not ready")
        if running_runtime_sha256(settings) != scope.runtime_pair()[0]:
            raise UpgradeConflict("Upgrade collective rank-zero native graph differs")
        if plan.operation.node == settings.node_name:
            intent = control.journal(plan.operation.job_id).current()
            expected_phase, deadline = (
                ("candidate_selected", intent.readiness_deadline_at)
                if scope.request.stage == "candidate"
                else ("source_selected", intent.recovery_deadline_at)
            )
            if intent.phase != expected_phase or deadline != scope.request.deadline_at:
                raise UpgradeConflict("Upgrade collective captured native stage differs")

    with gate.lock:
        verify()
    root = gate.store
    for part in ("collectives", plan.operation.job_id, scope.request.stage, str(scope.index)):
        root = PrivateUpgradeStore(root.path(part))
    with root.ownership("collective.lock"), gate.transfer_continuation(verify):
        previous = root.read("scope.json", NodeUpgradeCollectiveScope)
        result = root.read("result.json", NodeUpgradeCollectiveResult)
        if previous is not None and previous != scope:
            raise UpgradeConflict("Upgrade collective replay changes its captured authority")
        if result is not None:
            if previous is None:
                raise UpgradeConflict("Upgrade collective result has no retained dispatch")
            if (
                result.job_id,
                result.generation,
                result.plan_sha256,
                result.stage,
                result.deadline_at,
            ) != (
                plan.operation.job_id,
                scope.request.generation,
                scope.request.plan_sha256,
                scope.request.stage,
                scope.request.deadline_at,
            ) or len(result.observations) != 1:
                raise UpgradeConflict("Upgrade collective retained result authority differs")
            return result.observations[0]
        if previous is not None:
            from coire_node.collective_cleanup import retained_cleanup_receipt

            if retained_cleanup_receipt(root, scope) is not None:
                raise UpgradeUnavailable(
                    "Upgrade collective interrupted; cleanup verified, not qualification"
                )
            raise UpgradeUnavailable("Upgrade collective dispatch remains unresolved")
        path = Path(settings.sharding_jaccl_hostfile)
        with path.open("rb") as source:
            payload = source.read(65537)
        if len(payload) > 65536:
            raise UpgradeUnavailable("Upgrade collective hostfile exceeds its bound")
        command = LinkProbeCommand(
            command_id=scope.command_id,
            transport=ProbeTransport.JACCL,
            hostfile_sha256=hashlib.sha256(payload).hexdigest(),
        )
        verify()
        # Interrupted dispatch is uncertain. Neither a restart nor an elapsed
        # deadline can permission a second collective or release maintenance.
        root.write("scope.json", scope, replace=False)
        observation = runner.run(command, deadline_at=scope.request.deadline_at)
        root.write(
            "result.json",
            NodeUpgradeCollectiveResult(
                job_id=plan.operation.job_id,
                generation=scope.request.generation,
                plan_sha256=scope.request.plan_sha256,
                stage=scope.request.stage,
                deadline_at=scope.request.deadline_at,
                observations=(observation,),
            ),
            replace=False,
        )
        # Retain the actual completed worker result before a final readiness
        # check can fail. A retry can read it; it never launches a second probe.
        verify()
        return observation
