"""Registry kind migration against an explicitly local disposable PostgreSQL server."""

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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from coire_api.db import ModelRow
from coire_core.models.registry import ModelKind


@pytest.mark.integration
def test_registry_kind_upgrade_and_guarded_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
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
    database = "coire_image_kind_" + uuid.uuid4().hex[:12]
    test_dsn = urlunparse(parsed._replace(path="/" + database))
    text_id, vision_id, image_id = (uuid.uuid4() for _ in range(3))

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

    async def seed_old_rows() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            for model_id, repo, backend in (
                (text_id, "test/text-kind", "mlx_lm"),
                (vision_id, "test/vision-kind", "mlx_vlm"),
            ):
                await connection.execute(
                    """INSERT INTO models
                       (id, repo_id, slug, display_name, precision, weight_bytes,
                        total_bytes, file_count, memory_estimate_bytes, backend)
                       VALUES ($1, $2, $2, $2, '4bit', 1, 1, 1, 1, $3)""",
                    model_id,
                    repo,
                    backend,
                )
        finally:
            await connection.close()

    async def check_upgrade() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert (
                await connection.fetchval("SELECT kind FROM models WHERE id = $1", text_id)
                == "language_model"
            )
            assert (
                await connection.fetchval("SELECT kind FROM models WHERE id = $1", vision_id)
                == "language_model"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "UPDATE models SET kind = 'image_model' WHERE id = $1", text_id
                )
            await connection.execute(
                """INSERT INTO models
                   (id, repo_id, slug, display_name, precision, weight_bytes,
                    total_bytes, file_count, memory_estimate_bytes, backend, kind)
                   VALUES ($1, 'test/image-kind', 'test/image-kind', 'Image', '4bit',
                           1, 1, 1, 1, 'mflux', 'image_model')""",
                image_id,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "UPDATE models SET state = 'ready' WHERE id = $1", image_id
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "UPDATE models SET image_capability_profile = '{}'::jsonb WHERE id = $1",
                    text_id,
                )
            await connection.execute(
                "UPDATE models SET image_capability_profile = '{}'::jsonb, "
                "state = 'ready' WHERE id = $1",
                image_id,
            )
        finally:
            await connection.close()

        engine = create_async_engine(test_dsn.replace("postgresql:", "postgresql+asyncpg:", 1))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        classifier_id = uuid.uuid4()
        try:
            async with sessions.begin() as session:
                session.add(
                    ModelRow(
                        id=classifier_id,
                        repo_id="test/image-classifier-kind",
                        slug="test--image-classifier-kind",
                        display_name="Classifier",
                        precision="safetensors",
                        weight_bytes=1,
                        total_bytes=1,
                        file_count=1,
                        memory_estimate_bytes=1,
                        kind=ModelKind.IMAGE_CLASSIFIER,
                        backend="auxiliary",
                        source="studio",
                        image_capability_profile=None,
                    )
                )
            connection = await asyncpg.connect(test_dsn)
            try:
                assert await connection.fetchval(
                    "SELECT image_capability_profile IS NULL FROM models WHERE id = $1",
                    classifier_id,
                )
                await connection.execute("DELETE FROM models WHERE id = $1", classifier_id)
            finally:
                await connection.close()
        finally:
            await engine.dispose()

    async def clear_profile() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                "0030_image_output_retention"
            )
            await connection.execute(
                "UPDATE models SET state = 'downloading', "
                "image_capability_profile = NULL WHERE id = $1",
                image_id,
            )
        finally:
            await connection.close()

    async def remove_image() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            await connection.execute("DELETE FROM models WHERE id = $1", image_id)
        finally:
            await connection.close()

    async def check_downgrade() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                "0025_image_capacity"
            )
            assert (
                await connection.fetchval("SELECT backend FROM models WHERE id = $1", vision_id)
                == "mlx_vlm"
            )
            assert not await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'models' AND column_name = 'kind')"
            )
        finally:
            await connection.close()

    async def set_acquisition_provenance(present: bool) -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            await connection.execute(
                "UPDATE models SET source_revision = $1, license_id = $2 WHERE id = $3",
                "a" * 40 if present else None,
                "apache-2.0" if present else None,
                image_id,
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
        command.upgrade(config, "0025_image_capacity")
        asyncio.run(seed_old_rows())
        command.upgrade(config, "head")
        asyncio.run(check_upgrade())
        asyncio.run(set_acquisition_provenance(True))
        with pytest.raises(RuntimeError, match="image provenance"):
            command.downgrade(config, "0025_image_capacity")
        asyncio.run(set_acquisition_provenance(False))
        with pytest.raises(RuntimeError, match="image capability"):
            command.downgrade(config, "0025_image_capacity")
        asyncio.run(clear_profile())
        with pytest.raises(RuntimeError, match="image assets"):
            command.downgrade(config, "0025_image_capacity")
        asyncio.run(remove_image())
        command.downgrade(config, "0025_image_capacity")
        asyncio.run(check_downgrade())
    finally:
        asyncio.run(drop_database())
