"""Failed conversions return unused derivative quota only after output erasure."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.sql.elements import ClauseElement

from coire_api.chat import maintenance
from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatFileProcessingRow,
    ChatQuotaReservationRow,
    UserRow,
)

JOB_ID = "01K00000000000000000000000"


class QuotaRows:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.owner = UserRow(id=uuid.uuid4(), email="owner@example.test")
        self.conversation = ChatConversationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner.id,
            title="Private",
            mode="chat",
            revision=3,
            event_cursor=1,
            created_at=now,
            updated_at=now,
        )
        self.attachment = ChatAttachmentRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner.id,
            conversation_id=self.conversation.id,
            filename="private.pdf",
            detected_type="application/pdf",
            original_bytes=20,
            original_sha256="a" * 64,
            original_key="generated",
            derived_bytes=0,
            state="failed",
            created_at=now,
            updated_at=now,
        )
        self.job = ChatFileProcessingRow(
            id=JOB_ID,
            attachment_id=self.attachment.id,
            owner_user_id=self.owner.id,
            principal_kind="user",
            principal_subject=str(self.owner.id),
            request_id=uuid.uuid4(),
            operation="inspect",
            source_key=str(self.attachment.id),
            source_sha256="a" * 64,
            selected_pages=[],
            output_manifest={"output_purged": True},
            state="failed",
            attempt=1,
            deadline_at=now - timedelta(seconds=20),
            expires_at=now + timedelta(hours=24),
            created_at=now,
            updated_at=now,
        )
        self.reservation = ChatQuotaReservationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner.id,
            conversation_id=self.conversation.id,
            attachment_id=self.attachment.id,
            job_id=JOB_ID,
            reserved_bytes=20 + 32 * 1024 * 1024,
            state="active",
            expires_at=now + timedelta(hours=24),
            created_at=now,
        )
        self.queries: list[str] = []

    async def execute(self, statement: object) -> SimpleNamespace:
        self.queries.append(
            str(cast(ClauseElement, statement).compile(compile_kwargs={"literal_binds": True}))
        )
        eligible = (
            self.job.state == "failed"
            and (self.job.output_manifest or {}).get("output_purged") is True
            and self.reservation.reserved_bytes
            > self.attachment.original_bytes + self.attachment.derived_bytes
            and self.conversation.deleted_at is None
        )
        return SimpleNamespace(scalars=lambda: [JOB_ID] if eligible else [])

    async def get(self, model: object, key: object, **_kwargs: object) -> Any:
        for row in (self.owner, self.conversation, self.attachment, self.job):
            if type(row) is model and row.id == key:
                return row
        return None

    async def scalar(self, _statement: object) -> ChatQuotaReservationRow:
        return self.reservation


async def test_failed_quota_reclaims_after_marker_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = QuotaRows()

    @asynccontextmanager
    async def sessions() -> AsyncIterator[QuotaRows]:
        yield rows

    monkeypatch.setattr(maintenance, "session_scope", sessions)
    assert await maintenance.reclaim_failed_file_quota() == 1
    assert rows.reservation.reserved_bytes == 20
    assert "output_purged" in rows.queries[0]
    assert await maintenance.reclaim_failed_file_quota() == 0
    rows.reservation.reserved_bytes = 20 + 32 * 1024 * 1024
    rows.job.output_manifest = {"output_purged": False}
    assert await maintenance.reclaim_failed_file_quota() == 0
    assert rows.reservation.reserved_bytes > 20
    rows.job.output_manifest = {"output_purged": True}
    rows.conversation.deleted_at = datetime.now(UTC)
    assert await maintenance.reclaim_failed_file_quota() == 0
