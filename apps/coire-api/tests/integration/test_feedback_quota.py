"""Withdrawn copies and unremoved stages remain counted until erasure commits."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import (
    Base,
    ComparisonPairRow,
    PreferenceExportRow,
    TrainingStorageReservationRow,
    UserRow,
)
from coire_api.feedback.quota import feedback_counted_bytes, require_feedback_capacity
from coire_api.feedback.retention import purge_sources
from coire_core.errors import FeedbackQuotaExceeded
from coire_core.models.auth import UserRole
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


async def test_quota_counts_database_and_private_stage_until_cleanup(
    training_postgres_url: str,
) -> None:
    engine = create_async_engine(training_postgres_url)
    owner = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@test.local",
                    display_name="Owner",
                    role=UserRole.ADMIN,
                    active=True,
                )
            )
            await session.commit()
            baseline = await feedback_counted_bytes(session)
            now = datetime.now(UTC)
            pair = ComparisonPairRow(
                id="01ARZ3NDEKTSV4RRFFQ69G5FB0",
                owner_user_id=owner,
                conversation_id=uuid.uuid4(),
                source_turn_id=uuid.uuid4(),
                source_message_id=uuid.uuid4(),
                capture_generation=1,
                context_revision=1,
                client_request_id=uuid.uuid4(),
                request_sha256="a" * 64,
                expires_at=now + timedelta(hours=1),
                original="private",
                candidate="alternative",
                counted_bytes=128,
                withdrawn_at=now,
                purge_after=now + timedelta(hours=24),
                generation_state="withdrawn",
                selection_state="withdrawn",
            )
            hold = TrainingStorageReservationRow(
                id=uuid.uuid4(),
                owner_user_id=owner,
                subject_id=str(uuid.uuid4()),
                bytes=256,
                state="releasing",
            )
            hold_id = hold.id
            session.add_all([pair, hold])
            session.add(
                PreferenceExportRow(
                    id="01ARZ3NDEKTSV4RRFFQ69G5FB1",
                    owner_user_id=owner,
                    authorization_snapshot={},
                    request={},
                    staging={"hold_id": str(hold.id)},
                    queue_deadline_at=now + timedelta(hours=1),
                    cleanup_pending=True,
                )
            )
            await session.commit()
            assert await feedback_counted_bytes(session) == baseline + 384
            settings = Settings(
                _secrets_dir="/nonexistent", feedback_storage_quota_bytes=baseline + 384
            )  # type: ignore[call-arg]
            with pytest.raises(FeedbackQuotaExceeded):
                await require_feedback_capacity(session, 1, settings)
            await session.rollback()
            await purge_sources(session)
            await session.commit()
            assert await feedback_counted_bytes(session) == baseline + 256
            remaining = await session.get(TrainingStorageReservationRow, hold_id)
            assert remaining is not None
            remaining.state = "released"
            remaining.released_at = now
            await session.commit()
            assert await feedback_counted_bytes(session) == baseline
    finally:
        await engine.dispose()
