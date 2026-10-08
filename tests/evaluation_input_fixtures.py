"""Inert digest-bound consumed-input fixture; no models or engine execution."""

import uuid
from pathlib import Path

from coire_core.models.datasets import DatasetFormat
from coire_core.models.evaluation import EvaluationWorkload
from coire_core.training_data import normalize_row


def staged_workload(root: Path) -> EvaluationWorkload:
    import hashlib
    import json
    import random
    from datetime import UTC, datetime, timedelta

    from coire_core.evaluation_suites import cases
    from coire_core.models.datasets import DatasetMixture, DatasetSource, SplitManifest
    from coire_core.models.evaluation import EvaluationInputFile, EvaluationWorkload
    from coire_core.models.evaluation_inputs import (
        EvaluationTrainingBinding,
        EvaluationTrainingSource,
    )
    from coire_core.models.training import TrainingOptimizer
    from coire_core.models.training_node import CheckpointWorkerState, SingleSourceSamplerState
    from coire_core.training_data import split_digest

    fixture = Path(__file__).resolve().parent / "fixtures/evaluations/workload.json"
    work = EvaluationWorkload.model_validate_json(fixture.read_bytes())
    identity = uuid.uuid4()
    records = [
        {
            "prompt": cases(work.suite.template.template_id)[0].prompt,
            "completion": "different target",
        },
        {"prompt": "not consumed", "completion": "different target"},
        {"prompt": "held out", "completion": "validation"},
    ]
    raw = b"".join(json.dumps(value).encode() + b"\n" for value in records)
    source_sha = hashlib.sha256(raw).hexdigest()
    examples = [
        normalize_row(
            value, format=DatasetFormat.PROMPT_COMPLETION, dataset_id=identity, source_row=row
        )
        for row, value in enumerate(records, 1)
    ]
    split = SplitManifest(
        dataset_id=identity,
        source_sha256=source_sha,
        seed=0,
        train_rows=[1, 2],
        validation_rows=[3],
        row_content_sha256=[example.content_sha256() for example in examples],
    )
    split_sha = split_digest(split)
    split_raw = json.dumps(
        split.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    rng = random.Random(0)
    permutation = [0, 1]
    rng.shuffle(permutation)
    version, words, gaussian = rng.getstate()
    optimizer = TrainingOptimizer(updates=2, batch_size=1, accumulation_steps=1)
    state = CheckpointWorkerState(
        job_id=work.evaluation_id,
        attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAY",
        fence=1,
        completed_update=1,
        rank=0,
        world_size=1,
        runtime_sha256="d" * 64,
        resolved_spec_sha256="e" * 64,
        optimizer=optimizer,
        mlx_rng_key=(0, 1),
        sampler=SingleSourceSamplerState(
            dataset_sha256=hashlib.sha256(
                (split_sha + ":train:" + str([1, 2])).encode()
            ).hexdigest(),
            row_count=2,
            batch_size=1,
            max_sequence_length=optimizer.max_sequence_length,
            epoch=0,
            cursor=1,
            permutation=permutation,
            rng_version=version,
            rng_state=list(words),
            gaussian_cache=gaussian,
        ),
    )
    state_raw = state.model_dump_json().encode()
    source = EvaluationTrainingSource(
        dataset_id=identity,
        format=DatasetFormat.PROMPT_COMPLETION,
        source_sha256=source_sha,
        source_bytes=len(raw),
        split_sha256=split_sha,
        split_bytes=len(split_raw),
    )
    binding = EvaluationTrainingBinding(
        job_id=state.job_id,
        training_attempt_id=state.attempt_id,
        training_fence=1,
        checkpoint_id=uuid.uuid4(),
        checkpoint_manifest_sha256="c" * 64,
        resolved_spec_sha256="e" * 64,
        runtime_sha256="d" * 64,
        completed_update=1,
        batch_size=1,
        accumulation_steps=1,
        seed=0,
        mixture=DatasetMixture(
            datasets=[DatasetSource(dataset_id=identity, sample_count=2, mixture_proportion=1.0)],
            epoch_samples=2,
            seed=0,
        ),
        state_file_id="rank-0-state",
        state_sha256=hashlib.sha256(state_raw).hexdigest(),
        state_bytes=len(state_raw),
        sources=[source],
    )
    files = []
    for name, data, purpose in [
        (f"source-{identity}.jsonl", raw, "training_source"),
        (f"split-{identity}.json", split_raw, "split_manifest"),
        ("training-state.json", state_raw, "training_state"),
    ]:
        (root / name).write_bytes(data)
        files.append(
            EvaluationInputFile.model_validate(
                {
                    "name": name,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "purpose": purpose,
                }
            )
        )
    return EvaluationWorkload.model_validate(
        {
            **work.model_dump(mode="json"),
            "training": binding.model_dump(mode="json"),
            "input_files": [item.model_dump(mode="json") for item in files],
            "deadline": datetime.now(UTC) + timedelta(minutes=1),
        }
    )
