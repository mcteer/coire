"""Controlled baseline/mixed sampling; interrupted samples never mint a profile."""

import asyncio
import math
import uuid
from datetime import UTC, datetime, timedelta

from dbos import DBOS
from opentelemetry import trace
from pydantic import TypeAdapter
from sqlalchemy import select, text

from coire_api.auth import Principal
from coire_api.db import (
    AgentRunRow,
    EvaluationAttemptRow,
    EvaluationCoexistenceProfileRow,
    EvaluationMeasurementRow,
    EvaluationRunRow,
    NodeMemoryLedgerRow,
    NodeRow,
    session_scope,
)
from coire_api.evaluation.authorization import authorize_live_evaluation_action
from coire_api.evaluation.gateway_measurements import generate
from coire_api.evaluation.measurements import project
from coire_api.nodes_client import NodeClient
from coire_core.errors import EvaluationConflict, EvaluationForbidden
from coire_core.models.evaluation import (
    EvaluationLatency,
    EvaluationMeasurement,
    EvaluationMeasurementRequest,
    EvaluationPressureStop,
    EvaluationProbePrepared,
    EvaluationProbeSummary,
    EvaluationSuite,
    EvaluationTarget,
    EvaluationWorkload,
    canonical_digest,
)
from coire_core.models.evaluation_inputs import EvaluationTrainingBinding
from coire_core.models.runs import AgentRunState
from coire_core.settings import get_settings
from coire_scheduler.evaluation_guard import fingerprint, report_digest, resident_engine_bindings
from coire_scheduler.evaluations import phase_plan, request_stop

tracer = trace.get_tracer("coire.scheduler.evaluation.measurement")


def percentile(values: list[float]) -> float:
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise EvaluationConflict("Measurement samples are absent or invalid")
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


async def fail(identity: uuid.UUID, reason: str = "internal") -> None:
    async with session_scope() as session:
        row = await session.get(EvaluationMeasurementRow, identity)
        if row is None:
            return
        try:
            await authorize_live_evaluation_action(
                session, Principal.model_validate(row.authorization_snapshot)
            )
        except Exception:
            reason = "authorization_revoked"
        row = await session.get(
            EvaluationMeasurementRow, identity, with_for_update=True, populate_existing=True
        )
        assert row is not None
        if row.state not in {"succeeded", "failed", "cancelled"}:
            row.state, row.version = "failed", row.version + 1
            report = project(row).model_copy(update={"reason": reason})
            row.report = report.model_dump(mode="json")
        run = await session.get(EvaluationRunRow, row.execution.get("run_id"), with_for_update=True)
        if run is not None and run.state not in {"succeeded", "failed", "timed_out", "cancelled"}:
            await request_stop(session, run, "cancelled")


async def check(
    identity: uuid.UUID, ordinal: int | None = None
) -> tuple[Principal, EvaluationMeasurementRequest, EvaluationProbePrepared]:
    async with session_scope() as session:
        row = await session.get(EvaluationMeasurementRow, identity, populate_existing=True)
        if row is None or row.state != "running" or row.deadline_at <= datetime.now(UTC):
            raise EvaluationConflict("Measurement stopped or deadline elapsed")
        principal = Principal.model_validate(row.authorization_snapshot)
        await authorize_live_evaluation_action(session, principal)
        request = EvaluationMeasurementRequest.model_validate(row.request)
        probes = EvaluationProbePrepared.model_validate(row.execution.get("probes"))
        node = await session.scalar(select(NodeRow).where(NodeRow.name == request.node))
        ledger = (
            await session.get(NodeMemoryLedgerRow, node.id, populate_existing=True)
            if node
            else None
        )
        now = datetime.now(UTC)
        if (
            ledger is None
            or ledger.health_sampled_at is None
            or not now - timedelta(seconds=60) <= ledger.health_sampled_at <= now
            or ledger.swap_used_bytes != 0
            or ledger.thermal_state not in {"nominal", "fair"}
        ):
            raise EvaluationConflict("Measurement lacks live no-swap and thermal evidence")
        if ordinal is not None:
            attempt = await session.scalar(
                select(EvaluationAttemptRow).where(
                    EvaluationAttemptRow.run_id == row.execution.get("run_id"),
                    EvaluationAttemptRow.ordinal == ordinal,
                )
            )
            child = (
                await session.get(AgentRunRow, attempt.agent_run_id)
                if attempt and attempt.agent_run_id
                else None
            )
            if (
                attempt is None
                or child is None
                or child.state is not AgentRunState.RUNNING
                or attempt.collected_sha256 is not None
            ):
                raise EvaluationConflict(
                    "Mixed serving sample lacks active controlled suite pressure"
                )
        return principal, request, probes


async def sample(identity: uuid.UUID, ordinal: int | None) -> dict[str, EvaluationProbeSummary]:
    principal, request, probes = await check(identity, ordinal)
    settings = get_settings()
    semaphore = asyncio.Semaphore(request.concurrency)
    timings: dict[uuid.UUID, list[float]] = {
        item.instance_id: [] for item in request.resident_targets
    }
    overheads: dict[uuid.UUID, list[float]] = {
        item.instance_id: [] for item in request.resident_targets
    }

    async def one(index: int, resident_index: int) -> None:
        async with semaphore:
            await check(identity, ordinal)
            resident = request.resident_targets[resident_index]
            async with asyncio.timeout(settings.evaluation_timeout_seconds):
                completion = await generate(
                    settings,
                    principal,
                    identity,
                    resident,
                    probes.prompts[index % len(probes.prompts)],
                    request.max_output_tokens,
                )
            if completion.instance_id != resident.instance_id:
                raise EvaluationConflict("Serving sample instance changed")
            timings[resident.instance_id].append(completion.first_token_seconds)
            overheads[resident.instance_id].append(completion.gateway_seconds)

    for index in range(request.requests_per_phase):
        began = asyncio.get_running_loop().time()
        async with asyncio.TaskGroup() as tasks:
            for resident_index in range(len(request.resident_targets)):
                tasks.create_task(one(index, resident_index))
        await asyncio.sleep(
            max(0, request.arrival_interval_ms / 1000 - (asyncio.get_running_loop().time() - began))
        )
    await check(identity, ordinal)
    return {
        str(target.instance_id): EvaluationProbeSummary(
            count=len(timings[target.instance_id]),
            p95=percentile(timings[target.instance_id]),
            overhead_p95=percentile(overheads[target.instance_id]),
        )
        for target in request.resident_targets
    }


async def wait_phase(identity: uuid.UUID, ordinal: int) -> EvaluationAttemptRow:
    probe_index = 0
    while True:
        principal, request, probes = await check(identity)
        async with session_scope() as session:
            row = await session.get(EvaluationMeasurementRow, identity)
            assert row is not None
            run = await session.get(EvaluationRunRow, row.execution.get("run_id"))
            if run is None or run.state in {"succeeded", "failed", "timed_out", "cancelled"}:
                raise EvaluationConflict("Measurement execution ended before its declared phase")
            attempt = await session.scalar(
                select(EvaluationAttemptRow).where(
                    EvaluationAttemptRow.run_id == run.id, EvaluationAttemptRow.ordinal == ordinal
                )
            )
            child = (
                await session.get(AgentRunRow, attempt.agent_run_id)
                if attempt and attempt.agent_run_id
                else None
            )
            if attempt and child and child.state is AgentRunState.RUNNING:
                return attempt
        # Keep actual serving evidence fresh during checksum/load/cleanup gaps.
        # These are continuity requests, never claimed as active mixed samples.
        # Failure still makes qualification inconclusive; no telemetry is fabricated.
        for resident in request.resident_targets:
            await check(identity)
            await generate(
                get_settings(),
                principal,
                identity,
                resident,
                probes.prompts[probe_index % len(probes.prompts)],
                request.max_output_tokens,
            )
        probe_index += 1
        await asyncio.sleep(1)


@DBOS.step()
async def execute_measurement(identity_text: str) -> None:
    identity = uuid.UUID(identity_text)
    settings = get_settings()
    with tracer.start_as_current_span(
        "coire.scheduler.evaluation.measurement",
        attributes={"measurement_id": identity_text},
        record_exception=False,
        set_status_on_exception=False,
    ):
        try:
            async with session_scope() as session:
                row = await session.get(EvaluationMeasurementRow, identity)
                if row is None or row.state in {"succeeded", "failed", "cancelled"}:
                    return
                principal = Principal.model_validate(row.authorization_snapshot)
                await authorize_live_evaluation_action(session, principal)
                row = await session.get(
                    EvaluationMeasurementRow, identity, with_for_update=True, populate_existing=True
                )
                assert row is not None
                if row.state != "running" or row.execution.get("phase") != "claimed":
                    raise EvaluationConflict("Interrupted measurement is inconclusive")
                row.state, row.version = "running", row.version + 1
                row.execution = {**row.execution, "phase": "baseline_active"}
                suite = EvaluationSuite.model_validate(row.execution["suite"])
                subjects = TypeAdapter(list[EvaluationTarget]).validate_python(
                    row.execution["subjects"]
                )
                phases = phase_plan(suite, subjects)
            baseline = await sample(identity, None)
            passing = all(
                value.p95 <= 1.5 and value.overhead_p95 <= 0.02 for value in baseline.values()
            )
            async with session_scope() as session:
                row = await session.get(
                    EvaluationMeasurementRow, identity, with_for_update=True, populate_existing=True
                )
                if row is None or row.state != "running":
                    raise EvaluationConflict("Measurement authority ended before baseline commit")
                row.execution = {
                    **row.execution,
                    "baseline": {
                        key: value.model_dump(mode="json") for key, value in baseline.items()
                    },
                    "phase": "mixed" if passing else "baseline_complete",
                }
            if not passing:
                await fail(identity, "latency_breach")
                return
            mixed: list[dict[str, EvaluationProbeSummary]] = []
            for ordinal in range(1, len(phases) + 1):
                attempt = await wait_phase(identity, ordinal)
                measured = await sample(identity, ordinal)
                mixed.append(measured)
                workload = EvaluationWorkload.model_validate(attempt.workload)
                async with NodeClient(settings) as client:
                    async with session_scope() as session:
                        node = await session.get(NodeRow, attempt.node_id)
                        if node is None:
                            raise EvaluationConflict("Pressure node disappeared")
                        name = node.name
                    await client.stop_evaluation_pressure(
                        name,
                        EvaluationPressureStop(
                            run_id=workload.run_id,
                            measurement_id=identity,
                            request_sha256=canonical_digest(workload),
                        ),
                    )
            while True:
                await check(identity)
                async with session_scope() as session:
                    row = await session.get(EvaluationMeasurementRow, identity)
                    assert row is not None
                    run = await session.get(EvaluationRunRow, row.execution.get("run_id"))
                    if run is None or run.state in {"failed", "timed_out", "cancelled"}:
                        raise EvaluationConflict("Controlled suite execution did not complete")
                    if run.state == "succeeded" and run.cleanup_state == "complete":
                        break
                await asyncio.sleep(1)
            async with session_scope() as session:
                row = await session.get(EvaluationMeasurementRow, identity)
                assert row is not None
                await authorize_live_evaluation_action(
                    session, Principal.model_validate(row.authorization_snapshot)
                )
                row = await session.get(
                    EvaluationMeasurementRow, identity, with_for_update=True, populate_existing=True
                )
                assert row is not None
                request = EvaluationMeasurementRequest.model_validate(row.request)
                node = await session.scalar(select(NodeRow).where(NodeRow.name == request.node))
                if node is None or fingerprint(
                    suite,
                    subjects,
                    request.resident_targets,
                    node,
                    await resident_engine_bindings(session, request.resident_targets),
                    training=EvaluationTrainingBinding.model_validate(row.execution["training"])
                    if row.execution.get("training")
                    else None,
                ) != row.execution.get("fingerprint"):
                    raise EvaluationConflict("Measurement hardware or runtime fingerprint changed")
                targets = []
                for resident in request.resident_targets:
                    key = str(resident.instance_id)
                    base = baseline[key]
                    samples = [phase[key] for phase in mixed]
                    targets.append(
                        EvaluationLatency(
                            instance_id=resident.instance_id,
                            baseline_requests=base.count,
                            mixed_requests=sum(value.count for value in samples),
                            baseline_p95_seconds=base.p95,
                            mixed_p95_seconds=max(value.p95 for value in samples),
                            gateway_p95_seconds=max(
                                base.overhead_p95, *(value.overhead_p95 for value in samples)
                            ),
                            failures=0,
                        )
                    )
                report = EvaluationMeasurement(
                    id=row.id,
                    version=row.version + 1,
                    state="succeeded",
                    request=request,
                    targets=targets,
                    thermal_ok=True,
                    swap_growth_bytes=0,
                    profile_sha256=str(row.execution["fingerprint"]),
                    report_sha256="0" * 64,
                    valid_until=row.created_at + timedelta(hours=24),
                    created_at=row.created_at,
                )
                report = report.model_copy(update={"report_sha256": report_digest(report)})
                row.state, row.version, row.report_sha256, row.report = (
                    "succeeded",
                    report.version,
                    report.report_sha256,
                    report.model_dump(mode="json"),
                )
                row.execution = {
                    **row.execution,
                    "mixed": [
                        {key: value.model_dump(mode="json") for key, value in phase.items()}
                        for phase in mixed
                    ],
                    "phase": "complete",
                }
                session.add(
                    EvaluationCoexistenceProfileRow(
                        measurement_id=row.id,
                        fingerprint_sha256=report.profile_sha256,
                        report_sha256=report.report_sha256,
                        expires_at=report.valid_until,
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            await fail(identity, "invalid_evidence")


@DBOS.workflow(name="coire.evaluation.measurement", max_recovery_attempts=100)
async def measurement_workflow(identity: str) -> None:
    while not await claim_measurement(identity):
        await DBOS.sleep_async(1)
    await execute_measurement(identity)


@DBOS.step()
async def claim_measurement(identity_text: str) -> bool:
    identity = uuid.UUID(identity_text)
    async with session_scope() as session:
        row = await session.get(EvaluationMeasurementRow, identity)
        if row is None or row.state in {"succeeded", "failed", "cancelled", "running"}:
            return True
        revoked = False
        try:
            await authorize_live_evaluation_action(
                session, Principal.model_validate(row.authorization_snapshot)
            )
        except EvaluationForbidden:
            revoked = True
        await session.execute(text("SELECT pg_advisory_xact_lock(170100)"))
        row = await session.get(
            EvaluationMeasurementRow, identity, with_for_update=True, populate_existing=True
        )
        assert row is not None
        if (
            revoked
            or row.deadline_at <= datetime.now(UTC)
            or not get_settings().evaluations_enabled
        ):
            row.state, row.version = "failed", row.version + 1
            report = project(row).model_copy(
                update={
                    "reason": "authorization_revoked"
                    if revoked
                    else "admission_disabled"
                    if not get_settings().evaluations_enabled
                    else "capacity_timeout"
                }
            )
            row.report = report.model_dump(mode="json")
            run = await session.get(
                EvaluationRunRow, row.execution.get("run_id"), with_for_update=True
            )
            if run is not None:
                await request_stop(session, run, "admission_disabled")
            return True
        other = await session.scalar(
            select(EvaluationMeasurementRow.id)
            .where(
                EvaluationMeasurementRow.state == "running", EvaluationMeasurementRow.id != identity
            )
            .limit(1)
        )
        working = await session.scalar(
            select(EvaluationRunRow.id)
            .where(
                EvaluationRunRow.started_at.is_not(None),
                EvaluationRunRow.state.notin_(["succeeded", "failed", "timed_out", "cancelled"]),
            )
            .limit(1)
        )
        if other is not None or working is not None:
            return False
        row.state = "running"
        row.execution = {**row.execution, "phase": "claimed"}
        return True
