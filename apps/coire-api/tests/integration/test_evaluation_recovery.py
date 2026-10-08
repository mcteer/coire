"""Committed restart, deadline and unknown-stop behavior on disposable Postgres."""

import uuid
from pathlib import Path

import pytest
from evaluation_fixtures import seed_evaluation
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    ApiKeyRow,
    Base,
    EvaluationAttemptRow,
    EvaluationResultRow,
    EvaluationRunRow,
    MemoryReservationRow,
    NodeRow,
    UserRow,
)
from coire_core.models.auth import UserRole
from coire_core.models.evaluation import EvaluationWorkload
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.settings import Settings
from coire_scheduler.evaluations import advance

pytestmark = pytest.mark.integration
FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


@pytest.mark.parametrize("revocation", ["demoted", "disabled", "key_revoked", "key_rotated"])
async def test_live_owner_revocation_finishes_queue_without_launch_after_restart(
    training_postgres_url: str,
    tmp_path: Path,
    revocation: str,
) -> None:
    from datetime import UTC, datetime

    engine = create_async_engine(training_postgres_url)
    settings = Settings(evaluations_enabled=True, training_dataset_dir=str(tmp_path))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, identity)
            assert run is not None
            owner = await session.get(UserRow, run.owner_user_id)
            assert owner is not None
            if revocation == "demoted":
                owner.role = UserRole.USER
            elif revocation == "disabled":
                owner.active = False
            else:
                key = ApiKeyRow(
                    id=uuid.uuid4(),
                    user_id=owner.id,
                    name="synthetic",
                    prefix="fixture",
                    secret_hash="inert-fixture-not-a-credential",
                    scopes=["admin"],
                    credential_version=2 if revocation == "key_rotated" else 1,
                    revoked_at=datetime.now(UTC) if revocation == "key_revoked" else None,
                    requests_per_minute=100,
                    monthly_budget_tokens=10000,
                )
                session.add(key)
                run.authorization_snapshot = Principal(
                    kind=PrincipalKind.API_KEY,
                    user_id=owner.id,
                    role=UserRole.ADMIN,
                    scopes=frozenset({"admin"}),
                    api_key_id=key.id,
                    credential_version=1,
                ).model_dump(mode="json")
            await session.commit()
        async with AsyncSession(engine, expire_on_commit=False) as recovered:
            assert await advance(recovered, identity, settings)
            await recovered.commit()
        async with AsyncSession(engine, expire_on_commit=False) as recovered:
            assert await advance(recovered, identity, settings)
            result = await recovered.scalar(select(EvaluationResultRow))
            assert result is not None and result.result["reason"] == "authorization_revoked"
            assert result.result["aggregates"] == [None]
            assert (await recovered.scalars(select(EvaluationAttemptRow))).all() == []
            assert (await recovered.scalars(select(MemoryReservationRow))).all() == []
    finally:
        await engine.dispose()


async def test_disabled_admission_still_expires_queue_once_after_restart(
    training_postgres_url: str, tmp_path: Path
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        settings = Settings(evaluations_enabled=False, training_dataset_dir=str(tmp_path))
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session, expired=True)
            assert await advance(session, identity, settings)
            await session.commit()
        async with AsyncSession(engine, expire_on_commit=False) as recovered:
            assert await advance(recovered, identity, settings)
            results = (await recovered.scalars(select(EvaluationResultRow))).all()
            assert len(results) == 1
            assert results[0].result["outcome"] == "timed_out"
            assert results[0].result["reason"] == "capacity_timeout"
            assert results[0].result["aggregates"] == [None]
    finally:
        await engine.dispose()


async def test_unknown_stop_retains_reservation_and_cleanup_across_restart(
    training_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    import coire_scheduler.evaluations as module
    from coire_api.evaluation.evidence import EvidenceStore, reserve_quota
    from coire_api.nodes_client import NodeError, NodeErrorKind

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        settings = Settings(evaluations_enabled=False, training_dataset_dir=str(tmp_path))
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            node = NodeRow(
                id=uuid.uuid4(),
                name="coire-edge-a",
                role=NodeRole.STUDIO,
                reachability=Reachability.UNREACHABLE,
                memory_total_bytes=128 * 1024**3,
                disk_total_bytes=1024**4,
                gpu_cores=60,
                agent_version="test",
            )
            session.add(node)
            await session.flush()
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes()).model_copy(
                update={"evaluation_id": identity}
            )
            hold = MemoryReservationRow(
                id=uuid.uuid4(),
                node_id=node.id,
                holder_type=ReservationHolder.RUN,
                holder_id=str(work.attempt_id),
                bytes=1024,
                pinned=True,
                state=MemoryReservationState.HELD,
            )
            session.add(hold)
            await session.flush()
            session.add(
                EvaluationAttemptRow(
                    id=work.attempt_id,
                    run_id=identity,
                    phase="base",
                    ordinal=1,
                    fence=1,
                    node_id=node.id,
                    sandbox_reservation_id=hold.id,
                    workload=work.model_dump(mode="json"),
                    target_sha256="a" * 64,
                    state="reserving",
                    deadline_at=work.deadline,
                )
            )
            run = await session.get(EvaluationRunRow, identity)
            assert run is not None
            run.state, run.safe_failure_code, run.cleanup_state = (
                "cancelling",
                "cancelled",
                "pending",
            )
            await reserve_quota(session, run, settings)
            store = EvidenceStore(settings)
            orphan_id = uuid.uuid5(work.attempt_id, "evaluation-evidence-v1")
            foreign_id = uuid.uuid4()
            data = b"synthetic unpublished evidence"
            digest = hashlib.sha256(data).hexdigest()
            await store.stage(orphan_id, data, digest)
            await store.stage(foreign_id, data, digest)
            await session.commit()
            hold_id = hold.id

        class UnreachableClient:
            def __init__(self, settings: Settings) -> None:
                pass

            async def __aenter__(self) -> "UnreachableClient":
                return self

            async def __aexit__(self, *args: object) -> None:
                pass

            async def remove_run(self, *args: object, **kwargs: object) -> None:
                raise NodeError(NodeErrorKind.UNREACHABLE, "coire-edge-a", status=503)

        monkeypatch.setattr(module, "NodeClient", UnreachableClient)
        for _ in range(2):
            async with AsyncSession(engine, expire_on_commit=False) as recovered:
                assert not await advance(recovered, identity, settings)
                await recovered.commit()
                reservation = await recovered.get(MemoryReservationRow, hold_id)
                assert reservation is not None and reservation.state is MemoryReservationState.HELD
                run = await recovered.get(EvaluationRunRow, identity)
                assert (
                    run is not None and run.cleanup_state == "pending" and run.state == "cancelled"
                )
                assert run.evidence_reserved_bytes == 8 * 1024**2
                assert (store.root / str(orphan_id)).exists()

        class RecoveredClient(UnreachableClient):
            async def remove_run(self, *args: object, **kwargs: object) -> None:
                pass

            async def cleanup_evaluation_workspace(self, *args: object, **kwargs: object) -> None:
                pass

        monkeypatch.setattr(module, "NodeClient", RecoveredClient)
        async with AsyncSession(engine, expire_on_commit=False) as recovered:
            assert await advance(recovered, identity, settings)
            await recovered.commit()
            reservation = await recovered.get(MemoryReservationRow, hold_id)
            assert reservation is not None and reservation.state is MemoryReservationState.RELEASED
            run = await recovered.get(EvaluationRunRow, identity)
            assert run is not None and run.cleanup_state == "complete"
            assert run.evidence_reserved_bytes == 0
            assert not (store.root / str(orphan_id)).exists()
            assert (store.root / str(foreign_id)).exists()
            assert await advance(recovered, identity, settings)
            assert len((await recovered.scalars(select(EvaluationResultRow))).all()) == 1
    finally:
        await engine.dispose()


async def test_started_execution_deadline_expires_once_without_extending_on_restart(
    training_postgres_url: str, tmp_path: Path
) -> None:
    from datetime import UTC, datetime, timedelta

    engine = create_async_engine(training_postgres_url)
    settings = Settings(evaluations_enabled=False, training_dataset_dir=str(tmp_path))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        deadline = datetime.now(UTC) - timedelta(seconds=1)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            run = await session.get(EvaluationRunRow, identity)
            assert run is not None
            run.state = "running"
            run.started_at = deadline - timedelta(seconds=60)
            run.started_deadline_at = deadline
            await session.commit()
        for _ in range(2):
            async with AsyncSession(engine, expire_on_commit=False) as recovered:
                assert await advance(recovered, identity, settings)
                await recovered.commit()
                run = await recovered.get(EvaluationRunRow, identity)
                assert run is not None and run.started_deadline_at == deadline
                results = list((await recovered.scalars(select(EvaluationResultRow))).all())
                assert len(results) == 1
                assert results[0].result["reason"] == "execution_timeout"
                assert results[0].result["outcome"] == "timed_out"
                assert results[0].result["aggregates"] == [None]
    finally:
        await engine.dispose()
