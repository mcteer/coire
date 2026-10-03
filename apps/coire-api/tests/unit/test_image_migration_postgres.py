"""Image migration safety on an explicitly local disposable PostgreSQL server."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

import asyncpg
import pytest
from alembic import command
from alembic.config import Config


@pytest.mark.integration
def test_image_migrations_preserve_existing_rows_and_guard_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_dsn = os.environ.get("COIRE_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("set COIRE_TEST_POSTGRES_DSN for disposable local PostgreSQL")
    parsed = urlparse(admin_dsn)
    if parsed.scheme not in {"postgresql", "postgres"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        pytest.fail("migration test requires an explicit local PostgreSQL server")
    database = "coire_image_migration_" + uuid.uuid4().hex[:12]
    test_dsn = urlunparse(parsed._replace(path="/" + database))
    model_id, text_model_id, user_id, other_user_id = (uuid.uuid4() for _ in range(4))
    job_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

    async def create_database() -> None:
        connection = await asyncpg.connect(admin_dsn)
        try:
            await connection.execute(f"CREATE DATABASE {database}")
        finally:
            await connection.close()

    async def drop_database() -> None:
        connection = await asyncpg.connect(admin_dsn)
        try:
            await connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
                database,
            )
            await connection.execute(f"DROP DATABASE IF EXISTS {database}")
        finally:
            await connection.close()

    async def seed_existing_rows() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            await connection.execute(
                """INSERT INTO models
                   (id, repo_id, slug, display_name, precision, weight_bytes,
                    total_bytes, file_count, memory_estimate_bytes, backend,
                    visual_capability)
                   VALUES ($1, 'test/vision', 'vision@4bit', 'Vision', '4bit',
                           1, 1, 1, 1, 'mlx_vlm', '{}'::jsonb)""",
                model_id,
            )
            await connection.execute(
                """INSERT INTO models
                   (id, repo_id, slug, display_name, precision, weight_bytes,
                    total_bytes, file_count, memory_estimate_bytes)
                   VALUES ($1, 'test/text', 'text@4bit', 'Text', '4bit', 1, 1, 1, 1)""",
                text_model_id,
            )
            await connection.execute(
                "INSERT INTO users (id, email, display_name, role) "
                "VALUES ($1, 'image-migration@example.test', 'Owner', 'user')",
                user_id,
            )
            await connection.execute(
                "INSERT INTO users (id, email, display_name, role) "
                "VALUES ($1, 'image-other@example.test', 'Other', 'user')",
                other_user_id,
            )
        finally:
            await connection.close()

    async def check_upgraded_rows_and_constraints() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert (
                await connection.fetchval("SELECT backend FROM models WHERE id = $1", model_id)
                == "mlx_vlm"
            )
            assert (
                await connection.fetchval(
                    "SELECT visual_capability FROM models WHERE id = $1", model_id
                )
                == "{}"
            )
            assert (
                await connection.fetchval("SELECT backend FROM models WHERE id = $1", text_model_id)
                == "mlx_lm"
            )
            await connection.execute(
                """INSERT INTO image_jobs
                   (id, owner_user_id, idempotency_key, intent_sha256,
                    submitted_spec, resolved_spec, deadline_at, authorization_snapshot)
                   VALUES ($1, $2, 'same-key', repeat('a', 64), '{}'::jsonb,
                           '{}'::jsonb, now() + interval '1 hour', '{}'::jsonb)""",
                job_id,
                user_id,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    """INSERT INTO image_jobs
                       (id, owner_user_id, idempotency_key, intent_sha256,
                        submitted_spec, resolved_spec, deadline_at, authorization_snapshot)
                       VALUES ('bad', $1, 'bad-id', repeat('a', 64), '{}'::jsonb,
                               '{}'::jsonb, now() + interval '1 hour', '{}'::jsonb)""",
                    user_id,
                )
            with pytest.raises(asyncpg.UniqueViolationError):
                await connection.execute(
                    """INSERT INTO image_jobs
                       (id, owner_user_id, idempotency_key, intent_sha256,
                        submitted_spec, resolved_spec, deadline_at, authorization_snapshot)
                       VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAA', $1, 'same-key',
                               repeat('a', 64), '{}'::jsonb, '{}'::jsonb,
                               now() + interval '1 hour', '{}'::jsonb)""",
                    user_id,
                )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await connection.execute(
                    """INSERT INTO image_outputs
                       (id, job_id, owner_user_id, output_index, blob_key, size_bytes,
                        file_sha256, pixel_sha256, recipe, content_tag,
                        classifier_provenance, entitlement_snapshot)
                       VALUES ($1, $2, $3, 0, 'internal/output', 1,
                               repeat('a', 64), repeat('b', 64), '{}'::jsonb,
                               'normal', '{}'::jsonb, '{}'::jsonb)""",
                    uuid.uuid4(),
                    job_id,
                    other_user_id,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    """INSERT INTO image_inputs
                       (id, owner_user_id, purpose, original_key, original_bytes,
                        normalized_key, state)
                       VALUES ($1, $2, 'recipe', 'internal/original', 12,
                               'internal/normalized', 'processing')""",
                    uuid.uuid4(),
                    user_id,
                )
        finally:
            await connection.close()

    async def check_guarded_state() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                "0030_image_output_retention"
            )
            assert await connection.fetchval("SELECT count(*) FROM image_jobs") == 1
            await connection.execute("DELETE FROM image_jobs WHERE id = $1", job_id)
        finally:
            await connection.close()

    async def check_old_rows_after_downgrade() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                "0022_stopped_usage_outcome"
            )
            assert (
                await connection.fetchval("SELECT backend FROM models WHERE id = $1", model_id)
                == "mlx_vlm"
            )
            assert (
                await connection.fetchval("SELECT backend FROM models WHERE id = $1", text_model_id)
                == "mlx_lm"
            )
            assert (
                await connection.fetchval("SELECT id FROM users WHERE id = $1", user_id) == user_id
            )
        finally:
            await connection.close()

    monkeypatch.setenv("POSTGRES_HOST", parsed.hostname or "localhost")
    monkeypatch.setenv("POSTGRES_PORT", str(parsed.port or 5432))
    monkeypatch.setenv("POSTGRES_USER", unquote(parsed.username or ""))
    monkeypatch.setenv("POSTGRES_PASSWORD", unquote(parsed.password or ""))
    monkeypatch.setenv("POSTGRES_DB", database)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    asyncio.run(create_database())
    try:
        command.upgrade(config, "0022_stopped_usage_outcome")
        asyncio.run(seed_existing_rows())
        command.upgrade(config, "head")
        asyncio.run(check_upgraded_rows_and_constraints())
        with pytest.raises(RuntimeError, match="image records"):
            command.downgrade(config, "0022_stopped_usage_outcome")
        asyncio.run(check_guarded_state())
        command.downgrade(config, "0022_stopped_usage_outcome")
        asyncio.run(check_old_rows_after_downgrade())
    finally:
        asyncio.run(drop_database())
