"""Normalized owner inputs publish only after a bound worker result and local bytes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from coire_api.db import ImageInputRow
from coire_api.file_worker_client import FileWorkerParseRefused
from coire_api.images.inputs import project_image_input
from coire_core.models.files import ImageFileProcessRequest, ImageFileProcessResult
from coire_core.settings import Settings
from coire_scheduler import image_inputs as workflow


def _row() -> ImageInputRow:
    input_id = uuid.uuid4()
    return ImageInputRow(
        id=input_id,
        owner_user_id=uuid.uuid4(),
        purpose="mask",
        original_key=str(input_id),
        original_bytes=7,
        original_sha256="a" * 64,
        state="processing",
        processing_job_id="01K00000000000000000000000",
        held_bytes=7 + 10 * 1024 * 1024,
        active_references=0,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class FakeSession:
    def __init__(self, row: ImageInputRow) -> None:
        self.row = row
        self.lock_order: list[str] = []

    async def execute(self, statement: object) -> None:
        assert "pg_advisory_xact_lock" in str(statement)
        self.lock_order.append("quota")

    async def get(self, model: type[object], identity: object, **kwargs: object) -> object | None:
        assert model is ImageInputRow and identity == self.row.id
        if kwargs.get("with_for_update"):
            self.lock_order.append("input")
        return self.row


async def test_normalize_workflow_binds_result_verifies_bytes_and_settles_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row()
    session = FakeSession(row)

    @asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        yield session

    commands: list[ImageFileProcessRequest] = []
    settlements: list[tuple[int, int]] = []

    class Client:
        def __init__(self, settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def process_image_input(
            self, request: ImageFileProcessRequest
        ) -> ImageFileProcessResult:
            commands.append(request)
            return ImageFileProcessResult(
                job_id=request.job_id,
                input_id=request.input_id,
                operation=request.operation,
                source_sha256=request.source_sha256,
                output_id=request.output_id,
                normalized_sha256="b" * 64,
                normalized_bytes=123,
                width=512,
                height=512,
            )

    async def settle(_session: object, _owner: object, held: int, stored: int) -> None:
        settlements.append((held, stored))

    monkeypatch.setattr(workflow, "session_scope", scope)
    monkeypatch.setattr(workflow, "FileWorkerClient", Client)
    monkeypatch.setattr(workflow, "settle_storage_hold", settle)
    monkeypatch.setattr(workflow, "_verified_normalized", lambda path, result: True)
    monkeypatch.setattr(workflow, "get_settings", lambda: Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    await workflow.drive_normalized_input(row.id)
    assert commands[0].purpose == "mask" and commands[0].operation == "normalize_mask"
    assert row.state == "ready" and row.normalized_key == str(row.id)
    assert (row.normalized_sha256, row.normalized_width, row.normalized_height) == (
        "b" * 64,
        512,
        512,
    )
    projected = project_image_input(row)
    assert (projected.sha256, projected.byte_count, projected.width, projected.height) == (
        "b" * 64,
        123,
        512,
        512,
    )
    assert settlements == [(7 + 10 * 1024 * 1024, 130)]
    assert session.lock_order == ["quota", "input"]
    await workflow.drive_normalized_input(row.id)
    assert len(commands) == len(settlements) == 1


async def test_normalize_refusal_keeps_hold_for_physical_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row()

    @asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        yield FakeSession(row)

    class Client:
        def __init__(self, settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def process_image_input(
            self, request: ImageFileProcessRequest
        ) -> ImageFileProcessResult:
            raise FileWorkerParseRefused("invalid")

    monkeypatch.setattr(workflow, "session_scope", scope)
    monkeypatch.setattr(workflow, "FileWorkerClient", Client)
    monkeypatch.setattr(workflow, "get_settings", lambda: Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    await workflow.drive_normalized_input(row.id)
    assert row.state == "failed" and row.held_bytes == 7 + 10 * 1024 * 1024
