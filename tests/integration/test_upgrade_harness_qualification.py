"""Real retained scorecards grant writes only on the evaluated current Studio runtime."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from coire_api import evaluations
from coire_api.db import Base, ModelRow, ModelVariantRow, NodeRow
from coire_core.models.acquisition import VariantState
from coire_core.models.harness import CategoryScores, EvaluationVerdict, HarnessEvaluationSubmission
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.registry import ModelState
from coire_core.settings import Settings


async def test_current_harness_gate_preserves_historical_evidence(
    training_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coire_core.settings

    settings = Settings(upgrades_enabled=True, run_agent_image="coire-agent@sha256:" + "f" * 64)
    monkeypatch.setattr(coire_core.settings, "get_settings", lambda: settings)
    schema = f"upgrade_harness_{uuid4().hex}"
    owner = create_async_engine(training_postgres_url)
    async with owner.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    model_id, variant_id, node_id, other_node_id = [uuid4() for _ in range(4)]
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add(
                ModelRow(
                    id=model_id,
                    repo_id="fixture/tiny",
                    slug="tiny",
                    display_name="Tiny",
                    state=ModelState.READY,
                    precision="4bit",
                    weight_bytes=1024,
                    total_bytes=1024,
                    file_count=1,
                    memory_estimate_bytes=1024,
                )
            )
            await session.flush()
            session.add(
                ModelVariantRow(
                    id=variant_id,
                    model_id=model_id,
                    name="tiny",
                    slug="tiny",
                    source_revision="fixture",
                    precision="4bit",
                    state=VariantState.READY,
                    validated=True,
                    harness_verified=False,
                    is_default=True,
                    byte_size=1024,
                )
            )
            for identity, name, runtime in (
                (node_id, "coire-edge-a", "a" * 64),
                (other_node_id, "coire-edge-b", "b" * 64),
            ):
                session.add(
                    NodeRow(
                        id=identity,
                        name=name,
                        role=NodeRole.STUDIO,
                        reachability=Reachability.HEALTHY,
                        memory_total_bytes=8 * 1024**3,
                        disk_total_bytes=10 * 1024**3,
                        agent_version="same-version",
                        runtime_sha256=runtime,
                        health_observed_at=now,
                    )
                )
            await session.flush()
            submission = HarnessEvaluationSubmission(
                variant_id=variant_id,
                scores=CategoryScores(
                    tool_calling=1, structured_output=1, edit_application=1, long_context=1
                ),
                verdict=EvaluationVerdict.PASSED,
                harness_version="same-version",
                engine_version="same-version",
                runtime_sha256="a" * 64,
                harness_image=settings.run_agent_image,
            )
            report = await evaluations.record(session, submission)
            assert report.runtime_sha256 == "a" * 64
            assert await evaluations.variant_is_write_verified(session, variant_id)
            settings.run_agent_image = "coire-agent@sha256:" + "e" * 64
            assert not await evaluations.variant_is_write_verified(session, variant_id)
            settings.run_agent_image = "coire-agent@sha256:" + "f" * 64
            assert await evaluations.variant_is_write_verified(session, variant_id, node_id=node_id)
            assert not await evaluations.variant_is_write_verified(
                session, variant_id, node_id=other_node_id
            )
            # Infrastructure errors do not revoke a previously measured approval.
            await evaluations.record(
                session,
                submission.model_copy(update={"verdict": EvaluationVerdict.INFRASTRUCTURE_ERROR}),
            )
            assert await evaluations.variant_is_write_verified(session, variant_id, node_id=node_id)
            node = await session.get(NodeRow, node_id)
            assert node is not None
            node.runtime_sha256 = "b" * 64
            await session.flush()
            assert not await evaluations.variant_is_write_verified(session, variant_id)
            historical = await evaluations.get(session, report.id)
            assert historical == report
            node.runtime_sha256 = "a" * 64
            node.health_observed_at = now - timedelta(seconds=31)
            await session.flush()
            assert not await evaluations.variant_is_write_verified(
                session, variant_id, node_id=node_id
            )
            node.health_observed_at = datetime.now(UTC)
            await session.flush()
            assert await evaluations.variant_is_write_verified(session, variant_id, node_id=node_id)
            await evaluations.record(
                session, submission.model_copy(update={"verdict": EvaluationVerdict.FAILED})
            )
            assert not await evaluations.variant_is_write_verified(
                session, variant_id, node_id=node_id
            )
            assert await evaluations.get(session, report.id) == report
    finally:
        await engine.dispose()
        async with owner.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await owner.dispose()
