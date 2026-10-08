"""Older v1 training and optional node capabilities retain their wire identity."""

import asyncio
import copy
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from evaluation_training_fixtures import seed_evaluated_training
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import Base, TrainingAdapterRow, TrainingEvaluationTriggerRow, TrainingJobRow
from coire_api.evaluation.training import ensure_final_trigger
from coire_core.models.training import ResolvedTrainingSpec, parse_resolved_training_spec
from coire_core.models.training_node import (
    CheckpointCommitAcknowledgement,
    parse_checkpoint_acknowledgement,
)
from coire_core.settings import Settings

pytestmark = pytest.mark.integration
CHECKPOINT = json.loads(
    (
        Path(__file__).resolve().parents[4]
        / "tests/fixtures/evaluations/legacy_training/checkpoint.json"
    ).read_bytes()
)
REPOSITORY = Path(__file__).resolve().parents[4]


def previous_binary(root: Path) -> None:
    """Read-only export of the reviewed 016 parent, never a branch checkout."""
    archive = subprocess.check_output(
        [
            "git",
            "archive",
            "ec8679c1a3ede4c7b4d897ab4c27b0833a517ecc",
            "packages/coire-core",
            "apps/coire-api",
        ],
        cwd=REPOSITORY,
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(root, filter="data")


async def test_drained_previous_binary_reads_v1_with_additive_evaluation_tables(
    training_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, _ = await seed_evaluated_training(session, version=1)
            job = await session.get(TrainingJobRow, job_id)
            assert job is not None
            original, digest = job.resolved_spec, job.resolved_sha256
            assert (
                await session.scalar(select(func.count()).select_from(TrainingEvaluationTriggerRow))
                == 0
            )
        await asyncio.to_thread(previous_binary, tmp_path)
        environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(tmp_path / "packages/coire-core/src"), str(tmp_path / "apps/coire-api/src")]
            ),
        }
        # The database credential travels on stdin, not argv or diagnostics.
        script = """
import asyncio, json, sys
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from coire_api.db import TrainingJobRow
from coire_core.models.training import ResolvedTrainingSpec
from coire_core.models.training_node import TrainingArtifactManifest
async def main():
    value = json.load(sys.stdin)
    engine = create_async_engine(value["database"])
    try:
        async with AsyncSession(engine) as session:
            job = await session.get(TrainingJobRow, value["job_id"])
            assert job.state == "succeeded"
            resolved = ResolvedTrainingSpec.model_validate(job.resolved_spec)
            checkpoint = TrainingArtifactManifest.model_validate(value["checkpoint"])
            print(json.dumps({"resolved":resolved.model_dump(mode="json"),
                              "digest":job.resolved_sha256,
                              "checkpoint":checkpoint.model_dump(mode="json")}))
    finally:
        await engine.dispose()
asyncio.run(main())
"""
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            env=environment,
            cwd=tmp_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await asyncio.wait_for(
            child.communicate(
                json.dumps(
                    {
                        "database": training_postgres_url,
                        "job_id": job_id,
                        "checkpoint": CHECKPOINT,
                    }
                ).encode()
            ),
            timeout=30,
        )
        assert child.returncode == 0, "previous binary could not read drained historical state"
        received = json.loads(output)
        assert received["resolved"] == original and received["digest"] == digest
        assert received["checkpoint"] == CHECKPOINT
    finally:
        await engine.dispose()


async def test_v1_continuation_with_evaluations_disabled_keeps_resolved_identity(
    training_postgres_url: str,
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, adapter_id = await seed_evaluated_training(session, version=1)
            job = await session.get(TrainingJobRow, job_id)
            adapter = await session.get(TrainingAdapterRow, adapter_id)
            assert job is not None and adapter is not None
            original = copy.deepcopy(job.resolved_spec)
            digest = job.resolved_sha256
            for _ in range(2):
                assert (
                    await ensure_final_trigger(
                        session, job, adapter, settings=Settings(evaluations_enabled=False)
                    )
                    is None
                )
                parsed = parse_resolved_training_spec(job.resolved_spec)
                assert isinstance(parsed, ResolvedTrainingSpec)
                assert parsed.model_dump(mode="json") == original
                assert job.resolved_sha256 == digest
                assert "evaluations" not in parsed.model_dump(mode="json")
            await session.commit()
        async with AsyncSession(engine) as recovered:
            job = await recovered.get(TrainingJobRow, job_id)
            assert (
                job is not None and job.resolved_spec == original and job.resolved_sha256 == digest
            )
            assert (
                await recovered.scalar(
                    select(func.count()).select_from(TrainingEvaluationTriggerRow)
                )
                == 0
            )
            # The historical v1 acknowledgment is still accepted exactly as saved.
            value = CHECKPOINT
            ack = CheckpointCommitAcknowledgement.model_validate(
                {
                    "command_id": "00000000-0000-0000-0000-000000000001",
                    "job_id": value["job_id"],
                    "attempt_id": value["attempt_id"],
                    "fence": value["fence"],
                    "request_sha256": "a" * 64,
                    "node": "coire-edge-a",
                    "rank": 0,
                    "world_size": 1,
                    "checkpoint_id": value["artifact_id"],
                    "manifest_sha256": "a" * 64,
                    "update": value["update"],
                    "lease_expires_at": "2030-01-01T00:00:00Z",
                }
            )
            wire = ack.model_dump(mode="json")
            assert parse_checkpoint_acknowledgement(wire).model_dump(mode="json") == wire
            assert "evaluation_pause" not in wire and wire["schema_version"] == 1
    finally:
        await engine.dispose()
