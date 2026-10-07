"""Real Jobs history keeps uncertain reservations and excludes rolled-back losses/content."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_training_runtime_postgres import RuntimeDatabase
from test_training_runtime_postgres import runtime_db as runtime_db
from training_measurement_fixtures import ATTEMPT, JOB

from coire_api.auth import Principal, PrincipalKind
from coire_api.console.training import project_training_activity
from coire_api.db import MemoryReservationRow, TrainingJobRow, TrainingMetricRow
from coire_core.errors import TrainingForbidden, TrainingValidationError
from coire_core.models.auth import UserRole
from coire_core.models.placement import MemoryReservationState

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1", reason="requires disposable Postgres"
    ),
]


async def test_activity_has_loss_memory_and_stop_without_recipe_content(
    runtime_db: RuntimeDatabase,
) -> None:
    factory, _ = runtime_db
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        actor = Principal(kind=PrincipalKind.ADMIN, user_id=job.owner_user_id, role=UserRole.ADMIN)
        job.state, job.completed_update, job.safe_reason = "recovering", 1, "node_unreachable"
        job.source_yaml = "private recipe comment"
        hold = await session.scalar(select(MemoryReservationRow))
        assert hold is not None
        hold.state = MemoryReservationState.RELEASING
        expected_memory = hold.bytes
        now = datetime.now(UTC)
        for step, loss, rolled_back in [(1, 0.5, False), (2, 9.0, True)]:
            session.add(
                TrainingMetricRow(
                    job_id=JOB,
                    attempt_id=ATTEMPT,
                    completed_update=step,
                    kind="train",
                    loss=loss,
                    metric={},
                    rolled_back=rolled_back,
                    recorded_at=now + timedelta(seconds=step),
                )
            )
        await session.flush()
        page = await project_training_activity(session, actor)
        assert len(page.items) == 1
        row = page.items[0]
        assert row.job_id == JOB and row.state == "recovering" and row.can_stop
        assert row.reserved_bytes == expected_memory and row.latest_train_loss == 0.5
        assert row.latest_validation_loss is None and row.version == job.version
        assert "private recipe comment" not in page.model_dump_json()
        assert "source_yaml" not in page.model_dump_json()
        job.state = "cancelling"
        await session.flush()
        assert not (await project_training_activity(session, actor)).items[0].can_stop
        hold.state = MemoryReservationState.RELEASED
        await session.flush()
        assert (await project_training_activity(session, actor)).items[0].reserved_bytes == 0


async def test_activity_denies_service_authority_and_invalid_cursor(
    runtime_db: RuntimeDatabase,
) -> None:
    factory, _ = runtime_db
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        service = Principal(
            kind=PrincipalKind.SERVICE, role=UserRole.ADMIN, scopes=frozenset({"admin"})
        )
        with pytest.raises(TrainingForbidden):
            await project_training_activity(session, service)
        actor = Principal(kind=PrincipalKind.ADMIN, user_id=job.owner_user_id, role=UserRole.ADMIN)
        with pytest.raises(TrainingValidationError):
            await project_training_activity(session, actor, cursor="not-a-valid-cursor")


async def test_activity_cursor_orders_tied_ulids_and_excludes_retired_jobs(
    runtime_db: RuntimeDatabase,
) -> None:
    factory, _ = runtime_db
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        actor = Principal(kind=PrincipalKind.ADMIN, user_id=job.owner_user_id, role=UserRole.ADMIN)
        fields = {
            column.name: getattr(job, column.name) for column in TrainingJobRow.__table__.columns
        }
        older_id, newer_id = JOB[:-1] + "W", JOB[:-1] + "X"
        for identity in (older_id, newer_id):
            session.add(
                TrainingJobRow(
                    **{
                        **fields,
                        "id": identity,
                        "idempotency_key": identity,
                        "output_slug": identity.lower(),
                    }
                )
            )
        await session.flush()
        first = await project_training_activity(session, actor, limit=1)
        assert first.items[0].job_id == newer_id and first.next_cursor is not None
        second = await project_training_activity(session, actor, limit=1, cursor=first.next_cursor)
        assert second.items[0].job_id == older_id and second.next_cursor is not None
        job.deleted_at = datetime.now(UTC)
        await session.flush()
        assert not (
            await project_training_activity(session, actor, cursor=second.next_cursor)
        ).items
