"""Bounded lease recovery for plain-chat turns after an API process disappears."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy import and_, delete, func, or_, select, update

from coire_api.chat.processing import publish_processed_files
from coire_api.chat.telemetry import active_turns, purge_oldest_seconds, requests_total, tracer
from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatEventRow,
    ChatFileProcessingRow,
    ChatMessageRow,
    ChatQuotaReservationRow,
    ChatTurnRow,
    UserRow,
    session_scope,
)
from coire_core.models.chat import ChatEvent, ChatTurnTerminal
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
PROCESS_ID = uuid.uuid4().hex
LEASE_SECONDS = 30
SWEEP_SECONDS = 5
ACTIVE_STATES = ("accepted", "queued", "loading", "running", "stop_requested")
TEXT_PURGE_DELAY = timedelta(minutes=5)
STAGING_PURGE_DELAY = timedelta(hours=1)


async def renew_turn_lease(turn_id: uuid.UUID) -> bool:
    """Renew only this process's still-active turn; never revive a terminal turn."""
    async with session_scope() as session:
        result = await session.execute(
            update(ChatTurnRow)
            .where(
                ChatTurnRow.id == turn_id,
                ChatTurnRow.owner_process == PROCESS_ID,
                ChatTurnRow.state.in_(ACTIVE_STATES),
            )
            .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS))
            .returning(ChatTurnRow.id)
        )
        return result.scalar_one_or_none() is not None


async def maintain_turn_lease(turn_id: uuid.UUID) -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            if not await renew_turn_lease(turn_id):
                return
        except Exception as exc:
            logger.error(
                "chat lease renewal failed turn_id=%s error_type=%s", turn_id, type(exc).__name__
            )


async def sweep_stale_turns(settings: Settings) -> int:
    """Commit one terminal event per expired turn, preserving saved partial output."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=LEASE_SECONDS)
    expired = or_(
        ChatTurnRow.lease_expires_at <= now,
        and_(ChatTurnRow.lease_expires_at.is_(None), ChatTurnRow.updated_at <= cutoff),
    )
    async with session_scope() as session:
        candidates = (
            await session.execute(
                select(ChatTurnRow.id, ChatTurnRow.conversation_id)
                .where(ChatTurnRow.action == "chat", ChatTurnRow.state.in_(ACTIVE_STATES), expired)
                .order_by(ChatTurnRow.updated_at, ChatTurnRow.id)
                .limit(100)
            )
        ).all()
    recovered = 0
    for turn_id, conversation_id in candidates:
        with tracer.start_as_current_span("coire.api.chat.reconcile"):
            async with session_scope() as session:
                conversation = await session.scalar(
                    select(ChatConversationRow)
                    .where(ChatConversationRow.id == conversation_id)
                    .with_for_update()
                )
                if conversation is None:
                    continue
                turn = await session.get(ChatTurnRow, turn_id, with_for_update=True)
                if (
                    turn is None
                    or turn.action != "chat"
                    or turn.state not in ACTIVE_STATES
                    or (
                        turn.lease_expires_at is not None
                        and turn.lease_expires_at > datetime.now(UTC)
                    )
                    or (
                        turn.lease_expires_at is None
                        and turn.updated_at > datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS)
                    )
                ):
                    continue
                answer = await session.get(ChatMessageRow, turn.assistant_message_id)
                if answer is None:
                    logger.error("chat recovery missing answer turn_id=%s", turn_id)
                    continue
                state: Literal["stopped", "interrupted"] = (
                    "stopped" if turn.state == "stop_requested" else "interrupted"
                )
                terminal_at = datetime.now(UTC)
                turn.state = state
                turn.failure_code = "chat_lease_expired"
                turn.finished_at = terminal_at
                turn.updated_at = terminal_at
                turn.lease_expires_at = None
                turn.owner_process = None
                if conversation.active_turn_id == turn_id:
                    conversation.active_turn_id = None
                conversation.event_cursor += 1
                conversation.updated_at = terminal_at
                event = ChatEvent(
                    conversation_id=conversation_id,
                    cursor=conversation.event_cursor,
                    turn_id=turn_id,
                    created_at=terminal_at,
                    payload=ChatTurnTerminal(
                        state=state,
                        answer_length=len(answer.text),
                        reasoning_length=len(answer.reasoning),
                        safe_error=(
                            "stopped by user"
                            if state == "stopped"
                            else "connection interrupted; your partial answer was saved"
                        ),
                    ),
                )
                session.add(
                    ChatEventRow(
                        id=uuid.uuid4(),
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        cursor=event.cursor,
                        type=event.payload.type,
                        payload=event.payload.model_dump(mode="json"),
                        created_at=terminal_at,
                        expires_at=terminal_at
                        + timedelta(hours=settings.chat_event_retention_hours),
                    )
                )
            recovered += 1
            requests_total.add(1, {"operation": "reconcile", "outcome": state})
            logger.info("chat stale turn recovered turn_id=%s state=%s", turn_id, state)
    return recovered


async def purge_deleted_text() -> int:
    """Erase expired text-only content; keep the owner tombstone for idempotent DELETE."""
    cutoff = datetime.now(UTC) - TEXT_PURGE_DELAY
    async with session_scope() as session:
        candidates = (
            (
                await session.execute(
                    select(ChatConversationRow.id)
                    .where(
                        ChatConversationRow.deleted_at <= cutoff,
                        ChatConversationRow.purged_at.is_(None),
                    )
                    .order_by(ChatConversationRow.deleted_at, ChatConversationRow.id)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    purged = 0
    for conversation_id in candidates:
        with tracer.start_as_current_span("coire.api.chat.purge"):
            async with session_scope() as session:
                conversation = await session.scalar(
                    select(ChatConversationRow)
                    .where(ChatConversationRow.id == conversation_id)
                    .with_for_update()
                )
                if (
                    conversation is None
                    or conversation.deleted_at is None
                    or conversation.deleted_at > datetime.now(UTC) - TEXT_PURGE_DELAY
                    or conversation.purged_at is not None
                ):
                    continue
                attached = await session.scalar(
                    select(ChatAttachmentRow.id)
                    .where(ChatAttachmentRow.conversation_id == conversation_id)
                    .limit(1)
                )
                if attached is not None:
                    # Original/derived blob deletion is owned by the later file-purge path.
                    continue
                active = await session.scalar(
                    select(ChatTurnRow.id)
                    .where(
                        ChatTurnRow.conversation_id == conversation_id,
                        ChatTurnRow.state.in_(ACTIVE_STATES),
                    )
                    .limit(1)
                )
                if active is not None:
                    continue
                conversation.active_turn_id = None
                await session.flush()
                await session.execute(
                    delete(ChatEventRow).where(ChatEventRow.conversation_id == conversation_id)
                )
                await session.execute(
                    delete(ChatTurnRow).where(ChatTurnRow.conversation_id == conversation_id)
                )
                await session.execute(
                    delete(ChatMessageRow).where(ChatMessageRow.conversation_id == conversation_id)
                )
                await session.execute(
                    delete(ChatQuotaReservationRow).where(
                        ChatQuotaReservationRow.conversation_id == conversation_id
                    )
                )
                conversation.title = "Deleted conversation"
                conversation.selected_model_id = None
                conversation.purged_at = datetime.now(UTC)
            purged += 1
            requests_total.add(1, {"operation": "purge", "outcome": "succeeded"})
            logger.info("chat text content purged conversation_id=%s", conversation_id)
    return purged


async def purge_deleted_files(settings: Settings) -> int:
    """Erase generated originals and file rows only after worker outputs are purged."""

    cutoff = datetime.now(UTC) - TEXT_PURGE_DELAY
    async with session_scope() as session:
        attachment_ids = list(
            (
                await session.execute(
                    select(ChatAttachmentRow.id)
                    .join(
                        ChatConversationRow,
                        ChatConversationRow.id == ChatAttachmentRow.conversation_id,
                    )
                    .where(
                        ChatConversationRow.deleted_at <= cutoff,
                        ~select(ChatFileProcessingRow.id)
                        .where(
                            ChatFileProcessingRow.attachment_id == ChatAttachmentRow.id,
                            ChatFileProcessingRow.state != "purged",
                        )
                        .exists(),
                    )
                    .order_by(ChatAttachmentRow.created_at, ChatAttachmentRow.id)
                    .limit(100)
                )
            ).scalars()
        )
    purged = 0
    for attachment_id in attachment_ids:
        with tracer.start_as_current_span("coire.api.chat.file_purge") as span:
            span.set_attribute("file_id", str(attachment_id))
            async with session_scope() as session:
                snapshot = await session.get(ChatAttachmentRow, attachment_id)
                if snapshot is None:
                    continue
                conversation = await session.get(
                    ChatConversationRow, snapshot.conversation_id, with_for_update=True
                )
                attachment = await session.get(
                    ChatAttachmentRow, attachment_id, with_for_update=True
                )
                if (
                    conversation is None
                    or conversation.deleted_at is None
                    or conversation.deleted_at > datetime.now(UTC) - TEXT_PURGE_DELAY
                    or attachment is None
                ):
                    continue
                jobs = list(
                    (
                        await session.execute(
                            select(ChatFileProcessingRow).where(
                                ChatFileProcessingRow.attachment_id == attachment_id
                            )
                        )
                    ).scalars()
                )
                if any(job.state != "purged" for job in jobs):
                    continue
                if attachment.original_key != str(attachment.id):
                    logger.error("file original key mismatch file_id=%s", attachment.id)
                    continue
                try:
                    (Path(settings.chat_original_root) / attachment.original_key).unlink(
                        missing_ok=True
                    )
                except OSError as exc:
                    requests_total.add(1, {"operation": "file_purge", "outcome": "retry"})
                    logger.error(
                        "file original purge failed file_id=%s error_type=%s",
                        attachment.id,
                        type(exc).__name__,
                    )
                    continue
                await session.execute(
                    delete(ChatQuotaReservationRow).where(
                        ChatQuotaReservationRow.attachment_id == attachment_id
                    )
                )
                await session.execute(
                    delete(ChatFileProcessingRow).where(
                        ChatFileProcessingRow.attachment_id == attachment_id
                    )
                )
                await session.delete(attachment)
            purged += 1
            requests_total.add(1, {"operation": "file_purge", "outcome": "succeeded"})
            logger.info("chat attachment purged file_id=%s", attachment_id)
    return purged


async def reclaim_failed_file_quota() -> int:
    """Release derivative capacity only after the worker confirmed failed-output purge."""

    async with session_scope() as session:
        job_ids = list(
            (
                await session.execute(
                    select(ChatFileProcessingRow.id)
                    .join(
                        ChatQuotaReservationRow,
                        ChatQuotaReservationRow.job_id == ChatFileProcessingRow.id,
                    )
                    .join(
                        ChatAttachmentRow,
                        ChatAttachmentRow.id == ChatQuotaReservationRow.attachment_id,
                    )
                    .join(
                        ChatConversationRow,
                        ChatConversationRow.id == ChatAttachmentRow.conversation_id,
                    )
                    .where(
                        ChatFileProcessingRow.state == "failed",
                        func.coalesce(
                            ChatFileProcessingRow.output_manifest["output_purged"].as_boolean(),
                            False,
                        ).is_(True),
                        ChatQuotaReservationRow.state == "active",
                        ChatQuotaReservationRow.reserved_bytes
                        > ChatAttachmentRow.original_bytes + ChatAttachmentRow.derived_bytes,
                        ChatConversationRow.deleted_at.is_(None),
                    )
                    .order_by(ChatFileProcessingRow.updated_at, ChatFileProcessingRow.id)
                    .limit(100)
                )
            ).scalars()
        )
    reclaimed = 0
    for job_id in job_ids:
        with tracer.start_as_current_span("coire.api.chat.file_quota_reclaim") as span:
            span.set_attribute("job_id", job_id)
            async with session_scope() as session:
                snapshot = await session.get(ChatFileProcessingRow, job_id)
                if snapshot is None or snapshot.owner_user_id is None:
                    continue
                owner = await session.get(UserRow, snapshot.owner_user_id, with_for_update=True)
                attachment = await session.get(ChatAttachmentRow, snapshot.attachment_id)
                if owner is None or attachment is None:
                    continue
                conversation = await session.get(
                    ChatConversationRow, attachment.conversation_id, with_for_update=True
                )
                attachment = await session.get(
                    ChatAttachmentRow, attachment.id, with_for_update=True
                )
                job = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
                if (
                    conversation is None
                    or conversation.deleted_at is not None
                    or job is None
                    or job.state != "failed"
                    or not job.output_manifest
                    or job.output_manifest.get("output_purged") is not True
                    or attachment is None
                    or attachment.owner_user_id != snapshot.owner_user_id
                ):
                    continue
                reservation = await session.scalar(
                    select(ChatQuotaReservationRow)
                    .where(
                        ChatQuotaReservationRow.attachment_id == attachment.id,
                        ChatQuotaReservationRow.job_id == job.id,
                        ChatQuotaReservationRow.state == "active",
                    )
                    .with_for_update()
                )
                target = attachment.original_bytes + attachment.derived_bytes
                if reservation is None or reservation.reserved_bytes <= target:
                    continue
                reservation.reserved_bytes = target
            reclaimed += 1
            requests_total.add(1, {"operation": "file_quota_reclaim", "outcome": "succeeded"})
            logger.info("failed file quota reclaimed job_id=%s", job_id)
    return reclaimed


def purge_stale_uploads(root: Path, *, limit: int = 100) -> int:
    """Remove only aged generated temporary originals left by interrupted uploads."""

    cutoff = (datetime.now(UTC) - STAGING_PURGE_DELAY).timestamp()
    removed = 0
    try:
        entries = os.scandir(root)
    except FileNotFoundError:
        return 0
    with entries:
        for entry in entries:
            if removed >= limit:
                break
            parts = entry.name.split(".")
            if len(parts) != 4 or parts[0] or parts[3] != "uploading":
                continue
            try:
                uuid.UUID(parts[1])
                uuid.UUID(parts[2])
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_mtime <= cutoff:
                    Path(entry.path).unlink()
                    removed += 1
            except (ValueError, OSError):
                continue
    return removed


async def compact_expired_events() -> int:
    """Delete one bounded page of expired SSE events; history snapshots remain intact."""
    async with session_scope() as session:
        identifiers = (
            (
                await session.execute(
                    select(ChatEventRow.id)
                    .where(ChatEventRow.expires_at <= datetime.now(UTC))
                    .order_by(ChatEventRow.expires_at, ChatEventRow.id)
                    .limit(1000)
                )
            )
            .scalars()
            .all()
        )
        if identifiers:
            await session.execute(delete(ChatEventRow).where(ChatEventRow.id.in_(identifiers)))
    if identifiers:
        requests_total.add(len(identifiers), {"operation": "event_compact", "outcome": "deleted"})
    return len(identifiers)


async def record_oldest_pending_purge() -> float:
    async with session_scope() as session:
        oldest = await session.scalar(
            select(func.min(ChatConversationRow.deleted_at)).where(
                ChatConversationRow.deleted_at.is_not(None),
                ChatConversationRow.purged_at.is_(None),
            )
        )
    age = max((datetime.now(UTC) - oldest).total_seconds(), 0.0) if oldest is not None else 0.0
    purge_oldest_seconds.set(age)
    return age


async def record_active_turns() -> int:
    """Sample committed active turns, including ones owned by another API process."""

    async with session_scope() as session:
        count = await session.scalar(
            select(func.count(ChatTurnRow.id)).where(
                ChatTurnRow.action == "chat", ChatTurnRow.state.in_(ACTIVE_STATES)
            )
        )
    total = int(count or 0)
    active_turns.set(total)
    return total


class ChatMaintenance:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="chat-maintenance")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await sweep_stale_turns(self._settings)
                await publish_processed_files(self._settings)
                await reclaim_failed_file_quota()
                await purge_deleted_files(self._settings)
                await purge_deleted_text()
                stale_uploads = await asyncio.to_thread(
                    purge_stale_uploads, Path(self._settings.chat_original_root)
                )
                if stale_uploads:
                    requests_total.add(
                        stale_uploads, {"operation": "staging_purge", "outcome": "succeeded"}
                    )
                await compact_expired_events()
                await record_oldest_pending_purge()
                await record_active_turns()
            except Exception as exc:
                requests_total.add(1, {"operation": "maintenance", "outcome": "failed"})
                logger.error("chat maintenance pass failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(SWEEP_SECONDS)
