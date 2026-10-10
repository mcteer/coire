"""Real node-owned v3 measurement processes on acquired tiny offline Studio assets."""

import asyncio
import hashlib
import json
import shutil
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import psutil
import pytest

from coire_core.models.datasets import DatasetAnalysisBinding, DatasetFormat
from coire_core.models.preference import PreferenceRow
from coire_core.models.training import ResolvedTrainingSpecV3
from coire_core.models.training_node import (
    DatasetAnalysisWorkerInput,
    PreferenceMeasurementObservation,
    TrainingLeaseRenewal,
    TrainingMeasurementPrepare,
    TrainingPrepareRequest,
    TrainingStartRequest,
    TrainingStopRequest,
)
from coire_core.preference_data import preference_split_digest, split_preference_rows

pytestmark = pytest.mark.engine


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
@pytest.mark.asyncio
async def test_node_owned_native_preference_probe_counts_reference_and_serialization(
    training_model: Path,
    training_kind: Literal["lora", "qlora", "dora"],
    tmp_path: Path,
    objective: Literal["dpo", "orpo"],
) -> None:
    from test_training_worker import offline_command

    import coire_core
    import coire_node
    from coire_node.training.datasets import analyze_source, load_analysis_tokenizer
    from coire_node.training.journal import TrainingJournal
    from coire_node.training.measurement import MeasurementSupervisor
    from coire_node.training.preference_data import PreferenceFrozenInputs
    from coire_node.training.preference_runtime import validate_preference_input
    from coire_node.training.supervisor import NativeProcesses
    from coire_node.training.worker import payload_sha256

    old = offline_command(training_model, training_kind)
    raw = old.model_dump(mode="json")
    resolved = raw["resolved"]
    spec = resolved["spec"]
    spec.update(
        schema_version=3,
        objective=objective,
        init_adapter=None,
        objective_options={"beta": 0.1} if objective == "dpo" else {"weight": 0.1},
    )
    spec["parameterization"]["dropout"] = 0
    spec["optim"].update(updates=2, max_sequence_length=256)
    spec["data"]["train"]["datasets"][0]["sample_count"] = 2
    spec["data"]["train"]["epoch_samples"] = 2
    model = await asyncio.to_thread(
        validate_preference_input,
        training_model,
        old.resolved.spec.parameterization.model_copy(update={"dropout": 0}),
    )
    resolved["base_manifest_sha256"] = model.manifest.sha256()
    target = {**spec["model"], "base_manifest_sha256": model.manifest.sha256()}
    resolved.update(
        initial_target=target,
        reference_target=target if objective == "dpo" else None,
        sampler_version="coire-pair-sampler-v1",
    )
    resolved["resource_envelope"].update(
        weight_bytes=1,
        adapter_bytes=1,
        optimizer_bytes=1,
        activation_bytes=1,
        reference_weight_bytes=1 if objective == "dpo" else 0,
        reference_adapter_bytes=0,
        buffer_bytes=6 * 1024**3,
        safety_bytes=1024**3,
        checkpoint_bytes=128 * 1024**2,
    )
    selected = resolved["datasets"][0]
    rows = [
        PreferenceRow.model_validate(
            {
                "prompt": [{"role": "user", "content": f"Choose a concise answer {i}."}],
                "chosen": "The answer is yes.",
                "rejected": "The answer is no.",
            }
        )
        for i in range(4)
    ]
    encoded = b"".join(json.dumps(row.model_dump(mode="json")).encode() + b"\n" for row in rows)
    source = tmp_path / "source.jsonl"
    await asyncio.to_thread(source.write_bytes, encoded)
    await asyncio.to_thread(source.chmod, 0o600)
    digest = hashlib.sha256(encoded).hexdigest()
    split = split_preference_rows(
        uuid.UUID(selected["dataset_id"]), digest, rows, seed=0, validation_fraction=0.5
    )
    binding = DatasetAnalysisBinding.model_validate(
        {
            "dataset_id": selected["dataset_id"],
            "model_id": spec["model"]["model_id"],
            "variant_id": spec["model"]["variant_id"],
            "base_manifest_sha256": model.manifest.sha256(),
            "source_sha256": digest,
            "split_sha256": preference_split_digest(split),
            "model_slug": training_model.name,
            "format": DatasetFormat.PREFERENCE,
        }
    )
    tokenizer, tok, template, runtime = await asyncio.to_thread(
        load_analysis_tokenizer, training_model, binding
    )
    analysis = await asyncio.to_thread(
        analyze_source,
        source,
        DatasetAnalysisWorkerInput(
            command_id=uuid.uuid4(),
            analysis_id=uuid.UUID(selected["analysis_id"]),
            binding=binding,
            source_bytes=len(encoded),
            memory_bytes=1024**3,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        ),
        tokenizer,
        tokenizer_sha256=tok,
        template_sha256=template,
        runtime_sha256=runtime,
        max_sequence_length=256,
    )
    assert analysis.state == "succeeded"
    selected.update(
        source_sha256=digest,
        split_sha256=binding.split_sha256,
        analysis_sha256=payload_sha256(analysis),
    )
    resolved.update(tokenizer_sha256=tok, template_sha256=template, runtime_sha256=runtime)
    raw["lease_expires_at"] = (datetime.now(UTC) + timedelta(seconds=29)).isoformat()
    prepared = TrainingPrepareRequest.model_validate(raw)
    assert isinstance(prepared.resolved, ResolvedTrainingSpecV3)
    probe = TrainingMeasurementPrepare(
        measurement_id=uuid.uuid4(),
        prepare=prepared,
        hardware_sha256="a" * 64,
        mode="memory",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    journal = TrainingJournal(
        tmp_path / "node", node=prepared.node, admission_lock=threading.RLock()
    )

    class SourceProcesses(NativeProcesses):
        def spawn(self, argv: list[str], env: dict[str, str]) -> tuple[int, float]:
            assert "PYTHONPATH" not in env
            roots = []
            for package in (coire_core, coire_node):
                assert package.__file__ is not None
                roots.append(str(Path(package.__file__).parent.parent))
            return super().spawn(argv, {**env, "PYTHONPATH": ":".join(roots)})

    supervisor = MeasurementSupervisor(
        journal,
        interpreter=Path(sys.executable),
        accelerator_guard=lambda _: None,
        hardware_sha256=lambda: "a" * 64,
        store_root=training_model.parent,
        artifact_root=tmp_path / "probes",
        initial_artifact_root=tmp_path / "artifacts",
        memory_available=lambda: min(8 * 1024**3, psutil.virtual_memory().available),
        disk_available=lambda: shutil.disk_usage(tmp_path).free,
    )
    supervisor.processes = SourceProcesses()
    wire = prepared.model_dump(
        mode="json",
        exclude={
            "resolved",
            "reservation_id",
            "disk_reservation_id",
            "resume_checkpoint_id",
            "resume_manifest_sha256",
            "collective",
        },
    )
    started = False
    try:
        assert (await supervisor.prepare_measurement(probe)).ready is False
        await supervisor.bind_inputs(
            prepared,
            PreferenceFrozenInputs(binding, split, analysis, source),
            disk_available=shutil.disk_usage(tmp_path).free,
        )
        request = TrainingStartRequest.model_validate(
            {
                **wire,
                "command_id": str(uuid.uuid4()),
                "prepared_command_id": str(prepared.command_id),
                "spawn_nonce": str(uuid.uuid4()),
            }
        )
        await supervisor.start(request)
        started = True
        deadline, renew_at = time.monotonic() + 90, time.monotonic() + 6
        status = None
        while time.monotonic() < deadline:
            status = supervisor.measurement_status(prepared.attempt_id)
            if status.stopped:
                break
            if time.monotonic() >= renew_at:
                await supervisor.renew(
                    TrainingLeaseRenewal.model_validate(
                        {
                            **wire,
                            "command_id": str(uuid.uuid4()),
                            "lease_expires_at": (
                                datetime.now(UTC) + timedelta(seconds=29)
                            ).isoformat(),
                        }
                    )
                )
                renew_at = time.monotonic() + 6
            await asyncio.sleep(0.1)
        assert status is not None and status.stopped
        measured = status.observation
        assert isinstance(measured, PreferenceMeasurementObservation), status.model_dump_json()
        assert measured.completed_updates == measured.probe_count == 2
        assert measured.objective == objective and measured.checkpoint_bytes > 0
        assert measured.serialization_peak_bytes > 0 and measured.buffer_bytes > 0
        assert (measured.reference_weight_bytes > 0) == (objective == "dpo")
        assert (
            measured.reference_adapter_bytes == 0
            and measured.swap_growth_bytes == 0
            and measured.thermal_ok
        )
        assert (
            measured.weight_bytes > 0
            and measured.adapter_bytes > 0
            and measured.optimizer_bytes > 0
        )
        # Local probe saves are never durable training checkpoints.
        assert not list((tmp_path / "probes" / prepared.attempt_id).glob("*/manifest.json"))
    finally:
        if started:
            await supervisor.stop(
                TrainingStopRequest.model_validate(
                    {
                        **wire,
                        "command_id": str(uuid.uuid4()),
                        "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=29)).isoformat(),
                        "reason": "cancelled",
                    }
                )
            )
        journal.close()
