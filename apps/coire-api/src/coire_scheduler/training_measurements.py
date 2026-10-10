"""Measured experiments: full probe admission, real gateway phases, immutable evidence.

A crash during a phase is inconclusive. It never replays a mixed training spawn or
turns a partial baseline into a 15-minute result. Unknown node owners retain holds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from opentelemetry import metrics, trace
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeMemoryLedgerRow,
    NodeRow,
    RequestLeaseRow,
    TrainingAdapterRow,
    TrainingCommandRow,
    TrainingMeasurementRow,
    TrainingProfileRow,
    TrainingStorageReservationRow,
    VariantCopyRow,
    session_scope,
)
from coire_api.nodes_client import NodeClient
from coire_api.placement.service import effective_occupied_bytes, lock_nodes_for_admission
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.lease_snapshots import SNAPSHOT_VALIDITY_SECONDS
from coire_api.training.measurements import (
    freeze_measurement_inputs,
    mint_measurement_inputs,
    validate_measurement_prompts,
)
from coire_api.training.service import payload_digest, recheck_training_inputs, training_id
from coire_api.training.specs import training_config_digest
from coire_core.errors import TrainingConflict
from coire_core.models.datasets import DatasetFormat
from coire_core.models.engine import EngineState
from coire_core.models.instance import InstanceState
from coire_core.models.node import Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.preference import PreferenceResourceEnvelope
from coire_core.models.training import (
    PreferenceMemoryEvidence,
    ResolvedDatasetInput,
    ResolvedTrainingSpec,
    ResolvedTrainingSpecDocument,
    ResolvedTrainingSpecV3,
    TargetLatencyMeasurement,
    TrainingMeasurementCompletion,
    TrainingMeasurementPhase,
    TrainingMeasurementPrompt,
    TrainingMeasurementPromptSet,
    TrainingMeasurementRequest,
    TrainingMeasurementResult,
    TrainingMemoryEvidence,
    TrainingMemoryEvidenceDocument,
    TrainingNodeMemoryEvidence,
    TrainingProfile,
    TrainingResidentTarget,
    TrainingResourceEnvelope,
    TrainingSpec,
    TrainingSpecV2,
    TrainingSpecV3,
)
from coire_core.models.training_node import (
    PreferenceMeasurementBinding,
    PreferenceMeasurementObservation,
    TrainingCollectiveBinding,
    TrainingInputsRequest,
    TrainingLeaseRenewal,
    TrainingMeasurementBinding,
    TrainingMeasurementDispatch,
    TrainingMeasurementNodeStatus,
    TrainingMeasurementObservation,
    TrainingMeasurementPrepare,
    TrainingPrepared,
    TrainingPrepareRequest,
    TrainingStartReceipt,
    TrainingStartRequest,
    TrainingStopReceipt,
    TrainingStopRequest,
    parse_training_measurement_binding,
)
from coire_core.settings import Settings, get_settings
from coire_scheduler.training_guard import (
    hardware_digest,
    latency_reason,
    measurement_report_digest,
    memory_evidence_digest,
    profile_identity,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.scheduler.training.measurement")
workload_failures = metrics.get_meter("coire.scheduler.training").create_counter(
    "coire_training_workload_failures_total", unit="{request}"
)
outcomes = metrics.get_meter("coire.scheduler.training").create_counter(
    "coire_training_measurement_outcomes_total"
)
COUNTED = (
    MemoryReservationState.PENDING,
    MemoryReservationState.HELD,
    MemoryReservationState.RELEASING,
)
PHASE_SECONDS = 900


async def isolated_memory_envelope(
    session: AsyncSession,
    request: TrainingMeasurementRequest,
    binding: TrainingMeasurementBinding,
    nodes: list[NodeRow],
) -> TrainingResourceEnvelope | PreferenceResourceEnvelope:
    profiles = list(
        await session.scalars(
            select(TrainingProfileRow)
            .where(
                TrainingProfileRow.invalidated_reason.is_(None),
                TrainingProfileRow.valid_until > datetime.now(UTC),
            )
            .order_by(TrainingProfileRow.created_at.desc())
            .limit(100)
        )
    )
    for profile in profiles:
        row = await session.get(TrainingMeasurementRow, profile.measurement_id)
        if row is None or row.state != "succeeded" or row.report is None:
            continue
        report = TrainingMeasurementResult.model_validate(row.report)
        evidence = report.memory_evidence
        if (
            evidence is None
            or report.request.mode != "memory"
            or report.request.resident_targets
            or report.state != "succeeded"
            or report.id != row.id
            or evidence.measurement_id != row.id
            or evidence.completed_updates != request.spec.optim.updates
            or report.completed_updates != evidence.completed_updates
            or report.report_sha256 != row.report_sha256
            or report.report_sha256 != profile.report_sha256
            or report.report_sha256 != measurement_report_digest(report)
            or evidence.resource_envelope.evidence_sha256 != memory_evidence_digest(evidence)
            or evidence.training_config_sha256 != training_config_digest(request.spec)
            or evidence.base_manifest_sha256 != binding.base_manifest_sha256
            or evidence.tokenizer_sha256 != binding.tokenizer_sha256
            or evidence.template_sha256 != binding.template_sha256
            or evidence.runtime_sha256 != binding.runtime_sha256
            or evidence.worker_version != binding.worker_version
            or evidence.valid_until <= datetime.now(UTC)
            or evidence.measured_at > datetime.now(UTC)
            or set(request.nodes) != {n.node for n in evidence.nodes}
            or any(
                n.hardware_sha256
                != next((hardware_digest(node) for node in nodes if node.name == n.node), None)
                or n.swap_growth_bytes
                or not n.thermal_ok
                or n.peak_footprint_bytes > evidence.resource_envelope.memory_bytes
                for n in evidence.nodes
            )
        ):
            continue
        if isinstance(request.spec, TrainingSpecV3):
            if (
                not isinstance(binding, PreferenceMeasurementBinding)
                or not isinstance(evidence, PreferenceMemoryEvidence)
                or (
                    evidence.initial_target != binding.initial_target
                    or evidence.reference_target != binding.reference_target
                    or evidence.objective != binding.objective
                    or evidence.objective_options != binding.objective_options
                    or evidence.datasets
                    != [
                        ResolvedDatasetInput(
                            dataset_id=s.binding.dataset_id,
                            analysis_id=s.analysis.id,
                            source_sha256=s.binding.source_sha256,
                            split_sha256=s.binding.split_sha256,
                            analysis_sha256=payload_digest(s.analysis),
                        )
                        for s in binding.sources
                    ]
                )
            ):
                continue
        elif isinstance(evidence, PreferenceMemoryEvidence):
            continue
        return evidence.resource_envelope
    raise TrainingConflict(
        "Coexistence requires current isolated native memory evidence on these nodes"
    )


async def resident_members(
    session: AsyncSession, target: TrainingResidentTarget
) -> list[InstanceMemberRow]:
    instance = await session.get(ModelInstanceRow, target.instance_id, populate_existing=True)
    members = list(
        await session.scalars(
            select(InstanceMemberRow).where(InstanceMemberRow.instance_id == target.instance_id)
        )
    )
    if (
        instance is None
        or instance.state is not InstanceState.READY
        or (instance.model_id, instance.variant_id, instance.adapter_id)
        != (target.target.model_id, target.target.variant_id, target.target.adapter_id)
        or len(members) != 1
    ):
        raise TrainingConflict("Exact single-node resident target changed")
    # rank_healthy is maintained by the sharded lifecycle only. Single-node
    # placement and reconciliation publish health on the owned engine row.
    member = members[0]
    engine = (
        await session.get(EngineProcessRow, member.engine_id, populate_existing=True)
        if member.engine_id is not None
        else None
    )
    if (
        engine is None
        or engine.state is not EngineState.READY
        or engine.instance_id != instance.id
        or engine.node_id != member.node_id
        or engine.port != member.port
        or (engine.model_id, engine.variant_id, engine.adapter_id)
        != (target.target.model_id, target.target.variant_id, target.target.adapter_id)
    ):
        raise TrainingConflict("Exact single-node resident engine changed")
    copy = await session.scalar(
        select(VariantCopyRow).where(
            VariantCopyRow.variant_id == target.target.variant_id,
            VariantCopyRow.node_id == members[0].node_id,
            VariantCopyRow.verified.is_(True),
        )
    )
    if copy is None or copy.manifest_sha256 != target.target.base_manifest_sha256:
        raise TrainingConflict("Resident base artifact changed")
    if target.target.adapter_id is not None:
        adapter = await session.get(
            TrainingAdapterRow, target.target.adapter_id, populate_existing=True
        )
        if (
            adapter is None
            or adapter.state != "ready"
            or adapter.base_manifest_sha256 != target.target.base_manifest_sha256
            or adapter.manifest_sha256 != target.target.adapter_manifest_sha256
            or adapter.base_variant_id != target.target.variant_id
            or adapter.model_id != target.target.model_id
        ):
            raise TrainingConflict("Resident adapter artifact changed")
    return members


class MeasurementTransport(Protocol):
    async def prepare(self, probe: TrainingMeasurementPrepare) -> TrainingPrepared: ...
    async def inputs(self, request: TrainingInputsRequest) -> TrainingPrepared: ...
    async def start(self, request: TrainingStartRequest) -> TrainingStartReceipt: ...
    async def status(self, probe: TrainingMeasurementPrepare) -> TrainingMeasurementNodeStatus: ...
    async def renew(self, request: TrainingLeaseRenewal) -> TrainingMeasurementNodeStatus: ...
    async def begin(self, probe: TrainingMeasurementPrepare) -> None: ...
    async def stop(self, request: TrainingStopRequest) -> TrainingStopReceipt: ...


class MeasurementNodeClient(NodeClient):
    async def prepare(self, probe: TrainingMeasurementPrepare) -> TrainingPrepared:
        _, body = await self._call(
            "POST",
            probe.prepare.node,
            f"/node/training/measurements/{probe.prepare.attempt_id}/prepare",
            json=probe.model_dump(mode="json"),
            # Preparing the accounted namespace can exceed the five-second
            # control lane while reconciling disk reservations. Stop remains
            # independently bounded by stop_probe; preparation never spawns.
            request_timeout_s=30.0,
        )
        return TrainingPrepared.model_validate(body)

    async def inputs(self, request: TrainingInputsRequest) -> TrainingPrepared:
        _, body = await self._call(
            "POST",
            request.node,
            f"/node/training/measurements/{request.attempt_id}/inputs",
            json=request.model_dump(mode="json"),
            request_timeout_s=30.0
            if any(source.binding.format is DatasetFormat.PREFERENCE for source in request.sources)
            else None,
        )
        return TrainingPrepared.model_validate(body)

    async def start(self, request: TrainingStartRequest) -> TrainingStartReceipt:
        _, body = await self._call(
            "POST",
            request.node,
            f"/node/training/measurements/{request.attempt_id}/start",
            json=request.model_dump(mode="json"),
        )
        return TrainingStartReceipt.model_validate(body)

    async def status(self, probe: TrainingMeasurementPrepare) -> TrainingMeasurementNodeStatus:
        _, body = await self._call(
            "GET", probe.prepare.node, f"/node/training/measurements/{probe.prepare.attempt_id}"
        )
        return TrainingMeasurementNodeStatus.model_validate(body)

    async def renew(self, request: TrainingLeaseRenewal) -> TrainingMeasurementNodeStatus:
        _, body = await self._call(
            "POST",
            request.node,
            f"/node/training/measurements/{request.attempt_id}/lease",
            json=request.model_dump(mode="json"),
        )
        return TrainingMeasurementNodeStatus.model_validate(body)

    async def begin(self, probe: TrainingMeasurementPrepare) -> None:
        await self._call(
            "POST",
            probe.prepare.node,
            f"/node/training/measurements/{probe.prepare.attempt_id}/begin",
            expect=(204,),
        )

    async def stop(self, request: TrainingStopRequest) -> TrainingStopReceipt:
        _, body = await self._call(
            "POST",
            request.node,
            f"/node/training/measurements/{request.attempt_id}/stop",
            json=request.model_dump(mode="json"),
        )
        return TrainingStopReceipt.model_validate(body)


class GatewayWorkloadDriver:
    """The injected gateway operation must authenticate, lease, stream and finish.

    No direct engine access. Only completed streams count; content is discarded.
    Actual routed instance and token usage must accompany the measured first token.
    """

    def __init__(
        self,
        generate: Callable[
            [Principal, uuid.UUID, TrainingResidentTarget, TrainingMeasurementPrompt, int],
            Awaitable[TrainingMeasurementCompletion],
        ],
    ) -> None:
        self.generate = generate

    async def phase(
        self,
        principal: Principal,
        request: TrainingMeasurementRequest,
        prompts: TrainingMeasurementPromptSet,
        phase: str,
        *,
        measurement_id: uuid.UUID,
    ) -> TrainingMeasurementPhase:
        validate_measurement_prompts(request, prompts)
        began, start = datetime.now(UTC), time.monotonic()
        samples: dict[uuid.UUID, list[float]] = {
            t.instance_id: [] for t in request.resident_targets
        }
        failures = 0
        breached = asyncio.Event()
        rolling: dict[uuid.UUID, list[tuple[datetime, float]]] = {
            t.instance_id: [] for t in request.resident_targets
        }
        pending: set[asyncio.Task[None]] = set()
        semaphores = {
            t.instance_id: asyncio.Semaphore(request.workload.concurrency_per_target)
            for t in request.resident_targets
        }

        def failed(reason: str, target: TrainingResidentTarget | None = None) -> None:
            nonlocal failures
            failures += 1
            workload_failures.add(1, {"phase": phase, "reason": reason})
            # Closed reason labels deliberately omit exception text and prompt content.
            logger.warning(
                "Gateway measurement request failed",
                extra={
                    "measurement_id": str(measurement_id),
                    "instance_id": str(target.instance_id) if target else None,
                    "phase": phase,
                    "reason": reason,
                },
            )
            with tracer.start_as_current_span("coire.scheduler.training.workload_failure") as span:
                span.set_attribute("measurement_id", str(measurement_id))
                span.set_attribute("phase", phase)
                span.set_attribute("reason", reason)
                if target:
                    span.set_attribute("instance_id", str(target.instance_id))

        async def complete(
            target: TrainingResidentTarget, prompt: TrainingMeasurementPrompt
        ) -> None:
            semaphore = semaphores[target.instance_id]
            if semaphore.locked():
                failed("concurrency_busy", target)
                return
            async with semaphore:
                try:
                    async with asyncio.timeout(min(60, PHASE_SECONDS - (time.monotonic() - start))):
                        result = await self.generate(
                            principal,
                            measurement_id,
                            target,
                            prompt,
                            request.workload.max_output_tokens,
                        )
                    if (
                        result.instance_id != target.instance_id
                        or result.target != target.target
                        or result.input_tokens
                        != prompt.tokens_by_instance.get(target.instance_id, prompt.input_tokens)
                        or result.output_tokens > request.workload.max_output_tokens
                    ):
                        failed("identity_mismatch", target)
                        return
                    samples[target.instance_id].append(result.first_token_seconds)
                    now = datetime.now(UTC)
                    rolling[target.instance_id].append((now, result.first_token_seconds))
                    if (
                        latency_reason(rolling[target.instance_id], observed_at=now, now=now)
                        == "latency_breach"
                    ):
                        breached.set()
                except TimeoutError:
                    failed("completion_timeout", target)
                except Exception:
                    failed("completion_error", target)

        arrival = 0
        try:
            while time.monotonic() - start < PHASE_SECONDS:
                if breached.is_set():
                    raise TrainingConflict(
                        "Confirmed per-target first-token latency breach during measurement"
                    )
                # Bound attempts and report size even for a 1ms declared arrival rate.
                if arrival >= 20000:
                    failed("arrival_limit")
                    await asyncio.sleep(max(0, PHASE_SECONDS - (time.monotonic() - start)))
                    break
                for target in request.resident_targets:
                    task = asyncio.create_task(
                        complete(target, prompts.prompts[arrival % len(prompts.prompts)])
                    )
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                arrival += 1
                wait = max(
                    0,
                    start
                    + arrival * request.workload.arrival_interval_ms / 1000
                    - time.monotonic(),
                )
                try:
                    await asyncio.wait_for(breached.wait(), timeout=wait)
                    raise TrainingConflict("Confirmed latency breach during measurement")
                except TimeoutError:
                    pass
            await asyncio.gather(*pending)
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return TrainingMeasurementPhase.model_validate(
            {
                "phase": phase,
                "workload_sha256": request.workload.sha256,
                "started_at": began,
                "finished_at": datetime.now(UTC),
                "samples": samples,
                "failures": failures,
            }
        )


async def admit_measurement(
    session: AsyncSession,
    row: TrainingMeasurementRow,
    binding: TrainingMeasurementBinding,
    *,
    settings: Settings | None = None,
) -> TrainingMeasurementDispatch | None:
    request = TrainingMeasurementRequest.model_validate(row.request)
    nodes = list(await session.scalars(select(NodeRow).where(NodeRow.name.in_(request.nodes))))
    if len(nodes) != len(request.nodes):
        raise TrainingConflict("Probe participant inventory is incomplete")
    await lock_nodes_for_admission(session, [n.id for n in nodes])
    collective_binding = None
    if len(nodes) == 2:
        from coire_api.sharding import link_projection
        from coire_scheduler.sharding import validate_hostfile

        config = settings or get_settings()
        if not (await link_projection(session, config)).tp_eligible:
            raise TrainingConflict(
                "Two-rank measurement requires current bounded bare JACCL link evidence"
            )

        def read_hostfile() -> bytes:
            path = Path(config.sharding_jaccl_hostfile)
            if path.is_symlink():
                raise TrainingConflict("Declared generated hostfile is linked")
            with path.open("rb") as source:
                payload = source.read(64 * 1024 + 1)
            if len(payload) > 64 * 1024 or json.loads(payload).get("backend") != "jaccl":
                raise TrainingConflict("Declared bounded JACCL hostfile is unavailable")
            return payload

        try:
            payload = await asyncio.to_thread(read_hostfile)
            hostfile_sha = validate_hostfile(payload, {n.name: n.data_host or "" for n in nodes})
        except (OSError, ValueError):
            raise TrainingConflict("Declared generated JACCL hostfile is unavailable") from None
        collective_binding = TrainingCollectiveBinding(
            hostfile_sha256=hostfile_sha,
            coordinator_port=32323,
            runtime_sha256=binding.runtime_sha256,
        )
    measured_envelope = (
        await isolated_memory_envelope(session, request, binding, nodes)
        if request.mode == "coexistence"
        else None
    )
    now = datetime.now(UTC)
    commands = []
    common_job, common_attempt = training_id(), training_id()
    input_disk_bytes = 640 * 1024**2 * len(binding.sources)
    for rank, node in enumerate(sorted(nodes, key=lambda n: n.name)):
        ledger = await session.get(
            NodeMemoryLedgerRow, node.id, populate_existing=True, with_for_update=True
        )
        if (
            ledger is None
            or ledger.health is not Reachability.HEALTHY
            or ledger.health_sampled_at is None
            or not 0 <= (now - ledger.health_sampled_at).total_seconds() <= 60
            or ledger.thermal_state not in {"nominal", "fair"}
            or ledger.measured_resident_bytes is None
        ):
            return None
        holds = list(
            await session.scalars(
                select(MemoryReservationRow)
                .where(
                    MemoryReservationRow.node_id == node.id,
                    MemoryReservationRow.state.in_(COUNTED),
                )
                .with_for_update()
            )
        )
        model_holds = {h.id for h in holds if h.holder_type is ReservationHolder.MODEL}
        if any(
            h.holder_type
            in {ReservationHolder.TRAINING, ReservationHolder.IMAGE, ReservationHolder.CONVERSION}
            for h in holds
        ):
            return None
        if request.mode == "memory" and model_holds:
            return None
        local_targets = []
        local_engines: dict[uuid.UUID, uuid.UUID] = {}
        if request.mode == "coexistence":
            expected_holds = set()
            for target in request.resident_targets:
                members = await resident_members(session, target)
                if members[0].node_id not in {n.id for n in nodes}:
                    raise TrainingConflict("Resident target is outside measurement participants")
                if members[0].node_id == node.id:
                    expected_holds.add(members[0].reservation_id)
                    local_targets.append(target)
                    assert members[0].engine_id is not None
                    local_engines[target.instance_id] = members[0].engine_id
            if model_holds != expected_holds:
                return None
        if (
            await session.scalar(
                select(RequestLeaseRow.id)
                .where(
                    RequestLeaseRow.reservation_id.in_([h.id for h in holds]),
                    RequestLeaseRow.released_at.is_(None),
                    RequestLeaseRow.expires_at > now,
                )
                .limit(1)
            )
            is not None
        ):
            return None
        occupied = effective_occupied_bytes(holds, ledger.measured_resident_bytes)
        if occupied is None:
            return None
        ceiling = ledger.budget_bytes - occupied
        if ceiling <= 512 * 1024**2:
            return None
        if measured_envelope is not None and measured_envelope.memory_bytes > ceiling:
            return None
        disk_holds = list(
            await session.scalars(
                select(TrainingStorageReservationRow).where(
                    TrainingStorageReservationRow.node_id == node.id,
                    TrainingStorageReservationRow.state.in_(["held", "releasing"]),
                )
            )
        )
        if sum(h.bytes for h in disk_holds) + 20 * 1024**3 + input_disk_bytes > 200 * 1024**3:
            return None
        # This is the entire isolated available slot, not a inferred model estimate.
        cap: TrainingResourceEnvelope | PreferenceResourceEnvelope
        cap = TrainingResourceEnvelope(
            weight_bytes=1,
            adapter_bytes=1,
            optimizer_bytes=1,
            activation_bytes=1,
            buffer_bytes=ceiling - 4 - 256 * 1024**2,
            safety_bytes=256 * 1024**2,
            checkpoint_bytes=(20 * 1024**3) // (request.spec.output.keep_last_checkpoints + 2),
            evidence_sha256="0" * 64,
        )
        native_spec: TrainingSpec | TrainingSpecV3
        if isinstance(request.spec, TrainingSpec):
            native_spec = request.spec
        elif isinstance(request.spec, TrainingSpecV2):
            legacy = request.spec.model_dump(mode="json")
            legacy["schema_version"] = 1
            legacy["eval"].pop("suites")
            native_spec = TrainingSpec.model_validate(legacy)
        else:
            native_spec = request.spec
        resolved_fields: dict[str, Any] = {
            "spec": native_spec,
            "base_manifest_sha256": binding.base_manifest_sha256,
            "datasets": [
                ResolvedDatasetInput(
                    dataset_id=s.binding.dataset_id,
                    analysis_id=s.analysis.id,
                    source_sha256=s.binding.source_sha256,
                    split_sha256=s.binding.split_sha256,
                    analysis_sha256=payload_digest(s.analysis),
                )
                for s in binding.sources
            ],
            "tokenizer_sha256": binding.tokenizer_sha256,
            "template_sha256": binding.template_sha256,
            "enable_thinking": False,
            "runtime_sha256": binding.runtime_sha256,
            "worker_version": binding.worker_version,
            "resource_envelope": cap,
        }
        resolved: ResolvedTrainingSpecDocument
        if isinstance(request.spec, TrainingSpecV3):
            if not isinstance(binding, PreferenceMeasurementBinding):
                raise TrainingConflict("Preference probe has no frozen initial policy binding")
            reference_weight = int(request.spec.objective == "dpo")
            reference_adapter = int(
                request.spec.objective == "dpo" and request.spec.init_adapter is not None
            )
            cap = PreferenceResourceEnvelope.model_validate(
                {
                    **cap.model_dump(),
                    "reference_weight_bytes": reference_weight,
                    "reference_adapter_bytes": reference_adapter,
                    "buffer_bytes": cap.buffer_bytes - reference_weight - reference_adapter,
                }
            )
            isolated_spec = request.spec.model_copy(deep=True)
            isolated_spec.eval.suites = []
            resolved = ResolvedTrainingSpecV3.model_validate(
                {
                    **resolved_fields,
                    "spec": isolated_spec,
                    "resource_envelope": cap,
                    "initial_target": binding.initial_target,
                    "reference_target": binding.reference_target,
                }
            )
        else:
            resolved = ResolvedTrainingSpec.model_validate(resolved_fields)
        await recheck_training_inputs(session, resolved)
        reservation, disk = uuid.uuid4(), uuid.uuid4()
        probe = TrainingMeasurementPrepare(
            measurement_id=row.id,
            hardware_sha256=hardware_digest(node),
            mode=request.mode,
            resident_targets=local_targets,
            resident_engine_ids=local_engines,
            deadline=now + timedelta(hours=1),
            prepare=TrainingPrepareRequest(
                command_id=uuid.uuid4(),
                job_id=common_job,
                attempt_id=common_attempt,
                fence=1,
                request_sha256=payload_digest(resolved),
                node=node.name,
                rank=rank,
                world_size=1 if len(nodes) == 1 else 2,
                lease_expires_at=now + timedelta(seconds=30),
                resolved=resolved,
                reservation_id=reservation,
                disk_reservation_id=disk,
                collective=collective_binding,
            ),
        )
        commands.append(probe)
    # Replicated weights/optimizer are full per rank. Use the smaller safe slot as
    # one immutable full envelope, never divide the model estimate by world size.
    common_cap = min(
        (c.prepare.resolved.resource_envelope for c in commands), key=lambda c: c.memory_bytes
    )
    common_resolved = commands[0].prepare.resolved.model_copy(
        update={"resource_envelope": common_cap}
    )
    commands = [
        c.model_copy(
            update={
                "prepare": c.prepare.model_copy(
                    update={
                        "resolved": common_resolved,
                        "request_sha256": payload_digest(common_resolved),
                    }
                )
            }
        )
        for c in commands
    ]
    # All checks precede inserting any hold: both participants, or neither.
    for node, probe in zip(sorted(nodes, key=lambda n: n.name), commands, strict=True):
        session.add(
            MemoryReservationRow(
                id=probe.prepare.reservation_id,
                node_id=node.id,
                holder_type=ReservationHolder.TRAINING,
                holder_id=f"measurement:{row.id}",
                bytes=probe.prepare.resolved.resource_envelope.memory_bytes,
                pinned=True,
                state=MemoryReservationState.HELD,
            )
        )
        session.add(
            TrainingStorageReservationRow(
                id=probe.prepare.disk_reservation_id,
                owner_user_id=row.owner_user_id,
                node_id=node.id,
                subject_id=f"measurement:{row.id}",
                bytes=20 * 1024**3 + input_disk_bytes,
                state="held",
            )
        )
    row.state = "running"
    return TrainingMeasurementDispatch(
        commands=commands, sources=binding.sources, spawn_nonce=uuid.uuid4()
    )


def build_report(
    row: TrainingMeasurementRow,
    dispatch: TrainingMeasurementDispatch,
    observations: list[TrainingMeasurementObservation],
    phases: list[TrainingMeasurementPhase],
    settings: Settings,
) -> TrainingMeasurementResult:
    request = TrainingMeasurementRequest.model_validate(row.request)
    if len(observations) != len(dispatch.commands):
        raise TrainingConflict("Evidence must contain every physical participant")
    by_node = {o.node: o for o in observations}
    if len(by_node) != len(observations) or set(by_node) != set(request.nodes):
        raise TrainingConflict("Duplicate or missing node evidence")
    for probe in dispatch.commands:
        o = by_node[probe.prepare.node]
        if (
            o.measurement_id != row.id
            or training_config_digest(probe.prepare.resolved.spec)
            != training_config_digest(request.spec)
            or o.attempt_id != probe.prepare.attempt_id
            or o.rank != probe.prepare.rank
            or o.world_size != probe.prepare.world_size
            or o.request_sha256 != payload_digest(probe)
            or o.hardware_sha256 != probe.hardware_sha256
            or o.swap_growth_bytes
            or not o.thermal_ok
            or o.completed_updates != request.spec.optim.updates
            or o.peak_footprint_bytes > probe.prepare.resolved.resource_envelope.memory_bytes
        ):
            raise TrainingConflict("Observed execution differs from frozen safe probe")
    if isinstance(request.spec, TrainingSpecV3):
        for probe in dispatch.commands:
            observed = by_node[probe.prepare.node]
            resolved = probe.prepare.resolved
            if (
                not isinstance(observed, PreferenceMeasurementObservation)
                or not isinstance(resolved, ResolvedTrainingSpecV3)
                or (
                    observed.objective != resolved.spec.objective
                    or observed.initial_target != resolved.initial_target
                    or observed.reference_target != resolved.reference_target
                    or observed.probe_count != observed.completed_updates
                )
            ):
                raise TrainingConflict(
                    "Preference evidence differs from the exact measured objective"
                )
    elif any(isinstance(o, PreferenceMeasurementObservation) for o in observations):
        raise TrainingConflict("SFT measurement cannot reuse preference observations")
    if len(dispatch.commands) == 2:
        from coire_core.models.training_node import TrainingMeasurementRankSet

        pairs = [TrainingMeasurementRankSet(ranks=o.rank_checkpoints) for o in observations]
        if len({p.canonical_sha256() for p in pairs}) != 1:
            raise TrainingConflict(
                "Ranks did not report the same measured common serialization boundary"
            )
        for pair in pairs:
            for rank in pair.ranks:
                prepared = next(p.prepare for p in dispatch.commands if p.prepare.rank == rank.rank)
                if (
                    rank.job_id != prepared.job_id
                    or rank.attempt_id != prepared.attempt_id
                    or rank.fence != prepared.fence
                    or rank.runtime_sha256 != prepared.resolved.runtime_sha256
                    or rank.resolved_spec_sha256 != payload_digest(prepared.resolved)
                    or rank.serialized_bytes >= prepared.resolved.resource_envelope.checkpoint_bytes
                    or pair.serialized_bytes != by_node[prepared.node].checkpoint_bytes
                ):
                    raise TrainingConflict(
                        "Rank serialization metadata differs from full pinned probe bounds"
                    )

    def max_component(field: str) -> int:
        return max(int(getattr(o, field)) for o in observations)

    weights, adapter, optimizer, buffers = (
        max_component(f)
        for f in ("weight_bytes", "adapter_bytes", "optimizer_bytes", "buffer_bytes")
    )
    physical = max(
        max_component("peak_footprint_bytes"),
        max_component("peak_mlx_bytes"),
        max_component("serialization_peak_bytes"),
    )
    safety = max(256 * 1024**2, math.ceil(physical * 0.1))
    cap: TrainingResourceEnvelope | PreferenceResourceEnvelope
    cap = TrainingResourceEnvelope(
        weight_bytes=weights,
        adapter_bytes=adapter,
        optimizer_bytes=optimizer,
        activation_bytes=max(1, physical - weights - adapter - optimizer - buffers),
        buffer_bytes=buffers,
        safety_bytes=safety,
        checkpoint_bytes=max_component("checkpoint_bytes"),
        evidence_sha256="0" * 64,
    )
    if isinstance(request.spec, TrainingSpecV3):
        reference_weights = max_component("reference_weight_bytes")
        reference_adapters = max_component("reference_adapter_bytes")
        cap = PreferenceResourceEnvelope.model_validate(
            {
                **cap.model_dump(),
                "reference_weight_bytes": reference_weights,
                "reference_adapter_bytes": reference_adapters,
                "activation_bytes": max(
                    1,
                    physical
                    - weights
                    - adapter
                    - optimizer
                    - buffers
                    - reference_weights
                    - reference_adapters,
                ),
            }
        )
    if any(
        cap.memory_bytes > p.prepare.resolved.resource_envelope.memory_bytes
        for p in dispatch.commands
    ):
        raise TrainingConflict("Measured envelope plus safety does not fit probe hold")
    now = max(o.measured_at for o in observations)
    resolved = dispatch.commands[0].prepare.resolved
    evidence_fields: dict[str, Any] = {
        "measurement_id": row.id,
        "training_config_sha256": training_config_digest(request.spec),
        "base_manifest_sha256": resolved.base_manifest_sha256,
        "tokenizer_sha256": resolved.tokenizer_sha256,
        "template_sha256": resolved.template_sha256,
        "runtime_sha256": resolved.runtime_sha256,
        "worker_version": resolved.worker_version,
        "completed_updates": min(o.completed_updates for o in observations),
        "resource_envelope": cap,
        "nodes": [
            TrainingNodeMemoryEvidence(
                node=o.node,
                hardware_sha256=o.hardware_sha256,
                peak_footprint_bytes=o.peak_footprint_bytes,
                swap_growth_bytes=o.swap_growth_bytes,
                thermal_ok=o.thermal_ok,
            )
            for o in sorted(observations, key=lambda o: o.node)
        ],
        "measured_at": now,
        "valid_until": now + timedelta(seconds=settings.training_profile_ttl_s),
    }
    evidence: TrainingMemoryEvidenceDocument
    if isinstance(resolved, ResolvedTrainingSpecV3):
        evidence = PreferenceMemoryEvidence.model_validate(
            {
                **evidence_fields,
                "objective": resolved.spec.objective,
                "objective_options": resolved.spec.objective_options,
                "initial_target": resolved.initial_target,
                "reference_target": resolved.reference_target,
                "datasets": resolved.datasets,
            }
        )
    else:
        evidence = TrainingMemoryEvidence.model_validate(evidence_fields)
    evidence.resource_envelope.evidence_sha256 = memory_evidence_digest(evidence)
    targets = []
    if request.mode == "coexistence":
        if [p.phase for p in phases] != ["baseline", "mixed"]:
            raise TrainingConflict("Both real phases are required")
        expected = {t.instance_id for t in request.resident_targets}
        if any(
            (p.finished_at - p.started_at).total_seconds() < PHASE_SECONDS
            or p.workload_sha256 != request.workload.sha256
            or p.failures
            or set(p.samples) != expected
            or any(len(v) < 100 for v in p.samples.values())
            for p in phases
        ):
            raise TrainingConflict("Incomplete, failed or undersampled coexistence phase")
        mixed = phases[1]
        if phases[0].finished_at > mixed.started_at:
            raise TrainingConflict("Baseline and mixed phases must be separate")
        # Clock skew or a trainer finishing before the mixed phase prevents approval.
        if any(
            o.training_started_at > mixed.started_at or o.training_finished_at < mixed.finished_at
            for o in observations
        ):
            raise TrainingConflict("Training did not cover the entire mixed workload")
        for target in request.resident_targets:
            values = [sorted(p.samples[target.instance_id]) for p in phases]
            targets.append(
                TargetLatencyMeasurement(
                    instance_id=target.instance_id,
                    baseline_requests=len(values[0]),
                    mixed_requests=len(values[1]),
                    baseline_p95_seconds=values[0][math.ceil(0.95 * len(values[0])) - 1],
                    mixed_p95_seconds=values[1][math.ceil(0.95 * len(values[1])) - 1],
                )
            )
    report = TrainingMeasurementResult(
        id=row.id,
        request=request,
        state="succeeded",
        targets=targets,
        report_sha256="0" * 64,
        completed_updates=evidence.completed_updates,
        peak_memory_bytes=physical,
        thermal_ok=True,
        created_at=row.created_at,
        memory_evidence=evidence,
        phases=phases,
        node_report_sha256=[payload_digest(o) for o in sorted(observations, key=lambda o: o.node)],
    )
    report.report_sha256 = measurement_report_digest(report)
    return report


def control_fields(probe: TrainingMeasurementPrepare) -> dict[str, object]:
    return {
        k: getattr(probe.prepare, k)
        for k in ("job_id", "attempt_id", "fence", "request_sha256", "node", "rank", "world_size")
    } | {"command_id": uuid.uuid4(), "lease_expires_at": datetime.now(UTC) + timedelta(seconds=30)}


class TrainingMeasurementExecutor:
    def __init__(
        self,
        settings: Settings,
        transport: MeasurementTransport,
        workload: GatewayWorkloadDriver | None = None,
    ) -> None:
        self.settings, self.transport, self.workload = settings, transport, workload
        self.task: asyncio.Task[None] | None = None
        self.active: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def start(self) -> None:
        if self.settings.training_enabled:
            self.task = asyncio.create_task(self.run(), name="training-measurements")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
        for task in self.active.values():
            task.cancel()
        await asyncio.gather(
            *(list(self.active.values()) + ([self.task] if self.task else [])),
            return_exceptions=True,
        )

    async def run(self) -> None:
        while True:
            try:
                await self.pass_once()
            except Exception as error:
                logger.error("measurement scan failed", extra={"error_type": type(error).__name__})
            await asyncio.sleep(2)

    async def pass_once(self) -> None:
        for identity, task in list(self.active.items()):
            if task.done():
                if not task.cancelled() and task.exception():
                    logger.error(
                        "measurement execution failed",
                        extra={
                            "measurement_id": str(identity),
                            "error_type": type(task.exception()).__name__,
                        },
                    )
                self.active.pop(identity)
        async with session_scope() as session:
            identities = list(
                await session.scalars(
                    select(TrainingMeasurementRow.id)
                    .where(TrainingMeasurementRow.state.in_(["queued", "running"]))
                    .order_by(TrainingMeasurementRow.created_at)
                    .limit(8)
                )
            )
        for identity in identities:
            if identity not in self.active:
                self.active[identity] = asyncio.create_task(self.advance(identity))

    async def advance(self, identity: uuid.UUID) -> None:
        # The ownership transaction contains no domain row locks across node I/O.
        async with session_scope() as ownership:
            owned = await ownership.scalar(
                select(
                    func.pg_try_advisory_xact_lock(
                        func.hashtextextended(f"coire.training.measurement:{identity}", 0)
                    )
                )
            )
            if not owned:
                return
            await self._advance(identity)

    async def _advance(self, identity: uuid.UUID) -> None:
        with tracer.start_as_current_span("coire.scheduler.training.measurement.execute"):
            async with session_scope() as session:
                row = await session.get(TrainingMeasurementRow, identity, with_for_update=True)
                command = await session.scalar(
                    select(TrainingCommandRow)
                    .where(
                        TrainingCommandRow.operation == "training.measurement",
                        TrainingCommandRow.subject_id == str(identity),
                    )
                    .with_for_update()
                )
                if row is None or command is None or row.state not in {"queued", "running"}:
                    return
                principal = Principal.model_validate(command.payload["principal"])
                request = TrainingMeasurementRequest.model_validate(row.request)
                if (
                    command.request_sha256 != payload_digest(request)
                    or command.actor_user_id != row.owner_user_id
                ):
                    raise TrainingConflict(
                        "Queued measurement intent changed after audited submission"
                    )
                binding = parse_training_measurement_binding(command.payload["binding"])
                prompts = (
                    TrainingMeasurementPromptSet.model_validate(command.payload["prompts"])
                    if command.payload.get("prompts")
                    else None
                )
                dispatch = (
                    TrainingMeasurementDispatch.model_validate(command.payload["dispatch"])
                    if command.payload.get("dispatch")
                    else None
                )
                resumed = dispatch is not None
                if dispatch is None:
                    await authorize_live_training_action(session, principal)
                    if await freeze_measurement_inputs(session, request) != binding:
                        raise TrainingConflict("Frozen measurement inputs changed before admission")
                    if datetime.now(UTC) - row.created_at >= timedelta(hours=24):
                        row.state = "inconclusive"
                        command.state = "failed"
                        return
                    dispatch = await admit_measurement(
                        session, row, binding, settings=self.settings
                    )
                    if dispatch is None:
                        return
                    command.payload = {
                        **command.payload,
                        "dispatch": dispatch.model_dump(mode="json"),
                    }
                    command.state = "dispatching"
            # A preexisting dispatch after restart is stopped, never blindly replayed.
            assert dispatch is not None
            report: TrainingMeasurementResult | None = None
            phases: list[TrainingMeasurementPhase] = []
            renewal: asyncio.Task[None] | None = None
            try:
                if resumed:
                    raise TrainingConflict("Interrupted measurement phases are inconclusive")
                async with asyncio.timeout(3600):
                    renewal = await self.prepare_inputs(identity, principal, binding, dispatch)
                    if request.mode == "coexistence":
                        if self.workload is None or prompts is None:
                            raise TrainingConflict(
                                "Authenticated exact-instance gateway workload is unavailable"
                            )
                        baseline_task = asyncio.create_task(
                            self.workload.phase(
                                principal, request, prompts, "baseline", measurement_id=identity
                            )
                        )
                        try:
                            done, _ = await asyncio.wait(
                                [baseline_task, renewal], return_when=asyncio.FIRST_COMPLETED
                            )
                            if renewal in done:
                                await renewal
                                raise TrainingConflict("Baseline measurement watchdog ended")
                            baseline = await baseline_task
                        finally:
                            if not baseline_task.done():
                                baseline_task.cancel()
                                await asyncio.gather(baseline_task, return_exceptions=True)
                        phases.append(baseline)
                        await self.save_phase(identity, baseline)
                        if baseline.failures or any(
                            len(v) < 100 or sorted(v)[math.ceil(0.95 * len(v)) - 1] > 1.5
                            for v in baseline.samples.values()
                        ):
                            raise TrainingConflict(
                                "Baseline is incomplete or already breaches latency"
                            )
                        await self.settle_baseline_leases(renewal)
                    await self.recheck(identity, principal, binding)
                    # Attach both owned native ranks concurrently, after both inputs are ready.
                    starts: list[Awaitable[TrainingStartReceipt]] = []
                    for probe in dispatch.commands:
                        start = TrainingStartRequest.model_validate(
                            {
                                **control_fields(probe),
                                "prepared_command_id": probe.prepare.command_id,
                                "spawn_nonce": dispatch.spawn_nonce,
                            }
                        )
                        starts.append(self.transport.start(start))
                    await asyncio.gather(*starts)
                    if request.mode == "coexistence" or len(dispatch.commands) == 2:
                        while not all(s.ready for s in await self.statuses(dispatch)):
                            if renewal.done():
                                await renewal
                            await asyncio.sleep(0.5)
                        for probe in dispatch.commands:
                            await self.transport.begin(probe)
                        while not all(
                            s.training_started_at is not None for s in await self.statuses(dispatch)
                        ):
                            if renewal.done():
                                await renewal
                            await asyncio.sleep(0.1)
                    if request.mode == "coexistence":
                        assert self.workload is not None and prompts is not None
                        mixed_task = asyncio.create_task(
                            self.workload.phase(
                                principal, request, prompts, "mixed", measurement_id=identity
                            )
                        )
                        try:
                            done, _ = await asyncio.wait(
                                [mixed_task, renewal], return_when=asyncio.FIRST_COMPLETED
                            )
                            if renewal in done:
                                await renewal
                                raise TrainingConflict("Measurement watchdog ended")
                            mixed = await mixed_task
                        finally:
                            if not mixed_task.done():
                                mixed_task.cancel()
                                await asyncio.gather(mixed_task, return_exceptions=True)
                        phases.append(mixed)
                        await self.save_phase(identity, mixed)
                    while True:
                        statuses = await self.statuses(dispatch)
                        if all(s.stopped for s in statuses):
                            break
                        if renewal.done():
                            await renewal
                        await asyncio.sleep(0.5)
                    observations = [s.observation for s in statuses if s.observation is not None]
                    report = build_report(row, dispatch, observations, phases, self.settings)
            except (Exception, asyncio.CancelledError) as error:
                logger.warning(
                    "measurement inconclusive",
                    extra={"measurement_id": str(identity), "error_type": type(error).__name__},
                )
            finally:
                if renewal is not None:
                    renewal.cancel()
                    await asyncio.gather(renewal, return_exceptions=True)
                # Independent five-second node stop lane; uncertain stops keep core holds.
                proofs = await asyncio.gather(*(self.stop_probe(p) for p in dispatch.commands))
                async with session_scope() as session:
                    await self.finish(
                        session, identity, principal, binding, dispatch, report, proofs
                    )

    async def settle_baseline_leases(self, renewal: asyncio.Task[None]) -> None:
        # The last baseline response can finish after the node's most recent
        # authenticated lease snapshot. Let that snapshot expire while keeping
        # preparation owned and watched. Native start still requires fresh zero
        # leases; failed refreshes and unrelated requests remain refusals.
        with tracer.start_as_current_span("coire.scheduler.training.measurement.lease_handoff"):
            settle = asyncio.create_task(asyncio.sleep(SNAPSHOT_VALIDITY_SECONDS))
            try:
                done, _ = await asyncio.wait([settle, renewal], return_when=asyncio.FIRST_COMPLETED)
                if renewal in done:
                    await renewal
                    raise TrainingConflict("Baseline handoff watchdog ended")
                await settle
            finally:
                if not settle.done():
                    settle.cancel()
                await asyncio.gather(settle, return_exceptions=True)

    async def prepare_inputs(
        self,
        identity: uuid.UUID,
        principal: Principal,
        binding: TrainingMeasurementBinding,
        dispatch: TrainingMeasurementDispatch,
    ) -> asyncio.Task[None]:
        for probe in dispatch.commands:
            await self.transport.prepare(probe)
        renewal = asyncio.create_task(self.watch(identity, principal, binding, dispatch))

        async def deliver_all() -> None:
            for probe in dispatch.commands:
                await self.deliver(principal, probe)

        inputs = asyncio.create_task(deliver_all())
        try:
            done, _ = await asyncio.wait([inputs, renewal], return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
                raise TrainingConflict("Input preparation watchdog ended")
            await inputs
        except BaseException:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            raise
        finally:
            if not inputs.done():
                inputs.cancel()
            await asyncio.gather(inputs, return_exceptions=True)
        return renewal

    async def deliver(self, principal: Principal, probe: TrainingMeasurementPrepare) -> None:
        while True:
            async with session_scope() as session:
                request = await mint_measurement_inputs(
                    session, principal, probe, settings=self.settings
                )
                request.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
            ready = await self.transport.inputs(request)
            if ready.ready:
                return
            if datetime.now(UTC) >= probe.deadline:
                raise TrainingConflict("Measurement source preparation exceeded deadline")
            await asyncio.sleep(0.5)

    async def statuses(
        self, dispatch: TrainingMeasurementDispatch
    ) -> list[TrainingMeasurementNodeStatus]:
        statuses = await asyncio.gather(*(self.transport.status(p) for p in dispatch.commands))
        for probe, status in zip(dispatch.commands, statuses, strict=True):
            if (
                status.measurement_id != probe.measurement_id
                or status.status.attempt_id != probe.prepare.attempt_id
                or status.status.node != probe.prepare.node
                or status.status.fence != probe.prepare.fence
            ):
                raise TrainingConflict("Measurement status belongs to another execution")
        return list(statuses)

    async def recheck(
        self, identity: uuid.UUID, principal: Principal, binding: TrainingMeasurementBinding
    ) -> None:
        async with session_scope() as session:
            await authorize_live_training_action(session, principal, shared=True)
            row = await session.get(TrainingMeasurementRow, identity)
            if row is None or row.state != "running" or not self.settings.training_enabled:
                raise TrainingConflict("Measurement execution authority ended")
            request = TrainingMeasurementRequest.model_validate(row.request)
            if await freeze_measurement_inputs(session, request) != binding:
                raise TrainingConflict("Measurement pinned assets changed")
            dispatch = None
            if request.mode == "coexistence":
                command = await session.scalar(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.subject_id == str(identity),
                        TrainingCommandRow.operation == "training.measurement",
                    )
                )
                if command is None:
                    raise TrainingConflict("Measurement dispatch disappeared")
                dispatch = TrainingMeasurementDispatch.model_validate(
                    command.payload.get("dispatch")
                )
            nodes = list(
                await session.scalars(select(NodeRow).where(NodeRow.name.in_(request.nodes)))
            )
            await lock_nodes_for_admission(session, [n.id for n in nodes])
            if dispatch is not None:
                from coire_api.training.measurement_residents import resolve_measurement_residents

                await resolve_measurement_residents(
                    session,
                    [node.id for node in nodes],
                    request.resident_targets,
                    {
                        instance_id: engine_id
                        for probe in dispatch.commands
                        for instance_id, engine_id in probe.resident_engine_ids.items()
                    },
                    {probe.prepare.reservation_id for probe in dispatch.commands},
                    identity,
                    {node.id: node.name for node in nodes},
                )

    async def watch(
        self,
        identity: uuid.UUID,
        principal: Principal,
        binding: TrainingMeasurementBinding,
        dispatch: TrainingMeasurementDispatch,
    ) -> None:
        initial_swap: dict[str, tuple[int, int]] = {}
        while True:
            await self.recheck(identity, principal, binding)
            statuses = await self.statuses(dispatch)
            for probe, status in zip(dispatch.commands, statuses, strict=True):
                if (
                    status.swap_used_bytes is None
                    or status.swap_out_bytes is None
                    or status.sampled_at is None
                    or status.thermal_state not in {"nominal", "fair"}
                    or not 0 <= (datetime.now(UTC) - status.sampled_at).total_seconds() <= 60
                ):
                    raise TrainingConflict(
                        "Measurement resource telemetry is unsafe or unavailable"
                    )
                initial = initial_swap.setdefault(
                    probe.prepare.node, (status.swap_used_bytes, status.swap_out_bytes)
                )
                if status.swap_used_bytes > initial[0] or status.swap_out_bytes > initial[1]:
                    raise TrainingConflict("Measurement caused swap growth")
                # Prepared reservations remain owned throughout the chat baseline.
                # Renew before expiry; the node rejects stopped or expired work.
                if status.status.liveness in {"prepared", "running"}:
                    await self.transport.renew(
                        TrainingLeaseRenewal.model_validate(control_fields(probe))
                    )
            await asyncio.sleep(10)

    async def save_phase(self, identity: uuid.UUID, phase: TrainingMeasurementPhase) -> None:
        async with session_scope() as session:
            command = await session.scalar(
                select(TrainingCommandRow)
                .where(
                    TrainingCommandRow.operation == "training.measurement",
                    TrainingCommandRow.subject_id == str(identity),
                )
                .with_for_update()
            )
            assert command is not None
            key = "phase_" + phase.phase
            if key in command.payload:
                raise TrainingConflict("Completed measurement phase is immutable")
            command.payload = {**command.payload, key: phase.model_dump(mode="json")}

    async def stop_probe(self, probe: TrainingMeasurementPrepare) -> TrainingStopReceipt | None:
        from coire_api.nodes_client import NodeError, NodeErrorKind

        command = TrainingStopRequest.model_validate(
            {**control_fields(probe), "reason": "cancelled"}
        )
        try:
            async with asyncio.timeout(5):
                try:
                    return await self.transport.stop(command)
                except NodeError as error:
                    if error.kind is not NodeErrorKind.CONFLICT:
                        raise
                # A guard may have refused before native preparation journaling.
                # Rebind only the exact immutable prepare (never start/inputs), so
                # a compatible node can persist its fenced no-start rejection.
                # Neither this refusal nor a missing status is itself death proof.
                try:
                    await self.transport.prepare(probe)
                except NodeError as error:
                    if error.kind is not NodeErrorKind.CONFLICT:
                        raise
                return await self.transport.stop(command)
        except Exception:
            return None

    async def finish(
        self,
        session: AsyncSession,
        identity: uuid.UUID,
        principal: Principal,
        binding: TrainingMeasurementBinding,
        dispatch: TrainingMeasurementDispatch,
        report: TrainingMeasurementResult | None,
        proofs: list[TrainingStopReceipt | None],
    ) -> None:
        from coire_api.audit import write_principal_audit
        from coire_core.models.audit import AuditOutcome

        row = await session.get(TrainingMeasurementRow, identity, with_for_update=True)
        if row is None or row.state != "running":
            return
        command = await session.scalar(
            select(TrainingCommandRow)
            .where(
                TrainingCommandRow.operation == "training.measurement",
                TrainingCommandRow.subject_id == str(identity),
            )
            .with_for_update()
        )
        if (
            command is None
            or command.actor_user_id != row.owner_user_id
            or TrainingMeasurementDispatch.model_validate(command.payload.get("dispatch"))
            != dispatch
        ):
            raise TrainingConflict("Measurement stop proof has no matching persisted dispatch")
        safe = True
        scoped_proofs: list[dict[str, object] | None] = []
        for probe, proof in zip(dispatch.commands, proofs, strict=True):
            if (
                proof is None
                or not proof.stopped
                or proof.attempt_id != probe.prepare.attempt_id
                or proof.node != probe.prepare.node
                or proof.fence != probe.prepare.fence
            ):
                safe = False
                scoped_proofs.append(None)
                continue
            scoped_proofs.append(proof.model_dump(mode="json"))
            hold = await session.get(
                MemoryReservationRow, probe.prepare.reservation_id, with_for_update=True
            )
            disk = await session.get(
                TrainingStorageReservationRow,
                probe.prepare.disk_reservation_id,
                with_for_update=True,
            )
            if hold is not None:
                hold.state = MemoryReservationState.RELEASED
            if disk is not None:
                disk.state = "released"
        command.payload = {**command.payload, "stop_proofs": scoped_proofs}
        if not safe:
            outcomes.add(1, {"state": "unknown", "mode": dispatch.commands[0].mode})
            return  # Retain running uncertainty, retry observation/stop on the next pass.
        if report is not None:
            try:
                await authorize_live_training_action(session, principal)
                if await freeze_measurement_inputs(session, report.request) != binding:
                    raise TrainingConflict("Frozen evidence inputs changed before publication")
                for probe in dispatch.commands:
                    node = await session.scalar(
                        select(NodeRow).where(NodeRow.name == probe.prepare.node)
                    )
                    if node is None or hardware_digest(node) != probe.hardware_sha256:
                        raise TrainingConflict("Measured hardware changed before publication")
            except Exception:
                report = None
        if report is None:
            row.state = "inconclusive"
            command.state = "failed"
            outcomes.add(1, {"state": "inconclusive", "mode": dispatch.commands[0].mode})
            await write_principal_audit(
                session,
                principal=principal,
                action="training.measurement.inconclusive",
                target_type="measurement",
                target_id=str(identity),
                outcome=AuditOutcome.ERROR,
                context={"reason": "incomplete_or_unsafe_evidence"},
            )
            return
        assert report.memory_evidence is not None and report.report_sha256 is not None
        profile_id = uuid.uuid4()
        report.profile_id = profile_id
        profile = TrainingProfile(
            id=profile_id,
            report_sha256=report.report_sha256,
            request=report.request,
            expires_at=report.memory_evidence.valid_until,
        )
        session.add(
            TrainingProfileRow(
                id=profile_id,
                measurement_id=identity,
                report_sha256=report.report_sha256,
                identity_sha256=profile_identity(
                    report.request, report.memory_evidence.runtime_sha256
                ),
                profile=profile.model_dump(mode="json"),
                valid_until=profile.expires_at,
            )
        )
        row.state, row.report, row.report_sha256 = (
            "succeeded",
            report.model_dump(mode="json"),
            report.report_sha256,
        )
        command.state = "succeeded"
        await write_principal_audit(
            session,
            principal=principal,
            action="training.measurement.approve",
            target_type="measurement",
            target_id=str(identity),
            outcome=AuditOutcome.OK,
            context={"report_sha256": report.report_sha256},
        )
        outcomes.add(1, {"state": "succeeded", "mode": report.request.mode})
