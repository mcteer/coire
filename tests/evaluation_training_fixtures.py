"""Inert complete training lineage for evaluation obligation transaction tests."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import yaml
from evaluation_fixtures import FIXTURE, seed_evaluation, seed_evaluation_resident
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    EvaluationRunRow,
    EvaluationSuiteRow,
    TrainingAdapterRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingJobRow,
)
from coire_api.evaluation.catalog import build_suite
from coire_api.training.service import payload_digest
from coire_core.models.evaluation import (
    EvaluationSubject,
    EvaluationSuiteRegistration,
    EvaluationWorkload,
)
from coire_core.models.training import parse_resolved_training_spec


async def seed_evaluated_training(
    session: AsyncSession, *, version: int = 2, state: str = "succeeded"
) -> tuple[str, uuid.UUID]:
    original = await seed_evaluation(session)
    seed = await session.get(EvaluationRunRow, original)
    assert seed is not None
    await seed_evaluation_resident(session)
    work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
    legacy = json.loads((FIXTURE.parent / "legacy_training/resolved.json").read_bytes())
    spec = legacy["spec"]
    spec["model"] = {
        "model_id": str(work.target.target.model_id),
        "variant_id": str(work.target.target.variant_id),
    }
    base = work.target.model_copy(
        update={
            "variant_slug": "synthetic-evaluation",
            "runtime": work.target.runtime.model_copy(
                update={
                    "tokenizer_sha256": legacy["tokenizer_sha256"],
                    "template_sha256": legacy["template_sha256"],
                }
            ),
        }
    )
    if version in {2, 3}:
        spec["schema_version"] = 2
        spec["output"]["checkpoint_every_updates"] = 8
        spec["eval"]["suites"] = [
            {"suite_id": "task-final", "suite_version": 1, "checkpoint_updates": [8, 16]},
            {"suite_id": "judge-final", "suite_version": 1, "checkpoint_updates": []},
        ]
        judge_model = uuid.uuid4()
        judge = base.model_copy(
            update={
                "public_selector": judge_model,
                "target": base.target.model_copy(
                    update={
                        "model_id": judge_model,
                        "variant_id": uuid.uuid4(),
                        "base_manifest_sha256": "b" * 64,
                    }
                ),
            }
        )
        definitions = [
            build_suite(
                EvaluationSuiteRegistration(
                    suite_id="task-final", version=1, template_id="task-coding-instructions"
                ),
                owner=seed.owner_user_id,
                judge=None,
                now=datetime.now(UTC),
            ),
            build_suite(
                EvaluationSuiteRegistration(
                    suite_id="judge-final",
                    version=1,
                    template_id="judge-rubric",
                    judge=EvaluationSubject(
                        model_id=judge.target.model_id, variant_id=judge.target.variant_id
                    ),
                ),
                owner=seed.owner_user_id,
                judge=judge,
                now=datetime.now(UTC),
            ),
        ]
        for definition in definitions:
            session.add(
                EvaluationSuiteRow(
                    id=uuid.uuid4(),
                    suite_id=definition.suite_id,
                    version=1,
                    registry_version=1,
                    definition=definition.model_dump(mode="json"),
                    content_sha256=definition.content_sha256,
                    owner_user_id=seed.owner_user_id,
                    retired=False,
                )
            )
        legacy["evaluations"] = [
            {"schedule": schedule, "suite": suite.model_dump(mode="json")}
            for schedule, suite in zip(spec["eval"]["suites"], definitions, strict=True)
        ]
        legacy["evaluation_base"] = base.model_dump(mode="json")
    if version == 3:
        spec.update(
            schema_version=3, objective="dpo", objective_options={"beta": 0.1}, init_adapter=None
        )
        spec["parameterization"]["dropout"] = 0
        spec["data"]["loss_policy"] = "final_assistant"
        target = {**spec["model"], "base_manifest_sha256": legacy["base_manifest_sha256"]}
        legacy.update(
            initial_target=target, reference_target=target, sampler_version="coire-pair-sampler-v1"
        )
        legacy["resource_envelope"].update(reference_weight_bytes=1, reference_adapter_bytes=0)
    resolved = parse_resolved_training_spec(legacy)
    source = yaml.safe_dump(spec)
    now = datetime.now(UTC)
    identity = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
    job = TrainingJobRow(
        id=identity,
        owner_user_id=seed.owner_user_id,
        model_id=work.target.target.model_id,
        base_variant_id=work.target.target.variant_id,
        idempotency_key="synthetic-training",
        output_slug=spec["output"]["adapter_slug"],
        source_yaml=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        intent_sha256="a" * 64,
        submitted_spec=spec,
        resolved_spec=resolved.model_dump(mode="json"),
        resolved_sha256=payload_digest(resolved),
        authorization_snapshot=seed.authorization_snapshot,
        state=state,
        version=1,
        fence=1,
        completed_update=spec["optim"]["updates"],
        queue_deadline_at=now + timedelta(minutes=5),
        execution_deadline_at=now + timedelta(hours=1),
    )
    session.add(job)
    await session.flush()
    attempt = TrainingAttemptRow(
        id="01ARZ3NDEKTSV4RRFFQ69G5FAY",
        job_id=identity,
        generation=1,
        fence=1,
        world_size=1,
        runtime_sha256=resolved.runtime_sha256,
        state="stopped",
        stopped_at=now,
        lease_expires_at=now,
    )
    session.add(attempt)
    await session.flush()
    checkpoint = TrainingCheckpointRow(
        id=uuid.uuid4(),
        job_id=identity,
        attempt_id=attempt.id,
        fence=1,
        completed_update=job.completed_update,
        manifest_sha256="8" * 64,
        manifest={},
        total_bytes=1,
        state="committed",
        committed_at=now,
    )
    session.add(checkpoint)
    await session.flush()
    adapter = TrainingAdapterRow(
        id=uuid.uuid4(),
        model_id=job.model_id,
        base_variant_id=job.base_variant_id,
        source_job_id=identity,
        source_checkpoint_id=checkpoint.id,
        slug=job.output_slug,
        selector=f"{job.model_id}@{job.output_slug}",
        base_manifest_sha256=resolved.base_manifest_sha256,
        manifest_sha256="c" * 64,
        resolved_spec_sha256=job.resolved_sha256,
        parameterization=spec["parameterization"]["kind"],
        objective="dpo" if version == 3 else "sft",
        state="ready",
        visibility="admin_only",
        verified=False,
        metadata_record={"automatic": True, "authority": seed.authorization_snapshot},
    )
    session.add(adapter)
    await session.flush()
    job.adapter_id = adapter.id
    job.latest_checkpoint_id = checkpoint.id
    await session.commit()
    return identity, adapter.id
