"""Owner-initiated file retry waits for purged failure output and admits only attempt two."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.sql.elements import ClauseElement

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatEventRow,
    ChatFileProcessingRow,
    ChatQuotaReservationRow,
    get_session,
)
from coire_core.settings import Settings, get_settings


class RetrySession:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.owner_id = uuid.uuid4()
        self.conversation = ChatConversationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner_id,
            title="Private",
            mode="chat",
            revision=3,
            event_cursor=1,
            created_at=now,
            updated_at=now,
        )
        self.attachment = ChatAttachmentRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner_id,
            conversation_id=self.conversation.id,
            filename="secret.pdf",
            detected_type="application/octet-stream",
            original_bytes=20,
            original_sha256="a" * 64,
            original_key="",
            derived_bytes=0,
            state="failed",
            safe_error="worker_status_missing",
            created_at=now,
            updated_at=now,
        )
        self.attachment.original_key = str(self.attachment.id)
        self.old = ChatFileProcessingRow(
            id="01K00000000000000000000000",
            attachment_id=self.attachment.id,
            owner_user_id=self.owner_id,
            principal_kind="user",
            principal_subject=str(self.owner_id),
            request_id=uuid.uuid4(),
            operation="inspect",
            source_key=str(self.attachment.id),
            source_sha256=self.attachment.original_sha256,
            selected_pages=[],
            output_manifest={"output_purged": True},
            state="failed",
            safe_error="worker_status_missing",
            attempt=1,
            deadline_at=now - timedelta(seconds=30),
            expires_at=now + timedelta(hours=24),
            created_at=now,
            updated_at=now,
        )
        self.reservation = ChatQuotaReservationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner_id,
            conversation_id=self.conversation.id,
            attachment_id=self.attachment.id,
            job_id=self.old.id,
            reserved_bytes=20 + 32 * 1024 * 1024,
            state="active",
            expires_at=now + timedelta(hours=24),
            created_at=now,
        )
        self.next_job: ChatFileProcessingRow | None = None
        self.events: list[ChatEventRow] = []
        self.commits = 0

    async def scalar(self, statement: object) -> Any:
        sql = str(statement)
        if "FROM users" in sql:
            return object()
        if "FROM chat_conversations" in sql:
            return self.conversation
        if "FROM chat_attachments" in sql:
            return self.attachment
        if "FROM chat_file_processing" in sql and "WHERE chat_file_processing.request_id" in sql:
            rendered = str(
                cast(ClauseElement, statement).compile(compile_kwargs={"literal_binds": True})
            )
            return (
                self.next_job
                if self.next_job is not None and self.next_job.request_id.hex in rendered
                else None
            )
        if "FROM chat_file_processing" in sql:
            return self.next_job or self.old
        if "FROM chat_quota_reservations" in sql:
            return self.reservation
        raise AssertionError(sql)

    def add(self, row: object) -> None:
        if isinstance(row, ChatFileProcessingRow):
            self.next_job = row
        elif isinstance(row, ChatEventRow):
            self.events.append(row)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        chat_enabled=True,
        chat_browser_origin="http://localhost",
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr("file-retry-test"),
    )


async def test_retry_route_requires_cleanup_and_admits_once() -> None:
    session = RetrySession()
    settings = _settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    current_user = session.owner_id
    app.dependency_overrides[require_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=current_user
    )

    async def fake_session() -> AsyncIterator[RetrySession]:
        yield session

    app.dependency_overrides[get_session] = fake_session
    path = f"/api/v1/chat/conversations/{session.conversation.id}/files/{session.attachment.id}/process"
    request_id = uuid.uuid4()
    body = {"request_id": str(request_id), "expected_revision": 3, "operation": "inspect"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"Authorization": "Bearer file-retry-test"},
    ) as client:
        assert (await client.post(path, json=body)).status_code == 403
        session.old.output_manifest = {"request": "unpurged"}
        pending = await client.post(path, json=body, headers={"Origin": "http://localhost"})
        assert pending.status_code == 409
        assert session.commits == 0
        session.old.output_manifest = {"output_purged": True}
        stale = await client.post(
            path,
            json={**body, "expected_revision": 2},
            headers={"Origin": "http://localhost"},
        )
        assert stale.status_code == 409
        accepted = await client.post(path, json=body, headers={"Origin": "http://localhost"})
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["state"] == "processing"
        admitted = session.next_job
        assert admitted is not None
        assert admitted.attempt == 2
        assert admitted.state == "queued"
        assert admitted.source_sha256 == session.old.source_sha256
        assert session.reservation.job_id == admitted.id
        assert session.conversation.revision == 4
        assert len(session.events) == 1
        duplicate = await client.post(path, json=body, headers={"Origin": "http://localhost"})
        assert duplicate.status_code == 202
        assert session.commits == 1
        changed_replay = await client.post(
            path,
            json={**body, "expected_revision": 4},
            headers={"Origin": "http://localhost"},
        )
        assert changed_replay.status_code == 409
        capped = await client.post(
            path,
            json={**body, "request_id": str(uuid.uuid4()), "expected_revision": 4},
            headers={"Origin": "http://localhost"},
        )
        assert capped.status_code == 409
        foreign_parent = await client.post(
            path.replace(str(session.conversation.id), str(uuid.uuid4())),
            json=body,
            headers={"Origin": "http://localhost"},
        )
        assert foreign_parent.status_code == 404
        current_user = uuid.uuid4()
        foreign = await client.post(path, json=body, headers={"Origin": "http://localhost"})
        assert foreign.status_code == 404


@pytest.mark.parametrize("operation,pages", [("render", [1]), ("inspect", [1])])
async def test_retry_route_refuses_unsupported_action(operation: str, pages: list[int]) -> None:
    session = RetrySession()
    settings = _settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[require_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=session.owner_id
    )

    async def fake_session() -> AsyncIterator[RetrySession]:
        yield session

    app.dependency_overrides[get_session] = fake_session
    path = f"/api/v1/chat/conversations/{session.conversation.id}/files/{session.attachment.id}/process"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"Authorization": "Bearer file-retry-test"},
    ) as client:
        response = await client.post(
            path,
            json={
                "request_id": str(uuid.uuid4()),
                "expected_revision": 3,
                "operation": operation,
                "selected_pages": pages,
            },
            headers={"Origin": "http://localhost"},
        )
    assert response.status_code in {409, 422}
    assert session.next_job is None
