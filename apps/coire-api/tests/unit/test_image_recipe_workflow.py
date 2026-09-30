"""Durable recipe workflow binds worker results and settles storage once."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from coire_api.db import ImageInputRow
from coire_api.file_worker_client import FileWorkerBusy, FileWorkerParseRefused
from coire_core.models.image_worker import ImageRecipeParseRequest, ImageRecipeParseResult
from coire_core.models.images import (
    ImageRecipe,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_core.settings import Settings


def _row() -> ImageInputRow:
    input_id = uuid.uuid4()
    return ImageInputRow(
        id=input_id,
        owner_user_id=uuid.uuid4(),
        purpose="recipe",
        original_key=str(input_id),
        original_bytes=7,
        original_sha256="a" * 64,
        state="processing",
        processing_job_id="01K00000000000000000000000",
        held_bytes=7,
        active_references=0,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _result(request: ImageRecipeParseRequest) -> ImageRecipeParseResult:
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        prompt="private",
        seed=7,
        width=512,
        height=512,
        steps=20,
        guidance=Decimal("3"),
    )
    return ImageRecipeParseResult(
        input_id=request.input_id,
        source_sha256=request.source_sha256,
        byte_count=request.byte_count,
        recipe=ImageRecipe(
            resolved=ResolvedImageSpec(
                spec=spec,
                seeds=(7,),
                pipeline_version="mflux-0.20.0",
                environment_fingerprint="b" * 64,
                model_sha256="c" * 64,
                spec_hash=canonical_spec_hash(spec),
            ),
            output_index=0,
            seed=7,
            pixel_sha256="d" * 64,
            width=512,
            height=512,
        ),
    )


class FakeSession:
    def __init__(self, row: ImageInputRow) -> None:
        self.row = row

    async def get(self, model: type[object], identity: object, **_: object) -> object | None:
        assert model is ImageInputRow and identity == self.row.id
        return self.row


async def test_recipe_workflow_persists_and_settles_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_scheduler import image_inputs as workflow

    row = _row()
    session = FakeSession(row)

    @asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        yield session

    calls: list[int] = []

    class Client:
        def __init__(self, settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def parse_image_recipe(
            self, request: ImageRecipeParseRequest
        ) -> ImageRecipeParseResult:
            calls.append(request.byte_count)
            return _result(request)

    async def settle(*args: object) -> None:
        calls.append(99)

    monkeypatch.setattr(workflow, "session_scope", scope)
    monkeypatch.setattr(workflow, "FileWorkerClient", Client)
    monkeypatch.setattr(workflow, "settle_storage_hold", settle)
    monkeypatch.setattr(workflow, "get_settings", lambda: Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    await workflow.drive_recipe_input(row.id)
    assert row.state == "ready" and row.recipe is not None and row.held_bytes == 0
    assert calls == [7, 99]
    await workflow.drive_recipe_input(row.id)
    assert calls == [7, 99]


@pytest.mark.parametrize("failure", [FileWorkerBusy, FileWorkerParseRefused])
async def test_recipe_workflow_retries_busy_but_records_stable_refusal(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    from coire_scheduler import image_inputs as workflow

    row = _row()
    session = FakeSession(row)

    @asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        yield session

    class Client:
        def __init__(self, settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def parse_image_recipe(
            self, request: ImageRecipeParseRequest
        ) -> ImageRecipeParseResult:
            raise failure("private content")

    monkeypatch.setattr(workflow, "session_scope", scope)
    monkeypatch.setattr(workflow, "FileWorkerClient", Client)
    monkeypatch.setattr(workflow, "get_settings", lambda: Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    if failure is FileWorkerBusy:
        with pytest.raises(FileWorkerBusy):
            await workflow.drive_recipe_input(row.id)
        assert row.state == "processing"
    else:
        await workflow.drive_recipe_input(row.id)
        assert row.state == "failed" and row.held_bytes == 7


async def test_deletion_during_parse_prevents_recipe_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_scheduler import image_inputs as workflow

    row = _row()
    session = FakeSession(row)

    @asynccontextmanager
    async def scope() -> AsyncIterator[FakeSession]:
        yield session

    class Client:
        def __init__(self, settings: Settings) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def parse_image_recipe(
            self, request: ImageRecipeParseRequest
        ) -> ImageRecipeParseResult:
            row.state = "deleting"
            row.deleted_at = datetime.now(UTC)
            return _result(request)

    async def no_settlement(*args: object) -> None:
        pytest.fail("deleted input settled after parser finished")

    monkeypatch.setattr(workflow, "session_scope", scope)
    monkeypatch.setattr(workflow, "FileWorkerClient", Client)
    monkeypatch.setattr(workflow, "settle_storage_hold", no_settlement)
    monkeypatch.setattr(workflow, "get_settings", lambda: Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    await workflow.drive_recipe_input(row.id)
    assert row.state == "deleting" and row.recipe is None and row.held_bytes == 7
