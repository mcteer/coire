"""Qualify only the exact accepted Studio pair inside its original native epoch."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import LinkObservationRow
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.sharding import append_observation
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.grants import initiating_actor, verify_fenced
from coire_api.upgrades.persistence import get_operation
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models import ProbeOutcome, ProbeTransport
from coire_core.models.upgrade_collective import NodeUpgradeCollectiveResult
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    TERMINAL_UPGRADE_PHASES,
    NodeUpgradeCollectiveRead,
    NodeUpgradeCollectiveScope,
    NodeUpgradeCommand,
    StudioName,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeOperation,
    UpgradePhase,
)

tracer = trace.get_tracer("coire.api.upgrades")


@tracer.start_as_current_span(
    "coire.api.upgrade_collective", record_exception=False, set_status_on_exception=False
)
async def qualify_collective(
    session: AsyncSession,
    requester: StudioName,
    job_id: str,
    body: NodeUpgradeCollectiveRead,
    client: NodeClient,
    *,
    freshness_s: float = 120.0,
) -> NodeUpgradeCollectiveResult:
    async def authority() -> UpgradeExecutionPlan:
        row = await get_operation(session, job_id)
        if row.execution_plan is None or row.node_command is None:
            raise UpgradeConflict("Native collective has no consumed accepted authority")
        plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
        operation = UpgradeOperation.model_validate(row.payload)
        command = NodeUpgradeCommand.model_validate(row.node_command)
        receipt = command.activation_receipt
        if (
            operation.kind is not UpgradeKind.STUDIO
            or operation.node != requester
            or operation.phase in TERMINAL_UPGRADE_PHASES
            or plan.peer_runtime_sha256 is None
            or (body.generation, body.plan_sha256)
            != (operation.generation, plan.authority_sha256())
            or operation.authority_sha256() != plan.operation.authority_sha256()
            or command.authority_sha256() != plan.authority_sha256()
            or receipt is None
            or command.activation_grant is None
            or receipt.phase is not UpgradePhase.ACTIVATING
            or receipt.outcome != "intent"
            or operation.receipt_sequence < receipt.sequence
        ):
            raise UpgradeConflict("Native collective accepted owner differs")
        original = receipt.occurred_at + timedelta(seconds=operation.timing.readiness_s)
        if (body.stage == "candidate" and body.deadline_at != original) or (
            body.stage == "source"
            and body.deadline_at > original + timedelta(seconds=operation.timing.recovery_s)
        ):
            raise UpgradeConflict("Native collective original deadline differs")
        if datetime.now(UTC) >= body.deadline_at:
            raise UpgradeUnavailable("Native collective original deadline elapsed")
        if body.stage == "candidate":
            if operation.cancel_requested:
                raise UpgradeConflict("Cancelled candidate cannot start collective qualification")
            await authorize(session, initiating_actor(row))
        await verify_fenced(session, plan)
        # Validate all derived runtime/scope fields before collecting any node work.
        NodeUpgradeCollectiveScope(
            plan=plan,
            request=body,
            index=0,
            command_id=uuid5(
                NAMESPACE_URL,
                "coire-upgrade-collective/"
                + job_id
                + "/"
                + body.stage
                + "/"
                + body.plan_sha256
                + "/0",
            ),
        )
        return plan

    plan = await authority()
    # No identity-row barrier may span a remote worker. Recheck live authority
    # after each result before retaining it; revocation must remain possible
    # while an already dispatched probe drains.
    await session.commit()
    expected_pair = (
        (body.runtime_sha256, plan.peer_runtime_sha256)
        if requester == "coire-edge-a"
        else (plan.peer_runtime_sha256, body.runtime_sha256)
    )

    async def observe_pair() -> None:
        names: tuple[StudioName, StudioName] = ("coire-edge-a", "coire-edge-b")
        try:
            statuses = await asyncio.gather(*(client.upgrade_status(name) for name in names))
            inventories = await asyncio.gather(
                *(client.upgrade_inventory(name, job_id, body.generation) for name in names)
            )
        except NodeError:
            raise UpgradeUnavailable("Native collective pair cannot be observed") from None
        now = datetime.now(UTC)
        for name, expected, status, inventory in zip(
            names, expected_pair, statuses, inventories, strict=True
        ):
            if (
                not status.bootstrap_ready
                or (status.node, status.runtime_sha256) != (name, expected)
                or not 0 <= (now - status.observed_at).total_seconds() <= 5
                or (
                    inventory.node,
                    inventory.job_id,
                    inventory.generation,
                    inventory.runtime_sha256,
                )
                != (name, job_id, body.generation, expected)
                or not 0 <= (now - inventory.observed_at).total_seconds() <= 5
                or not inventory.idle(NODE_UPGRADE_OWNERS)
                or (
                    name == requester
                    and (status.job_id, status.generation) != (job_id, body.generation)
                )
                or (
                    name == requester
                    and status.phase
                    is not (
                        UpgradePhase.VERIFYING
                        if body.stage == "candidate"
                        else UpgradePhase.VERIFYING_SOURCE
                    )
                )
            ):
                raise UpgradeUnavailable("Native collective requires the fresh idle exact pair")

    observations = []
    for index in (0, 1, 2):
        await authority()
        await session.commit()
        await observe_pair()
        scope = NodeUpgradeCollectiveScope(
            plan=plan,
            request=body,
            index=index,
            command_id=uuid5(
                NAMESPACE_URL,
                "coire-upgrade-collective/"
                + job_id
                + "/"
                + body.stage
                + "/"
                + body.plan_sha256
                + "/"
                + str(index),
            ),
        )
        try:
            observed = await client.run_upgrade_collective(scope)
        except NodeError:
            # A refused or lost response is not a failed measurement. The node
            # retains the fixed dispatch/result, so retry its exact scope within
            # the original epoch rather than publishing synthetic evidence.
            raise UpgradeUnavailable("Native collective result is unavailable") from None
        await authority()
        await observe_pair()
        now = datetime.now(UTC)
        if (
            observed.transport is not ProbeTransport.JACCL
            or (observed.node_a, observed.node_b) != ("coire-edge-a", "coire-edge-b")
            or not 0 <= (now - observed.observed_at).total_seconds() <= freshness_s
            or observed.observed_at < plan.operation.accepted_at
            or (
                observed.outcome is ProbeOutcome.SUCCEEDED
                and (observed.runtime_sha256_a, observed.runtime_sha256_b) != expected_pair
            )
        ):
            raise UpgradeConflict("Native collective returned foreign or stale evidence")
        if observed.outcome is not ProbeOutcome.SUCCEEDED:
            # Both authenticated endpoints were independently attested before and
            # after this failed qualification; the pair cannot retain older eligibility.
            observed = observed.model_copy(
                update={
                    "runtime_sha256_a": expected_pair[0],
                    "runtime_sha256_b": expected_pair[1],
                }
            )
        retained = await session.get(LinkObservationRow, observed.id)
        if retained is None:
            await append_observation(session, observed)
        elif any(
            getattr(retained, field) != getattr(observed, field)
            for field in (
                "transport",
                "outcome",
                "bandwidth_bytes_per_second",
                "latency_ms",
                "os_version_a",
                "os_version_b",
                "engine_version",
                "runtime_sha256_a",
                "runtime_sha256_b",
                "reason",
                "observed_at",
            )
        ):
            raise UpgradeConflict("Native collective replay changes retained evidence")
        observations.append(observed)
        await session.commit()
        if observed.outcome is not ProbeOutcome.SUCCEEDED:
            break
    return NodeUpgradeCollectiveResult(
        job_id=job_id,
        generation=body.generation,
        plan_sha256=body.plan_sha256,
        stage=body.stage,
        deadline_at=body.deadline_at,
        observations=tuple(observations),
    )
