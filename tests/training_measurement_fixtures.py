"""Synthetic CPU-only contracts for measurement control/evidence tests."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import NodeRow, TrainingMeasurementRow
from coire_api.training.service import payload_digest
from coire_core.models.datasets import (
    DatasetAnalysis,
    DatasetAnalysisBinding,
    DatasetFormat,
    SplitManifest,
    TokenDistribution,
)
from coire_core.models.training import (
    ResolvedDatasetInput,
    ResolvedTrainingSpec,
    TrainingMeasurementRequest,
    TrainingResidentTarget,
    TrainingResourceEnvelope,
)
from coire_core.models.training_node import (
    TrainingMeasurementBinding,
    TrainingMeasurementDispatch,
    TrainingMeasurementObservation,
    TrainingMeasurementPrepare,
    TrainingMeasurementSource,
    TrainingPrepareRequest,
)
from coire_core.training_data import normalize_row, split_digest
from coire_node.training.worker import FrozenInputs

DIGEST = "a" * 64
ATTEMPT = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def experiment() -> tuple[TrainingMeasurementRow, TrainingMeasurementDispatch]:
    model, variant, dataset = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    request = TrainingMeasurementRequest.model_validate(
        {
            "spec": {
                "model": {"model_id": model, "variant_id": variant},
                "data": {
                    "train": {
                        "datasets": [
                            {"dataset_id": dataset, "mixture_proportion": 1.0, "sample_count": 1}
                        ],
                        "epoch_samples": 1,
                    },
                    "validation": {"dataset_ids": [dataset]},
                    "loss_policy": "all_tokens",
                },
                "parameterization": {"target_modules": ["self_attn.q_proj"]},
                "optim": {"updates": 2, "max_sequence_length": 32},
                "output": {"adapter_slug": "probe"},
            },
            "mode": "memory",
            "nodes": ["coire-edge-a"],
            "workload": {
                "sha256": DIGEST,
                "concurrency_per_target": 1,
                "arrival_interval_ms": 1000,
                "max_output_tokens": 16,
            },
        }
    )
    split = SplitManifest(
        dataset_id=dataset,
        source_sha256=DIGEST,
        seed=0,
        train_rows=[1],
        validation_rows=[2],
        row_content_sha256=["b" * 64, "c" * 64],
    )
    binding = DatasetAnalysisBinding(
        dataset_id=dataset,
        model_id=model,
        variant_id=variant,
        base_manifest_sha256=DIGEST,
        source_sha256=DIGEST,
        split_sha256=payload_digest(split),
        format=DatasetFormat.TEXT,
        model_slug="fixture",
    )
    analysis = DatasetAnalysis.model_validate(
        {
            "id": uuid.uuid4(),
            "dataset_id": dataset,
            "model_id": model,
            "variant_id": variant,
            "tokenizer_sha256": DIGEST,
            "template_sha256": DIGEST,
            "runtime_sha256": DIGEST,
            "state": "succeeded",
            "row_count": 2,
            "created_at": now,
            "tokens": {
                "minimum": 4,
                "maximum": 4,
                "p50": 4,
                "p95": 4,
                "histogram": [2],
                "upper_bounds": [4],
            },
        }
    )
    resolved = ResolvedTrainingSpec(
        spec=request.spec,
        base_manifest_sha256=DIGEST,
        tokenizer_sha256=DIGEST,
        template_sha256=DIGEST,
        runtime_sha256=DIGEST,
        enable_thinking=False,
        worker_version="1",
        datasets=[
            ResolvedDatasetInput(
                dataset_id=dataset,
                analysis_id=analysis.id,
                source_sha256=DIGEST,
                split_sha256=payload_digest(split),
                analysis_sha256=payload_digest(analysis),
            )
        ],
        resource_envelope=TrainingResourceEnvelope(
            weight_bytes=1,
            adapter_bytes=1,
            optimizer_bytes=1,
            activation_bytes=1,
            buffer_bytes=8 * 1024**3 - 4 - 256 * 1024**2,
            safety_bytes=256 * 1024**2,
            checkpoint_bytes=4 * 1024**3,
            evidence_sha256="0" * 64,
        ),
    )
    row = TrainingMeasurementRow(
        id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        request=request.model_dump(mode="json"),
        state="running",
        created_at=now,
    )
    probe = TrainingMeasurementPrepare(
        measurement_id=row.id,
        hardware_sha256=DIGEST,
        deadline=now + timedelta(hours=1),
        mode="memory",
        prepare=TrainingPrepareRequest(
            command_id=uuid.uuid4(),
            job_id=JOB,
            attempt_id=ATTEMPT,
            fence=1,
            request_sha256=payload_digest(resolved),
            node="coire-edge-a",
            rank=0,
            world_size=1,
            lease_expires_at=now + timedelta(seconds=30),
            resolved=resolved,
            reservation_id=uuid.uuid4(),
            disk_reservation_id=uuid.uuid4(),
        ),
    )
    return row, TrainingMeasurementDispatch(
        commands=[probe],
        sources=[TrainingMeasurementSource(binding=binding, split=split, analysis=analysis)],
        spawn_nonce=uuid.uuid4(),
    )


def frozen_binding(dispatch: TrainingMeasurementDispatch) -> TrainingMeasurementBinding:
    r = dispatch.commands[0].prepare.resolved
    return TrainingMeasurementBinding(
        base_manifest_sha256=r.base_manifest_sha256,
        tokenizer_sha256=r.tokenizer_sha256,
        template_sha256=r.template_sha256,
        runtime_sha256=r.runtime_sha256,
        worker_version=r.worker_version,
        sources=dispatch.sources,
    )


def observation(probe: TrainingMeasurementPrepare) -> TrainingMeasurementObservation:
    now = datetime.now(UTC)
    return TrainingMeasurementObservation(
        measurement_id=probe.measurement_id,
        attempt_id=probe.prepare.attempt_id,
        node=probe.prepare.node,
        hardware_sha256=probe.hardware_sha256,
        request_sha256=payload_digest(probe),
        completed_updates=2,
        elapsed_seconds=1200.0,
        training_started_at=now - timedelta(minutes=20),
        training_finished_at=now,
        peak_footprint_bytes=600 * 1024**2,
        peak_mlx_bytes=512 * 1024**2,
        weight_bytes=100 * 1024**2,
        adapter_bytes=1024**2,
        optimizer_bytes=3 * 1024**2,
        buffer_bytes=1024**2,
        checkpoint_bytes=4 * 1024**2,
        serialization_peak_bytes=600 * 1024**2,
        swap_growth_bytes=0,
        thermal_ok=True,
        sample_count=2400,
        measured_at=now,
    )


def mixture_experiment(
    root: Path, *, world_size: int = 1
) -> tuple[TrainingMeasurementRow, TrainingMeasurementDispatch, list[FrozenInputs]]:
    """Two training sources plus a separate held-out revision; synthetic CPU text."""
    row, original = experiment()
    probe = original.commands[0]
    dataset_ids = [uuid.uuid4() for _ in range(3)]
    request = TrainingMeasurementRequest.model_validate(row.request)
    spec = request.spec.model_dump(mode="json")
    spec["data"]["train"] = {
        "datasets": [
            {"dataset_id": str(key), "sample_count": 4, "mixture_proportion": 0.5}
            for key in dataset_ids[:2]
        ],
        "epoch_samples": 6,
    }
    spec["data"]["validation"] = {"dataset_ids": [str(dataset_ids[2])]}
    spec["optim"]["batch_size"] = 2
    spec["placement"] = {"mode": "data_parallel" if world_size == 2 else "single"}
    request = TrainingMeasurementRequest.model_validate(
        {
            **request.model_dump(mode="json"),
            "spec": spec,
            "nodes": ["coire-edge-a", "coire-edge-b"] if world_size == 2 else ["coire-edge-a"],
        }
    )
    sources, inputs, resolved_inputs = [], [], []
    for index, key in enumerate(dataset_ids):
        rows = [{"text": str((index + 1) * 100 + n)} for n in range(1, 7)]
        encoded = b"".join(json.dumps(r).encode() + b"\n" for r in rows)
        path = root / f"{key}.jsonl"
        path.write_bytes(encoded)
        path.chmod(0o600)
        source_sha = hashlib.sha256(encoded).hexdigest()
        split = SplitManifest(
            dataset_id=key,
            source_sha256=source_sha,
            seed=0,
            train_rows=[1, 2, 3, 4],
            validation_rows=[5, 6],
            row_content_sha256=[
                normalize_row(
                    r, format=DatasetFormat.TEXT, dataset_id=key, source_row=n
                ).content_sha256()
                for n, r in enumerate(rows, 1)
            ],
        )
        binding = DatasetAnalysisBinding(
            dataset_id=key,
            model_id=request.spec.model.model_id,
            variant_id=request.spec.model.variant_id,
            base_manifest_sha256=DIGEST,
            source_sha256=source_sha,
            split_sha256=split_digest(split),
            format=DatasetFormat.TEXT,
            model_slug="fixture",
        )
        analysis = original.sources[0].analysis.model_copy(
            deep=True,
            update={
                "id": uuid.uuid4(),
                "dataset_id": key,
                "row_count": 6,
                "tokens": TokenDistribution(
                    minimum=2, maximum=2, p50=2, p95=2, histogram=[6], upper_bounds=[2]
                ),
            },
        )
        sources.append(TrainingMeasurementSource(binding=binding, split=split, analysis=analysis))
        inputs.append(FrozenInputs(binding, split, analysis, path))
        resolved_inputs.append(
            ResolvedDatasetInput(
                dataset_id=key,
                analysis_id=analysis.id,
                source_sha256=source_sha,
                split_sha256=split_digest(split),
                analysis_sha256=payload_digest(analysis),
            )
        )
    resolved = probe.prepare.resolved.model_copy(
        update={"spec": request.spec, "datasets": resolved_inputs}
    )
    commands = []
    for rank in range(world_size):
        prepared = probe.prepare.model_copy(
            update={
                "resolved": resolved,
                "rank": rank,
                "world_size": world_size,
                "node": "coire-edge-a" if rank == 0 else "coire-edge-b",
                "command_id": uuid.uuid4(),
                "reservation_id": uuid.uuid4(),
                "disk_reservation_id": uuid.uuid4(),
                "request_sha256": payload_digest(resolved),
            }
        )
        commands.append(probe.model_copy(update={"prepare": prepared}))
    row.request = request.model_dump(mode="json")
    return (
        row,
        TrainingMeasurementDispatch(
            commands=commands, sources=sources, spawn_nonce=original.spawn_nonce
        ),
        inputs,
    )


async def persist_measured_profile(
    session: "AsyncSession",
    owner: uuid.UUID,
    resolved: ResolvedTrainingSpec,
    nodes: "list[NodeRow]",
    *,
    residents: "list[TrainingResidentTarget] | None" = None,
) -> ResolvedTrainingSpec:
    """Persist internally consistent SIMULATED node/report evidence for DB races.

    This is not a measurement of a model or Studio. It tests the real strict
    evidence gate/digests/transactions rather than mocking admission eligibility.
    """
    from coire_api.db import TrainingProfileRow
    from coire_api.training.specs import training_config_digest
    from coire_core.models.training import (
        TargetLatencyMeasurement,
        TrainingMeasurementPhase,
        TrainingMeasurementResult,
        TrainingMemoryEvidence,
        TrainingNodeMemoryEvidence,
        TrainingProfile,
    )
    from coire_scheduler.training_guard import (
        hardware_digest,
        measurement_report_digest,
        memory_evidence_digest,
        profile_identity,
    )

    now = datetime.now(UTC)
    measurement_id, profile_id = uuid.uuid4(), uuid.uuid4()
    envelope = resolved.resource_envelope.model_copy(deep=True)
    evidence = TrainingMemoryEvidence(
        measurement_id=measurement_id,
        training_config_sha256=training_config_digest(resolved.spec),
        base_manifest_sha256=resolved.base_manifest_sha256,
        tokenizer_sha256=resolved.tokenizer_sha256,
        template_sha256=resolved.template_sha256,
        runtime_sha256=resolved.runtime_sha256,
        worker_version=resolved.worker_version,
        completed_updates=1,
        resource_envelope=envelope,
        nodes=[
            TrainingNodeMemoryEvidence(
                node=node.name,
                hardware_sha256=hardware_digest(node),
                peak_footprint_bytes=envelope.memory_bytes,
                swap_growth_bytes=0,
                thermal_ok=True,
            )
            for node in nodes
        ],
        measured_at=now,
        valid_until=now + timedelta(days=7),
    )
    envelope.evidence_sha256 = memory_evidence_digest(evidence)
    request = TrainingMeasurementRequest.model_validate(
        {
            "spec": resolved.spec,
            "nodes": [node.name for node in nodes],
            "resident_targets": residents or [],
            "mode": "coexistence" if residents else "memory",
            "workload": {
                "sha256": DIGEST,
                "concurrency_per_target": 1,
                "arrival_interval_ms": 1000,
                "max_output_tokens": 16,
            },
        }
    )
    phases = (
        [
            TrainingMeasurementPhase(
                phase=phase,
                workload_sha256=DIGEST,
                started_at=now - timedelta(minutes=30 if phase == "baseline" else 15),
                finished_at=now - timedelta(minutes=15 if phase == "baseline" else 0),
                samples={item.instance_id: [0.3] * 100 for item in residents or []},
                failures=0,
            )
            for phase in ("baseline", "mixed")
        ]
        if residents
        else []
    )
    report = TrainingMeasurementResult.model_validate(
        {
            "id": measurement_id,
            "request": request,
            "state": "succeeded",
            "profile_id": profile_id,
            "report_sha256": DIGEST,
            "completed_updates": 1,
            "thermal_ok": True,
            "peak_memory_bytes": envelope.memory_bytes,
            "created_at": now,
            "memory_evidence": evidence,
            "phases": phases,
            "targets": [
                TargetLatencyMeasurement(
                    instance_id=item.instance_id,
                    baseline_requests=100,
                    mixed_requests=100,
                    baseline_p95_seconds=0.3,
                    mixed_p95_seconds=0.3,
                )
                for item in residents or []
            ],
        }
    )
    report.report_sha256 = measurement_report_digest(report)
    session.add(
        TrainingMeasurementRow(
            id=measurement_id,
            owner_user_id=owner,
            request=request.model_dump(mode="json"),
            state="succeeded",
            report=report.model_dump(mode="json"),
            report_sha256=report.report_sha256,
        )
    )
    await session.flush()
    profile = TrainingProfile(
        id=profile_id,
        report_sha256=report.report_sha256,
        request=request,
        expires_at=evidence.valid_until,
    )
    session.add(
        TrainingProfileRow(
            id=profile_id,
            measurement_id=measurement_id,
            report_sha256=report.report_sha256,
            identity_sha256=profile_identity(request, resolved.runtime_sha256),
            profile=profile.model_dump(mode="json"),
            valid_until=evidence.valid_until,
        )
    )
    await session.flush()
    return resolved.model_copy(update={"resource_envelope": envelope})
