"""Evaluation-specific resource and serving guard; never consumes training profiles."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics, trace
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    EngineProcessRow,
    EvaluationAttemptRow,
    EvaluationCoexistenceProfileRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
    InstanceMemberRow,
    MemoryReservationRow,
    NodeMemoryLedgerRow,
    NodeRow,
)
from coire_api.evaluation.catalog import definition_digest
from coire_api.placement.service import lock_nodes_for_admission
from coire_core.models.engine import EngineState
from coire_core.models.evaluation import (
    EvaluationMeasurement,
    EvaluationMeasurementRequest,
    EvaluationReason,
    EvaluationResident,
    EvaluationSuite,
    EvaluationTarget,
    EvaluationWorkload,
)
from coire_core.models.evaluation_inputs import EvaluationTrainingBinding, training_input_shape
from coire_core.models.node import Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.training import TrainingResidentTarget
from coire_scheduler.training_guard import (
    current_residents,
    hardware_digest,
    instance_latency_reason,
)

tracer = trace.get_tracer("coire.scheduler.evaluation")
breaches = metrics.get_meter("coire.scheduler.evaluation").create_counter(
    "coire_evaluation_guard_breaches_total"
)
COUNTED = (
    MemoryReservationState.PENDING,
    MemoryReservationState.HELD,
    MemoryReservationState.RELEASING,
)


def fingerprint(
    suite: EvaluationSuite,
    targets: list[EvaluationTarget],
    residents: list[EvaluationResident],
    node: NodeRow,
    engine_bindings: dict[str, str] | None = None,
    *,
    training: EvaluationTrainingBinding | None = None,
) -> str:
    value = {
        "suite_sha256": definition_digest(suite),
        "training_input_shape": training_input_shape(training),
        "targets": [target.model_dump(mode="json") for target in targets],
        "residents": [
            resident.model_dump(mode="json")
            for resident in sorted(residents, key=lambda item: str(item.instance_id))
        ],
        "hardware_sha256": hardware_digest(node),
        "resident_engines": engine_bindings or {},
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def engine_binding(engine: EngineProcessRow) -> str:
    """Stable process identity shared by inventory and serving authorization."""
    return hashlib.sha256(
        json.dumps(
            {
                "id": str(engine.id),
                "pid": engine.pid,
                "created": engine.process_create_time,
                "template": engine.chat_template_sha256,
                "backend": engine.backend,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


async def resident_engine_bindings(
    session: AsyncSession, residents: list[EvaluationResident]
) -> dict[str, str] | None:
    bindings: dict[str, str] = {}
    for resident in residents:
        engines = (
            await session.scalars(
                select(EngineProcessRow)
                .join(InstanceMemberRow, InstanceMemberRow.engine_id == EngineProcessRow.id)
                .where(InstanceMemberRow.instance_id == resident.instance_id)
            )
        ).all()
        if len(engines) != 1 or engines[0].state is not EngineState.READY:
            return None
        engine = engines[0]
        bindings[str(resident.instance_id)] = engine_binding(engine)
    return bindings


def qualified(report: EvaluationMeasurement, *, now: datetime) -> bool:
    return (
        report.state == "succeeded"
        and report.valid_until is not None
        and report.created_at <= now < report.valid_until <= report.created_at + timedelta(hours=24)
        and bool(report.targets)
        and all(
            item.baseline_requests >= 100
            and item.mixed_requests >= 100
            and item.failures == 0
            and math.isfinite(item.gateway_p95_seconds)
            and item.gateway_p95_seconds <= 0.02
            and item.mixed_p95_seconds <= 1.5
            and item.baseline_p95_seconds <= 1.5
            for item in report.targets
        )
        and report.swap_growth_bytes == 0
        and report.thermal_ok
    )


def report_digest(report: EvaluationMeasurement) -> str:
    return hashlib.sha256(
        json.dumps(
            report.model_dump(mode="json", exclude={"report_sha256"}),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


async def controlled_measurement(
    session: AsyncSession, run: EvaluationRunRow, node: NodeRow
) -> bool:
    """Only a live persisted explicit baseline can authorize unqualified mixed admission."""
    if run.measurement_id is None:
        return False
    row = await session.get(EvaluationMeasurementRow, run.measurement_id, populate_existing=True)
    if (
        row is None
        or row.state != "running"
        or row.owner_user_id != run.owner_user_id
        or row.deadline_at <= datetime.now(UTC)
    ):
        return False
    request = EvaluationMeasurementRequest.model_validate(row.request)
    owned_holds = list(
        (
            await session.scalars(
                select(MemoryReservationRow.id)
                .join(
                    EvaluationAttemptRow,
                    cast(EvaluationAttemptRow.instance_id, String)
                    == MemoryReservationRow.holder_id,
                )
                .where(
                    EvaluationAttemptRow.run_id == run.id,
                    EvaluationAttemptRow.node_id == node.id,
                    EvaluationAttemptRow.owns_instance.is_(True),
                    EvaluationAttemptRow.state != "released",
                    MemoryReservationRow.node_id == node.id,
                    MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                    MemoryReservationRow.state.in_(COUNTED),
                )
            )
        ).all()
    )
    # Exclude proven owned holds before validating READY chat residency. Placement
    # persists the phase hold before its instance member and engine become ready.
    residents = await current_residents(session, [node.id], exclude=owned_holds)
    if residents is None:
        return False
    current = {(item.instance_id, item.target.model_dump_json()) for item in residents}
    declared = {
        (item.instance_id, item.target.model_dump_json()) for item in request.resident_targets
    }
    if current != declared:
        return False
    baseline = row.execution.get("baseline")
    engines = await resident_engine_bindings(session, request.resident_targets)
    if engines is None or engines != row.execution.get("resident_engines"):
        return False
    return (
        request.node == node.name
        and row.execution.get("run_id") == run.id
        and row.execution.get("phase") == "mixed"
        and row.execution.get("suite") == run.suite_snapshot
        and row.execution.get("subjects") == run.subjects
        and isinstance(baseline, dict)
        and set(baseline) == {str(item.instance_id) for item in request.resident_targets}
        and all(
            isinstance(value, dict)
            and value.get("count") == request.requests_per_phase
            and isinstance(value.get("p95"), (int, float))
            and value["p95"] <= 1.5
            and isinstance(value.get("overhead_p95"), (int, float))
            and value["overhead_p95"] <= 0.02
            for value in baseline.values()
        )
    )


async def matching_profile(
    session: AsyncSession, binding: str, *, now: datetime
) -> EvaluationCoexistenceProfileRow | None:
    rows = (
        await session.scalars(
            select(EvaluationCoexistenceProfileRow)
            .where(
                EvaluationCoexistenceProfileRow.fingerprint_sha256 == binding,
                EvaluationCoexistenceProfileRow.expires_at > now,
                EvaluationCoexistenceProfileRow.invalidated_at.is_(None),
            )
            .order_by(EvaluationCoexistenceProfileRow.expires_at.desc())
            .limit(100)
        )
    ).all()
    for profile in rows:
        measurement = await session.get(EvaluationMeasurementRow, profile.measurement_id)
        if measurement is None or measurement.state != "succeeded" or measurement.report is None:
            continue
        try:
            report = EvaluationMeasurement.model_validate(measurement.report)
        except ValueError:
            continue
        if (
            qualified(report, now=now)
            and report.id == measurement.id == profile.measurement_id
            and report.request.model_dump(mode="json") == measurement.request
            and report.report_sha256 == profile.report_sha256 == measurement.report_sha256
            and report_digest(report) == report.report_sha256
            and report.profile_sha256 == binding
            and profile.expires_at == report.valid_until
        ):
            return profile
    return None


async def guard_reason(
    session: AsyncSession, attempt: EvaluationAttemptRow
) -> EvaluationReason | None:
    reason = await _guard_reason(session, attempt)
    if reason is not None:
        breaches.add(1, {"reason": reason})
        if attempt.profile_id is not None and reason in {
            "latency_breach",
            "memory_breach",
            "thermal_breach",
            "telemetry_stale",
        }:
            profile = await session.get(
                EvaluationCoexistenceProfileRow, attempt.profile_id, with_for_update=True
            )
            if profile is not None and profile.invalidated_at is None:
                profile.invalidated_at, profile.invalidated_reason = datetime.now(UTC), reason
    return reason


async def _guard_reason(
    session: AsyncSession, attempt: EvaluationAttemptRow
) -> EvaluationReason | None:
    if attempt.node_id is None:
        return "capacity_busy"
    await lock_nodes_for_admission(session, [attempt.node_id])
    ledger = await session.get(NodeMemoryLedgerRow, attempt.node_id, populate_existing=True)
    now = datetime.now(UTC)
    if (
        ledger is None
        or ledger.health is not Reachability.HEALTHY
        or ledger.health_sampled_at is None
        or not now - timedelta(seconds=60) <= ledger.health_sampled_at <= now
        or ledger.swap_used_bytes is None
        or ledger.measured_resident_bytes is None
    ):
        return "telemetry_stale"
    if ledger.thermal_state in {"serious", "critical"}:
        return "thermal_breach"
    if ledger.swap_used_bytes or ledger.measured_resident_bytes > ledger.budget_bytes:
        return "memory_breach"
    holds = (
        await session.scalars(
            select(MemoryReservationRow).where(
                MemoryReservationRow.node_id == attempt.node_id,
                MemoryReservationRow.state.in_(COUNTED),
            )
        )
    ).all()
    if any(hold.holder_type is ReservationHolder.TRAINING for hold in holds):
        return "capacity_busy"
    # The phase's own engine can still be starting. Its model hold remains
    # counted by admission and the live memory guard, but is not a chat
    # resident that needs a READY serving identity and coexistence evidence.
    # Placement commits the model reservation before attaching its member.
    # Bind ownership to the reservation's exact instance holder, not a member
    # row that may not exist yet during that allocation boundary.
    owned_holds = [
        hold.id
        for hold in holds
        if attempt.owns_instance
        and attempt.instance_id is not None
        and hold.holder_type is ReservationHolder.MODEL
        and hold.holder_id == str(attempt.instance_id)
    ]
    residents = await current_residents(session, [attempt.node_id], exclude=owned_holds)
    if residents is None:
        return "telemetry_stale"
    serving = [
        resident
        for resident in residents
        if not (attempt.owns_instance and resident.instance_id == attempt.instance_id)
    ]
    if serving:
        run = await session.get(EvaluationRunRow, attempt.run_id, populate_existing=True)
        node = await session.get(NodeRow, attempt.node_id, populate_existing=True)
        if run is None or node is None:
            return "telemetry_stale"
        projected = [
            EvaluationResident(instance_id=item.instance_id, target=item.target) for item in serving
        ]
        bindings = await resident_engine_bindings(session, projected)
        if bindings is None:
            return "telemetry_stale"
        binding = fingerprint(
            EvaluationSuite.model_validate(run.suite_snapshot),
            [EvaluationTarget.model_validate(item) for item in run.subjects],
            projected,
            node,
            engine_bindings=bindings,
            training=EvaluationWorkload.model_validate(attempt.workload).training,
        )
        if await matching_profile(
            session, binding, now=now
        ) is None and not await controlled_measurement(session, run, node):
            return "telemetry_stale"
    for resident in residents:
        if attempt.owns_instance and resident.instance_id == attempt.instance_id:
            continue
        reason = await instance_latency_reason(
            session,
            TrainingResidentTarget(instance_id=resident.instance_id, target=resident.target),
            now=now,
        )
        if reason == "latency_breach":
            return "latency_breach"
        if reason == "insufficient_samples":
            return "telemetry_stale"
    return None
