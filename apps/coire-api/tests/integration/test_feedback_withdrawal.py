"""Owner generation fences invalidate writers and replay immediately."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import Base, ComparisonPairRow, UserRow
from coire_api.feedback.eligibility import lock_preference, require_generation
from coire_api.feedback.service import change_preference
from coire_core.errors import FeedbackForbidden
from coire_core.models.auth import UserRole
from coire_core.models.feedback import FeedbackPreferenceUpdate

pytestmark = pytest.mark.integration


async def test_opt_out_and_reenable_never_resurrect_old_generation(
    training_postgres_url: str,
) -> None:
    engine = create_async_engine(training_postgres_url)
    owner = uuid.uuid4()
    principal = Principal(kind=PrincipalKind.USER, user_id=owner)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@test.local",
                    display_name="Owner",
                    role=UserRole.USER,
                    active=True,
                )
            )
            await session.commit()
            preference = await lock_preference(session, owner)
            old_generation = preference.capture_generation
            pair = ComparisonPairRow(
                id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
                owner_user_id=owner,
                conversation_id=uuid.uuid4(),
                source_turn_id=uuid.uuid4(),
                source_message_id=uuid.uuid4(),
                capture_generation=old_generation,
                context_revision=1,
                client_request_id=uuid.uuid4(),
                request_sha256="a" * 64,
                expires_at=datetime.now(UTC),
                original="secret original",
                candidate="secret candidate",
            )
            session.add(pair)
            await session.commit()
            result = await change_preference(
                session,
                principal,
                FeedbackPreferenceUpdate(
                    client_request_id=uuid.uuid4(),
                    expected_version=1,
                    enabled=False,
                    disclosure_version="feedback-v1",
                ),
            )
            assert not result.enabled and result.capture_generation > old_generation
            with pytest.raises(FeedbackForbidden):
                await require_generation(session, owner, old_generation)
            await session.refresh(pair)
            assert pair.selection_state == "withdrawn" and pair.purge_after is not None
            # Logical exclusion precedes bounded physical purge.
            assert pair.original == "secret original"
            await change_preference(
                session,
                principal,
                FeedbackPreferenceUpdate(
                    client_request_id=uuid.uuid4(),
                    expected_version=result.version,
                    enabled=True,
                    disclosure_version="feedback-v1",
                ),
            )
            with pytest.raises(FeedbackForbidden):
                await require_generation(session, owner, old_generation)
    finally:
        await engine.dispose()


async def test_writer_lock_serializes_opt_out_and_purge_erases_only_source_copies(
    training_postgres_url: str,
) -> None:
    import asyncio

    from coire_api.feedback.retention import purge_sources

    engine = create_async_engine(training_postgres_url)
    owner = uuid.uuid4()
    principal = Principal(kind=PrincipalKind.USER, user_id=owner)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as seed:
            seed.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@test.local",
                    display_name="Owner",
                    role=UserRole.USER,
                    active=True,
                )
            )
            await seed.commit()
            preference = await lock_preference(seed, owner)
            generation = preference.capture_generation
            await seed.commit()
        async with AsyncSession(engine, expire_on_commit=False) as writer:
            await require_generation(writer, owner, generation)
            started = asyncio.Event()

            async def disable() -> None:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    started.set()
                    await change_preference(
                        session,
                        principal,
                        FeedbackPreferenceUpdate(
                            client_request_id=uuid.uuid4(),
                            expected_version=1,
                            enabled=False,
                            disclosure_version="feedback-v1",
                        ),
                    )
                    await session.commit()

            task = asyncio.create_task(disable())
            await started.wait()
            writer.add(
                ComparisonPairRow(
                    id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
                    owner_user_id=owner,
                    conversation_id=uuid.uuid4(),
                    source_turn_id=uuid.uuid4(),
                    source_message_id=uuid.uuid4(),
                    capture_generation=generation,
                    context_revision=1,
                    client_request_id=uuid.uuid4(),
                    request_sha256="b" * 64,
                    expires_at=datetime.now(UTC),
                    original="private",
                    candidate="alternative",
                    counted_bytes=18,
                )
            )
            await writer.commit()
            await asyncio.wait_for(task, timeout=5)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            with pytest.raises(FeedbackForbidden):
                await require_generation(session, owner, generation)
            await session.rollback()
            assert await purge_sources(session) >= 1
            await session.commit()
            pair = await session.get(ComparisonPairRow, "01ARZ3NDEKTSV4RRFFQ69G5FAX")
            assert pair is not None and pair.original is None and pair.candidate is None
            assert pair.counted_bytes == 0 and pair.purged_at is not None
    finally:
        await engine.dispose()


async def test_inactive_user_and_revoked_user_key_cannot_mutate_settings(
    training_postgres_url: str,
) -> None:
    from coire_api.feedback.eligibility import authorize_owner

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
                    display_name="Disabled",
                    role=UserRole.USER,
                    active=False,
                )
            )
            await session.commit()
            with pytest.raises(FeedbackForbidden):
                await authorize_owner(session, Principal(kind=PrincipalKind.USER, user_id=owner))
            await session.rollback()
            user = await session.get(UserRow, owner)
            assert user is not None
            user.active = True
            await session.commit()
            with pytest.raises(FeedbackForbidden):
                await authorize_owner(
                    session,
                    Principal(
                        kind=PrincipalKind.API_KEY,
                        user_id=owner,
                        api_key_id=uuid.uuid4(),
                        credential_version=1,
                        scopes=frozenset({"chat"}),
                    ),
                )
    finally:
        await engine.dispose()


async def test_setting_replay_is_content_free_and_projects_current_state(
    training_postgres_url: str,
) -> None:
    from sqlalchemy import select

    from coire_api.db import FeedbackMutationRow
    from coire_core.errors import FeedbackConflict

    engine = create_async_engine(training_postgres_url)
    owner = uuid.uuid4()
    principal = Principal(kind=PrincipalKind.USER, user_id=owner)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@test.local",
                    display_name="Owner",
                    role=UserRole.USER,
                    active=True,
                )
            )
            await session.commit()
            command = FeedbackPreferenceUpdate(
                client_request_id=uuid.uuid4(),
                expected_version=1,
                enabled=False,
                disclosure_version="feedback-v1",
            )
            first = await change_preference(session, principal, command)
            await session.commit()
            assert await change_preference(session, principal, command) == first
            await session.commit()
            current = await change_preference(
                session,
                principal,
                FeedbackPreferenceUpdate(
                    client_request_id=uuid.uuid4(),
                    expected_version=2,
                    enabled=True,
                    disclosure_version="feedback-v1",
                ),
            )
            await session.commit()
            assert await change_preference(session, principal, command) == current
            await session.commit()
            with pytest.raises(FeedbackConflict):
                await change_preference(
                    session, principal, command.model_copy(update={"enabled": True})
                )
            await session.rollback()
            receipts = (
                await session.scalars(
                    select(FeedbackMutationRow).where(FeedbackMutationRow.actor_user_id == owner)
                )
            ).all()
            assert len(receipts) == 2
            assert all(set(receipt.receipt) == {"owner_id", "version"} for receipt in receipts)
    finally:
        await engine.dispose()


async def test_expiry_and_purge_clear_unexported_labels_but_keep_receipts(
    training_postgres_url: str,
) -> None:
    from coire_api.db import FeedbackRow
    from coire_api.feedback.retention import expire_sources, purge_sources

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
                    role=UserRole.USER,
                    active=False,
                )
            )
            await session.commit()
            conversation_id = uuid.uuid4()
            now = datetime.now(UTC)
            pair = ComparisonPairRow(
                id="01ARZ3NDEKTSV4RRFFQ69G5FB2",
                owner_user_id=owner,
                conversation_id=conversation_id,
                source_turn_id=uuid.uuid4(),
                source_message_id=uuid.uuid4(),
                capture_generation=1,
                context_revision=1,
                client_request_id=uuid.uuid4(),
                request_sha256="a" * 64,
                expires_at=now,
                original="original",
                candidate="candidate",
                generation_state="ready",
                selection_state="pending",
            )
            session.add(pair)
            await session.flush()
            judgement = FeedbackRow(
                id=uuid.uuid4(),
                owner_user_id=owner,
                actor_user_id=owner,
                conversation_id=conversation_id,
                pair_id=pair.id,
                kind="pair",
                source="owner",
                judgement="candidate",
                tags=["private-label"],
                capture_generation=1,
            )
            session.add(judgement)
            await session.commit()
            assert await expire_sources(session, batch_size=100) >= 1
            await purge_sources(session, batch_size=100)
            await session.commit()
            await session.refresh(pair)
            await session.refresh(judgement)
            assert pair.selection_state == "expired" and pair.original is None
            assert (
                judgement.judgement is None
                and judgement.tags == []
                and judgement.purged_at is not None
            )
            assert judgement.version == 1
    finally:
        await engine.dispose()


async def test_lost_api_generation_is_reaped_without_enabled_admissions(
    training_postgres_url: str,
) -> None:
    from datetime import timedelta

    from coire_api.feedback.retention import expire_sources, purge_sources
    from coire_api.training.service import training_id

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
                    display_name="Inactive",
                    role=UserRole.USER,
                    active=False,
                )
            )
            await session.flush()
            now = datetime.now(UTC)
            pair = ComparisonPairRow(
                id=training_id(),
                owner_user_id=owner,
                conversation_id=uuid.uuid4(),
                source_turn_id=uuid.uuid4(),
                source_message_id=uuid.uuid4(),
                capture_generation=1,
                context_revision=1,
                client_request_id=uuid.uuid4(),
                request_sha256="a" * 64,
                expires_at=now + timedelta(hours=1),
                generation_state="running",
                selection_state="pending",
                original="private original",
                candidate="private partial",
                counted_bytes=31,
                execution={
                    "owner_process": "dead-api",
                    "lease_expires_at": (now - timedelta(seconds=1)).isoformat(),
                },
            )
            session.add(pair)
            await session.commit()
            assert await expire_sources(session) >= 1
            await purge_sources(session)
            await session.commit()
            await session.refresh(pair)
            assert pair.generation_state == "failed" and pair.selection_state == "dismissed"
            assert pair.original is None and pair.candidate is None and pair.counted_bytes == 0
    finally:
        await engine.dispose()
