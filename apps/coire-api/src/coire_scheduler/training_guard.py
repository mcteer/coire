"""Measured evidence identities and chat-first protective decisions."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import ADMIN
from coire_api.db import (
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeMemoryLedgerRow,
    NodeRow,
    TrainingAttemptRow,
    TrainingCommandRow,
    TrainingJobRow,
    TrainingMeasurementRow,
    TrainingParticipantRow,
    TrainingProfileRow,
    UsageRecordRow,
    session_scope,
)
from coire_api.gateway.targets import ModelNotFoundError, resolve_target
from coire_api.training.telemetry import observed
from coire_core.models.gateway import UsageOutcome
from coire_core.models.instance import InstanceState
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.training import (
    ResolvedTrainingSpec,
    TrainingMeasurementRequest,
    TrainingMeasurementResult,
    TrainingProfile,
    TrainingReason,
    TrainingResidentTarget,
)


def profile_identity(request: TrainingMeasurementRequest, runtime_sha256: str) -> str:
    """Instance multiplicity and exact adapter targets survive canonicalization."""
    payload = request.model_dump(mode="json")
    payload["nodes"] = sorted(payload["nodes"])
    payload["resident_targets"] = sorted(
        payload["resident_targets"], key=lambda item: item["instance_id"]
    )
    return hashlib.sha256(
        json.dumps(
            {"request": payload, "runtime_sha256": runtime_sha256},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def hardware_digest(node: NodeRow) -> str:
    value = {
        "node": node.name,
        "memory_total_bytes": node.memory_total_bytes,
        "gpu_cores": node.gpu_cores,
        "agent_version": node.agent_version,
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def memory_evidence_digest(evidence: BaseModel) -> str:
    value = evidence.model_dump(mode="json")
    value["resource_envelope"].pop("evidence_sha256")
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def measurement_report_digest(report: BaseModel) -> str:
    value = report.model_dump(mode="json")
    value.pop("report_sha256", None)
    value.pop("profile_id", None)
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def profile_eligible(
    profile: TrainingProfile, request: TrainingMeasurementRequest, *, now: datetime | None = None
) -> bool:
    current = now or datetime.now(UTC)
    if profile.invalidated_reason is not None or profile.expires_at <= current:
        return False
    return profile_identity(profile.request, "") == profile_identity(request, "")


def latency_reason(
    samples: Sequence[tuple[datetime, float]], *, observed_at: datetime, now: datetime | None = None
) -> TrainingReason | None:
    """Nearest-rank trailing-window p95, matching coire-ttft-v1.

    Insufficient observations block new mixed admission, but do not constitute
    a confirmed latency breach for an already running attempt.
    """
    current = now or datetime.now(UTC)
    if observed_at > current or current - observed_at > timedelta(seconds=60):
        return "insufficient_samples"
    values = sorted(
        value
        for timestamp, value in samples
        if current - timedelta(minutes=5) <= timestamp <= current
        and math.isfinite(value)
        and value >= 0
    )
    if len(values) < 30:
        return "insufficient_samples"
    return "latency_breach" if values[math.ceil(0.95 * len(values)) - 1] > 1.5 else None


def protective_reason(
    *,
    footprint_bytes: int | None,
    reservation_bytes: int,
    swap_growth_bytes: int,
    thermal_state: str | None,
    latency_reasons: Sequence[TrainingReason | None],
) -> TrainingReason | None:
    if swap_growth_bytes > 0 or (
        footprint_bytes is not None and footprint_bytes > reservation_bytes
    ):
        return "memory_breach"
    if thermal_state in {"serious", "critical"}:
        return "thermal_breach"
    if "latency_breach" in latency_reasons:
        return "latency_breach"
    return None


async def recheck_resource_evidence(
    session: AsyncSession, job: TrainingJobRow, nodes: Sequence[NodeRow]
) -> TrainingReason | None:
    """Re-run the trusted resolver; a frozen envelope is not itself measured evidence.

    Never replace the submitted resolution with new defaults or a different profile.
    Evidence must still cover the actual selected hardware, not merely another Studio.
    """
    return (
        None if await matching_profile(session, job, nodes, residents=None) else "profile_missing"
    )


async def current_residents(
    session: AsyncSession, node_ids: Sequence[uuid.UUID], *, exclude: Sequence[uuid.UUID] = ()
) -> list[TrainingResidentTarget] | None:
    """Inventory counted model holds, including exact adapter and instance multiplicity.

    Unknown/legacy holders and non-ready occupancy cannot become a measured match.
    """
    holds = (
        await session.scalars(
            select(MemoryReservationRow).where(
                MemoryReservationRow.node_id.in_(node_ids),
                MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                MemoryReservationRow.state.in_(
                    [
                        MemoryReservationState.PENDING,
                        MemoryReservationState.HELD,
                        MemoryReservationState.RELEASING,
                    ]
                ),
                MemoryReservationRow.id.not_in(exclude),
            )
        )
    ).all()
    identities: dict[uuid.UUID, TrainingResidentTarget] = {}
    for hold in holds:
        member = await session.scalar(
            select(InstanceMemberRow).where(InstanceMemberRow.reservation_id == hold.id)
        )
        instance = await session.get(ModelInstanceRow, member.instance_id) if member else None
        if instance is None or instance.state != InstanceState.READY or instance.variant_id is None:
            return None
        try:
            from coire_api.db import TrainingAdapterRow

            adapter = (
                await session.get(TrainingAdapterRow, instance.adapter_id)
                if instance.adapter_id
                else None
            )
            target = await resolve_target(
                session,
                adapter.selector if adapter else instance.model_id,
                ADMIN,
                instance.variant_id,
            )
        except (LookupError, ModelNotFoundError):
            return None
        if target.identity is None:
            return None
        identities[instance.id] = TrainingResidentTarget(
            instance_id=instance.id, target=target.identity
        )
    return sorted(identities.values(), key=lambda item: str(item.instance_id))


async def matching_profile(
    session: AsyncSession,
    job: TrainingJobRow,
    nodes: Sequence[NodeRow],
    *,
    residents: list[TrainingResidentTarget] | None,
) -> TrainingProfile | None:
    """Validate actual immutable reports, never an asserted profile pass flag.

    New evidence can authorize the same frozen conservative upper bound, without
    rewriting resolved intent. Output names are not execution configuration.
    """
    from coire_api.training.service import payload_digest
    from coire_api.training.specs import training_config_digest
    from coire_core.settings import get_settings

    resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
    if payload_digest(resolved) != job.resolved_sha256:
        return None
    now = datetime.now(UTC)
    profiles = (
        await session.scalars(
            select(TrainingProfileRow)
            .where(
                TrainingProfileRow.invalidated_reason.is_(None),
                TrainingProfileRow.valid_until > now,
            )
            .order_by(TrainingProfileRow.created_at.desc())
            .limit(100)
        )
    ).all()
    for row in profiles:
        measurement = await session.get(TrainingMeasurementRow, row.measurement_id)
        if measurement is None or measurement.state != "succeeded" or measurement.report is None:
            continue
        try:
            report = TrainingMeasurementResult.model_validate(measurement.report)
            profile = TrainingProfile.model_validate(row.profile)
        except ValueError:
            continue
        evidence = report.memory_evidence
        if (
            evidence is None
            or report.state != "succeeded"
            or report.id != measurement.id
            or report.request.model_dump(mode="json") != measurement.request
            or profile.id != row.id
            or report.profile_id != row.id
            or row.identity_sha256 != profile_identity(report.request, resolved.runtime_sha256)
            or profile.request != report.request
            or profile.invalidated_reason is not None
            or profile.expires_at != row.valid_until
            or profile.report_sha256 != row.report_sha256
            or report.report_sha256 != row.report_sha256
            or report.report_sha256 != measurement.report_sha256
            or report.report_sha256 != measurement_report_digest(report)
            or training_config_digest(report.request.spec) != training_config_digest(resolved.spec)
            or evidence.training_config_sha256 != training_config_digest(resolved.spec)
            or evidence.measurement_id != report.id
            or evidence.completed_updates > report.completed_updates
            or evidence.base_manifest_sha256 != resolved.base_manifest_sha256
            or evidence.tokenizer_sha256 != resolved.tokenizer_sha256
            or evidence.template_sha256 != resolved.template_sha256
            or evidence.runtime_sha256 != resolved.runtime_sha256
            or evidence.worker_version != resolved.worker_version
            or not evidence.measured_at <= now < evidence.valid_until
            or evidence.valid_until
            > evidence.measured_at + timedelta(seconds=get_settings().training_profile_ttl_s)
            or evidence.resource_envelope.evidence_sha256 != memory_evidence_digest(evidence)
            or set(report.request.nodes) != {node.name for node in nodes}
            or {item.node for item in evidence.nodes} != {node.name for node in nodes}
            or any(
                item.hardware_sha256
                != next(hardware_digest(node) for node in nodes if node.name == item.node)
                or item.swap_growth_bytes
                or not item.thermal_ok
                or item.peak_footprint_bytes > evidence.resource_envelope.memory_bytes
                for item in evidence.nodes
            )
            or evidence.resource_envelope.memory_bytes > resolved.resource_envelope.memory_bytes
            or evidence.resource_envelope.checkpoint_bytes
            > resolved.resource_envelope.checkpoint_bytes
        ):
            continue
        if residents is not None:
            expected = sorted(residents, key=lambda item: str(item.instance_id))
            actual = sorted(report.request.resident_targets, key=lambda item: str(item.instance_id))
            if actual != expected or (expected and report.request.mode != "coexistence"):
                continue
            if expected:
                phases = {phase.phase: phase for phase in report.phases}
                if set(phases) != {"baseline", "mixed"} or len(report.phases) != 2:
                    continue
                if any(
                    (phase.finished_at - phase.started_at).total_seconds() < 900
                    or phase.workload_sha256 != report.request.workload.sha256
                    or phase.failures
                    or set(phase.samples) != {item.instance_id for item in expected}
                    or any(len(samples) < 100 for samples in phase.samples.values())
                    for phase in report.phases
                ):
                    continue
                summaries = {item.instance_id: item for item in report.targets}
                inconsistent = False
                for item in expected:
                    summary = summaries[item.instance_id]
                    for phase in report.phases:
                        count, recorded_p95 = (
                            (summary.baseline_requests, summary.baseline_p95_seconds)
                            if phase.phase == "baseline"
                            else (summary.mixed_requests, summary.mixed_p95_seconds)
                        )
                        values = sorted(phase.samples[item.instance_id])
                        if (
                            count != len(values)
                            or values[math.ceil(0.95 * len(values)) - 1] != recorded_p95
                        ):
                            inconsistent = True
                if inconsistent:
                    continue
        locked = await session.get(
            TrainingProfileRow, row.id, populate_existing=True, with_for_update=True
        )
        if (
            locked is None
            or locked.invalidated_reason is not None
            or locked.valid_until <= datetime.now(UTC)
        ):
            continue
        return profile
    return None


async def instance_latency_reason(
    session: AsyncSession, resident: TrainingResidentTarget, *, now: datetime | None = None
) -> TrainingReason | None:
    """Exact-instance rolling coire-ttft-v1 nearest-rank p95, aggregated in Postgres."""
    current = now or datetime.now(UTC)
    timestamp = getattr(UsageRecordRow, "first_token_at", None)
    duration = getattr(UsageRecordRow, "first_token_duration_ms", None)
    instance = getattr(UsageRecordRow, "instance_id", None)
    if timestamp is None or duration is None or instance is None:
        return "insufficient_samples"
    count, newest, p95 = (
        await session.execute(
            select(
                func.count(UsageRecordRow.id),
                func.max(timestamp),
                func.percentile_disc(0.95).within_group(duration),
            ).where(
                instance == resident.instance_id,
                UsageRecordRow.model_id == resident.target.model_id,
                UsageRecordRow.variant_id == resident.target.variant_id,
                UsageRecordRow.adapter_id == resident.target.adapter_id,
                UsageRecordRow.outcome == UsageOutcome.SUCCEEDED,
                UsageRecordRow.prompt_tokens > 0,
                UsageRecordRow.prompt_tokens <= 4000,
                UsageRecordRow.completion_tokens > 0,
                timestamp >= current - timedelta(minutes=5),
                timestamp >= UsageRecordRow.started_at,
                timestamp <= current,
                UsageRecordRow.finished_at <= current,
                duration >= 0,
                duration < float("inf"),
            )
        )
    ).one()
    if count < 30 or newest is None or current - newest > timedelta(seconds=60):
        return "insufficient_samples"
    return "latency_breach" if float(p95) > 1500 else None


@observed("coire.scheduler.training.guard")
async def guard_reason(
    attempt_id: str, *, session: AsyncSession | None = None
) -> TrainingReason | None:
    if session is None:
        async with session_scope() as owned:
            return await guard_reason(attempt_id, session=owned)
    attempt = await session.get(TrainingAttemptRow, attempt_id)
    if attempt is None:
        return "insufficient_samples"
    participants = (
        await session.scalars(
            select(TrainingParticipantRow).where(TrainingParticipantRow.attempt_id == attempt_id)
        )
    ).all()
    if len(participants) != attempt.world_size:
        return "insufficient_samples"
    from coire_api.placement.service import lock_nodes_for_admission

    await lock_nodes_for_admission(session, [p.node_id for p in participants])
    baseline = await session.get(
        TrainingCommandRow, uuid.uuid5(uuid.NAMESPACE_URL, f"coire:admission-evidence:{attempt_id}")
    )
    baseline_swaps = baseline.payload.get("swap_bytes") if baseline else None
    insufficient = False
    now = datetime.now(UTC)
    for p in participants:
        ledger = await session.get(NodeMemoryLedgerRow, p.node_id)
        hold = await session.get(MemoryReservationRow, p.reservation_id)
        if (
            ledger is None
            or hold is None
            or ledger.health_sampled_at is None
            or not now - timedelta(seconds=60) <= ledger.health_sampled_at <= now
        ):
            insufficient = True
            continue
        if p.footprint_bytes is not None and p.footprint_bytes > hold.bytes:
            return "memory_breach"
        if ledger.thermal_state in {"serious", "critical"}:
            return "thermal_breach"
        swap = getattr(ledger, "swap_used_bytes", None)
        initial_swap = (
            baseline_swaps.get(str(p.node_id)) if isinstance(baseline_swaps, dict) else None
        )
        if isinstance(swap, int) and isinstance(initial_swap, int):
            if swap > initial_swap:
                return "memory_breach"
        else:
            insufficient = True
        if (
            ledger.measured_resident_bytes is not None
            and ledger.measured_resident_bytes > ledger.budget_bytes
        ):
            return "memory_breach"
        if ledger.health.value != "healthy" or ledger.thermal_state not in {"nominal", "fair"}:
            insufficient = True
    residents = await current_residents(session, [p.node_id for p in participants])
    if residents is None:
        insufficient = True
    else:
        for resident in residents:
            reason = await instance_latency_reason(session, resident, now=now)
            if reason == "latency_breach":
                # Only profiles matching this execution are invalidated. A new
                # measurement is mandatory before protective latency auto-resume.
                if baseline:
                    profile_id = baseline.payload.get("profile_id")
                    if isinstance(profile_id, str):
                        profile = await session.get(
                            TrainingProfileRow, uuid.UUID(profile_id), with_for_update=True
                        )
                        if profile:
                            profile.invalidated_reason = "latency_breach"
                return reason
            insufficient |= reason == "insufficient_samples"
    return "insufficient_samples" if insufficient else None


async def resume_profile(
    job_id: str, *, session: AsyncSession | None = None
) -> TrainingProfile | None:
    if session is None:
        async with session_scope() as owned:
            return await resume_profile(job_id, session=owned)
    job = await session.get(TrainingJobRow, job_id)
    if job is None or job.pause_origin != "protective" or job.state != "paused":
        return None
    attempt = await session.scalar(
        select(TrainingAttemptRow)
        .where(TrainingAttemptRow.job_id == job_id)
        .order_by(TrainingAttemptRow.generation.desc())
        .limit(1)
    )
    if attempt is None or await guard_reason(attempt.id, session=session) is not None:
        return None
    participants = (
        await session.scalars(
            select(TrainingParticipantRow).where(TrainingParticipantRow.attempt_id == attempt.id)
        )
    ).all()
    nodes = list(
        (
            await session.scalars(
                select(NodeRow).where(NodeRow.id.in_([p.node_id for p in participants]))
            )
        ).all()
    )
    residents = await current_residents(session, [node.id for node in nodes])
    return (
        await matching_profile(session, job, nodes, residents=residents)
        if residents is not None
        else None
    )
