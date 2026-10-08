"""Actual frozen migration against disposable populated feature-016 storage."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from evaluation_fixtures import seed_evaluation
from sqlalchemy import insert, inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from coire_api.db import Base, ModelRow, ModelVariantRow, TrainingAttemptRow, TrainingCheckpointRow
from coire_api.evaluation.finalizer import finalize

pytestmark = pytest.mark.integration
ALEMBIC_CONFIG = Path(__file__).resolve().parents[2] / "alembic.ini"
FIXTURES = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/legacy_training"
JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


async def seed_legacy(connection: AsyncConnection) -> None:
    recipe = json.loads((FIXTURES / "recipe.json").read_text())
    resolved = json.loads((FIXTURES / "resolved.json").read_text())
    manifest = json.loads((FIXTURES / "checkpoint.json").read_text())
    digests = json.loads((FIXTURES / "digests.json").read_text())["digests"]
    model, variant = recipe["model"]["model_id"], recipe["model"]["variant_id"]
    await connection.execute(
        insert(ModelRow).values(
            id=uuid.UUID(model),
            repo_id="synthetic/migration",
            slug="migration-fixture",
            display_name="Migration fixture",
            state="ready",
            visibility="admin_only",
            placement_policy="single:auto",
            memory_estimate_bytes=1024,
            idle_ttl_seconds=900,
            precision="bf16",
            weight_bytes=1024,
            total_bytes=1024,
            file_count=2,
        )
    )
    await connection.execute(
        insert(ModelVariantRow).values(
            id=uuid.UUID(variant),
            model_id=uuid.UUID(model),
            name="fixture",
            slug="migration-base",
            precision="bf16",
            state="ready",
            memory_estimate_bytes=1024,
            source_revision="fixture",
        )
    )
    await connection.execute(
        text("""INSERT INTO training_jobs
          (id,owner_user_id,model_id,base_variant_id,idempotency_key,output_slug,
           source_yaml,source_sha256,intent_sha256,submitted_spec,resolved_spec,resolved_sha256,
           state,queue_deadline_at,execution_deadline_at)
          VALUES (:id,'00000000-0000-4000-8000-000000000001',:model,:variant,'legacy','fixture',
           :source,:recipe_digest,:recipe_digest,CAST(:recipe AS jsonb),CAST(:resolved AS jsonb),
           :resolved_digest,'succeeded',now(),now())"""),
        {
            "id": JOB,
            "model": uuid.UUID(model),
            "variant": uuid.UUID(variant),
            "source": (FIXTURES / "recipe.json").read_text(),
            "recipe": json.dumps(recipe),
            "resolved": json.dumps(resolved),
            "recipe_digest": digests["recipe"],
            "resolved_digest": digests["resolved"],
        },
    )
    from datetime import UTC, datetime

    await connection.execute(
        insert(TrainingAttemptRow).values(
            id=JOB,
            job_id=JOB,
            generation=1,
            fence=1,
            world_size=1,
            runtime_sha256="a" * 64,
            state="stopped",
            lease_expires_at=datetime.now(UTC),
        )
    )
    await connection.execute(
        insert(TrainingCheckpointRow).values(
            id=uuid.UUID(manifest["artifact_id"]),
            job_id=JOB,
            attempt_id=JOB,
            fence=1,
            completed_update=manifest["update"],
            manifest_sha256=digests["checkpoint"],
            manifest=manifest,
            total_bytes=manifest["total_bytes"],
            state="committed",
        )
    )


async def assert_legacy(connection: AsyncConnection) -> None:
    job = (
        await connection.execute(
            text(
                "SELECT submitted_spec,resolved_spec,resolved_sha256,source_yaml FROM training_jobs WHERE id=:id"
            ),
            {"id": JOB},
        )
    ).one()
    digests = json.loads((FIXTURES / "digests.json").read_text())["digests"]
    assert job.submitted_spec == json.loads((FIXTURES / "recipe.json").read_text())
    assert job.resolved_spec == json.loads((FIXTURES / "resolved.json").read_text())
    assert job.resolved_sha256 == digests["resolved"]
    assert job.source_yaml == (FIXTURES / "recipe.json").read_text()
    checkpoint = (
        await connection.execute(
            text("SELECT manifest,manifest_sha256 FROM training_checkpoints WHERE job_id=:id"),
            {"id": JOB},
        )
    ).one()
    assert checkpoint.manifest == json.loads((FIXTURES / "checkpoint.json").read_text())
    assert checkpoint.manifest_sha256 == digests["checkpoint"]


def column_names(connection: Connection, name: str) -> set[str]:
    return {str(v["name"]) for v in inspect(connection).get_columns(name)}


async def test_evaluation_migration_preserves_016_and_protects_history(
    training_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = make_url(training_postgres_url)
    assert url.database == "training_test" and url.host == "127.0.0.1" and url.password
    for key, value in {
        "POSTGRES_HOST": url.host,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_USER": url.username or "postgres",
        "POSTGRES_DB": url.database,
        "POSTGRES_PASSWORD": url.password,
    }.items():
        monkeypatch.setenv(key, value)
    config = Config(str(ALEMBIC_CONFIG))
    engine = create_async_engine(training_postgres_url)
    try:
        # The session-scoped fixture is a disposable database shared with ORM tests.
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await asyncio.to_thread(command.upgrade, config, "0031_sft_training")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id,email,display_name,role,active) VALUES ('00000000-0000-4000-8000-000000000001','evaluation@test.local','Admin','admin',true)"
                )
            )
            await seed_legacy(conn)
        await asyncio.to_thread(command.upgrade, config, "0032_evaluation_verbs")
        async with engine.connect() as conn:
            await assert_legacy(conn)
            for name, table in Base.metadata.tables.items():
                if name.startswith("evaluation_") or name == "training_evaluation_triggers":
                    columns = await conn.run_sync(column_names, name)
                    assert columns == set(table.columns.keys())
            row = (
                await conn.execute(
                    text(
                        "SELECT definition,content_sha256,attribution,owner_user_id FROM evaluation_suites"
                    )
                )
            ).one()
            assert row.attribution == "system:migration-0032" and row.owner_user_id is None
            assert row.definition["template"]["case_count"] == 4
            from coire_core.models.evaluation import EvaluationSuite

            suite = EvaluationSuite.model_validate(row.definition)
            assert suite.content_sha256 == row.content_sha256
            with pytest.raises(DBAPIError, match="immutable"):
                await conn.execute(
                    text("UPDATE evaluation_suites SET content_sha256=repeat('a',64)")
                )
            await conn.rollback()
            await conn.execute(
                text("UPDATE evaluation_suites SET retired=true, registry_version=2")
            )
            await conn.commit()
        await asyncio.to_thread(command.downgrade, config, "0031_sft_training")
        async with engine.connect() as conn:
            await assert_legacy(conn)
            assert (
                await conn.scalar(
                    text("SELECT display_name FROM users WHERE email='evaluation@test.local'")
                )
                == "Admin"
            )
            assert (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                == "0031_sft_training"
            )
        await asyncio.to_thread(command.upgrade, config, "0032_evaluation_verbs")
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            result = await finalize(session, identity, fence=1, outcome="failed", reason="internal")
            await session.commit()
        async with engine.connect() as conn:
            await conn.execute(
                text("""INSERT INTO training_evaluation_triggers
                (id,job_id,checkpoint_id,boundary_kind,completed_update,schedule_sha256,
                 schedules,phase,fence,deadline_at,completed_at)
                SELECT gen_random_uuid(),job_id,id,'checkpoint',completed_update,
                 repeat('a',64),'[]'::jsonb,'complete',fence,now(),now()
                FROM training_checkpoints WHERE job_id=:job"""),
                {"job": JOB},
            )
            await conn.commit()
            with pytest.raises(DBAPIError, match="immutable"):
                await conn.execute(
                    text("UPDATE training_evaluation_triggers SET schedules='[{}]'::jsonb")
                )
            await conn.rollback()
            with pytest.raises(DBAPIError, match="fence"):
                await conn.execute(
                    text("""INSERT INTO evaluation_results
                    (id,run_id,fence,result,result_sha256,created_at)
                    SELECT '01ARZ3NDEKTSV4RRFFQ69G5FAW',run_id,2,result,result_sha256,created_at
                    FROM evaluation_results""")
                )
            await conn.rollback()
            for statement in (
                "UPDATE evaluation_runs SET data_snapshot='{}'::jsonb",
                "UPDATE evaluation_results SET result_sha256=repeat('b',64)",
                "DELETE FROM evaluation_results",
                "INSERT INTO evaluation_results SELECT * FROM evaluation_results",
                "DELETE FROM evaluation_suites WHERE suite_id='task-recovery'",
            ):
                with pytest.raises(DBAPIError):
                    await conn.execute(text(statement))
                await conn.rollback()
            assert (
                await conn.scalar(text("SELECT result_sha256 FROM evaluation_results"))
                == result.result_sha256
            )
        with pytest.raises(RuntimeError, match="retained"):
            await asyncio.to_thread(command.downgrade, config, "0031_sft_training")
    finally:
        await engine.dispose()
