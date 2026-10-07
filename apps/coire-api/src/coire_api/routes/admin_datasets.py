"""Administrator JSONL uploads with quota/auth guards before multipart parsing."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any, cast

import anyio
from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import Response
from fastapi.routing import APIRoute
from pydantic import Json
from starlette.types import Message

from coire_api.auth import Principal, bound_principal
from coire_api.db import session_scope
from coire_api.training.authorization import CurrentTrainingAdmin, require_training_principal
from coire_api.training.datasets import (
    analyze_dataset,
    dataset_detail,
    dataset_page,
    purge_retired_dataset,
    register_source,
    retire_dataset,
    upload_identity,
)
from coire_api.training.quota import finish_upload_hold, reserve_upload
from coire_api.training.storage import DatasetStore
from coire_core.errors import TrainingUnavailable, TrainingUploadTooLarge, TrainingValidationError
from coire_core.models.datasets import (
    DatasetAnalysisReceipt,
    DatasetAnalyzeRequest,
    DatasetDeleteRequest,
    DatasetDeletionReceipt,
    DatasetDetail,
    DatasetPage,
    DatasetReceipt,
    DatasetUploadRequest,
)
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class UploadContext:
    principal: Principal
    dataset_id: uuid.UUID
    hold_id: uuid.UUID
    declared_bytes: int
    key: str
    store: DatasetStore
    retained_bytes: int = 0
    publication_uncertain: bool = False


class DatasetUploadRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        if self.path != "/api/v1/admin/datasets" or "POST" not in (self.methods or set()):
            return original

        async def guarded(request: Request) -> Response:
            principal = await require_training_principal(request, bound_principal())
            settings = cast(Settings, request.app.state.settings)
            if not settings.training_enabled:
                raise TrainingUnavailable("Training datasets are disabled")
            length = request.headers.getlist("content-length")
            if len(length) != 1 or re.fullmatch(r"[0-9]{1,10}", length[0]) is None:
                raise TrainingValidationError("Dataset uploads require one bounded Content-Length")
            declared = int(length[0])
            if not 1 <= declared <= settings.training_dataset_upload_max_bytes + 64 * 1024:
                raise TrainingUploadTooLarge()
            if request.headers.get(
                "content-encoding", "identity"
            ).lower() != "identity" or request.headers.get("transfer-encoding"):
                raise TrainingValidationError(
                    "Compressed or unbounded dataset uploads are unsupported"
                )
            keys = request.headers.getlist("idempotency-key")
            if len(keys) != 1 or principal.user_id is None:
                raise TrainingValidationError("Dataset upload requires one idempotency key")
            identity = upload_identity(principal.user_id, keys[0])
            async with session_scope() as session:
                hold = await reserve_upload(
                    session, principal, settings, declared_bytes=declared, subject_id=identity
                )
                hold_id = hold.id
            received = 0
            store: DatasetStore | None = None
            bounded: Request | None = None
            context: UploadContext | None = None

            async def receive() -> Message:
                nonlocal received
                message = await request.receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > declared:
                        raise TrainingUploadTooLarge()
                return message

            try:
                store = await anyio.to_thread.run_sync(DatasetStore, settings)
                bounded = Request(request.scope, receive=receive)
                context = UploadContext(principal, identity, hold_id, declared, keys[0], store)
                bounded.state.dataset_upload = context
                # The body is bounded in bytes and duration; callbacks and source
                # validation cannot hold admission indefinitely after a disconnect.
                async with asyncio.timeout(settings.training_analysis_timeout_s):
                    return await original(bounded)
            finally:
                with anyio.CancelScope(shield=True):
                    cleanup_proven = received == 0
                    if bounded is not None:
                        form = getattr(bounded, "_form", None)
                        if form is not None:
                            await form.close()
                            cleanup_proven = True
                    if store is not None:
                        await store.discard(hold_id)
                    if context is not None and context.publication_uncertain:
                        cleanup_proven = False
                    async with session_scope() as session:
                        await finish_upload_hold(
                            session,
                            hold_id,
                            retained_bytes=context.retained_bytes if context else 0,
                            private_staging_cleanup_proven=cleanup_proven,
                        )

        return guarded


router = APIRouter(
    prefix="/api/v1/admin/datasets", tags=["admin:datasets"], route_class=DatasetUploadRoute
)


@router.post("", response_model=DatasetReceipt, status_code=202)
async def upload_dataset(
    request: Request,
    metadata: Annotated[Json[DatasetUploadRequest], Form()],
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> DatasetReceipt:
    context = cast(UploadContext, request.state.dataset_upload)
    form = await request.form()
    if len(form.multi_items()) != 2 or set(form) != {"metadata", "file"}:
        raise TrainingValidationError("Dataset upload requires exactly metadata and one file")
    if idempotency_key != context.key:
        raise TrainingValidationError("Dataset upload command identity differs")

    async def data() -> AsyncIterator[bytes]:
        while chunk := await file.read(64 * 1024):
            yield chunk

    result = await context.store.stage(
        context.hold_id,
        context.dataset_id,
        metadata,
        data(),
        byte_ceiling=min(
            context.declared_bytes,
            cast(Settings, request.app.state.settings).training_dataset_upload_max_bytes,
        ),
    )
    published = False
    try:
        async with session_scope() as session:
            receipt, created = await register_source(
                session,
                context.principal,
                metadata,
                result,
                dataset_id=context.dataset_id,
                hold_id=context.hold_id,
                key=context.key,
            )
            retained = 0
            if created and not result.invalid_count:
                retained = await context.store.commit(context.hold_id, context.dataset_id, result)
                published = True
        context.retained_bytes = retained
        return receipt
    except BaseException:
        if published:
            # A failed commit acknowledgement is not proof the DB transaction
            # failed. Keep source bytes private and counted for reconciliation;
            # never delete a possibly committed immutable source here.
            context.publication_uncertain = True
        raise


@router.get("", response_model=DatasetPage)
async def list_datasets(
    request: Request,
    principal: CurrentTrainingAdmin,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> DatasetPage:
    if not request.app.state.settings.training_enabled:
        raise TrainingUnavailable("Training datasets are disabled")
    async with session_scope() as session:
        return await dataset_page(session, principal, cursor=cursor, limit=limit)


@router.get("/{dataset_id}", response_model=DatasetDetail)
async def get_dataset(
    request: Request, dataset_id: uuid.UUID, principal: CurrentTrainingAdmin
) -> DatasetDetail:
    if not request.app.state.settings.training_enabled:
        raise TrainingUnavailable("Training datasets are disabled")
    async with session_scope() as session:
        return await dataset_detail(session, principal, dataset_id)


@router.post("/{dataset_id}/analyze", response_model=DatasetAnalysisReceipt, status_code=202)
async def analyze(
    request: Request,
    dataset_id: uuid.UUID,
    body: DatasetAnalyzeRequest,
    principal: CurrentTrainingAdmin,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> DatasetAnalysisReceipt:
    if not request.app.state.settings.training_enabled:
        raise TrainingUnavailable("Training datasets are disabled")
    async with session_scope() as session:
        return await analyze_dataset(session, principal, dataset_id, body, idempotency_key)


@router.delete("/{dataset_id}", response_model=DatasetDeletionReceipt, status_code=202)
async def delete_dataset(
    request: Request,
    dataset_id: uuid.UUID,
    body: DatasetDeleteRequest,
    principal: CurrentTrainingAdmin,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> DatasetDeletionReceipt:
    if not request.app.state.settings.training_enabled:
        raise TrainingUnavailable("Training datasets are disabled")
    async with session_scope() as session:
        receipt = await retire_dataset(session, principal, dataset_id, body, idempotency_key)
    try:
        store = await anyio.to_thread.run_sync(DatasetStore, request.app.state.settings)
        async with session_scope() as session:
            await purge_retired_dataset(session, dataset_id, store)
    except Exception as error:
        # Retirement/audit already committed. The durable scheduler cleanup lane
        # retries; a failed file operation must not undo the counted source hold.
        logger.error(
            "dataset cleanup pending",
            extra={"dataset_id": str(dataset_id), "error_type": type(error).__name__},
        )
    return receipt
