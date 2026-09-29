"""Scoped temporary visual normalization through the private CPU file worker."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import func, select

from coire_api.auth import Principal
from coire_api.chat.file_manifest import validate_result
from coire_api.chat.files import new_job_id, stage_original
from coire_api.chat.processing import InvalidAsset, verify_asset
from coire_api.db import ChatFileProcessingRow, session_scope
from coire_api.gateway.context import VisualContextUnavailable
from coire_core.errors import ChatConflict, ChatQuotaExceeded
from coire_core.models.files import FileProcessRequest, FileProcessResult
from coire_core.models.gateway import ChatMessage, OpenAIImagePart, OpenAIImageURL, OpenAITextPart
from coire_core.models.registry import VisualCapability
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
TEMPORARY_SCHEMA_VERSION = 1
TEMPORARY_TTL = timedelta(hours=1)
MAX_TEMPORARY_JOBS_PER_PRINCIPAL = 8


class TemporaryVisualUnavailable(RuntimeError):
    """An isolated worker could not return a verified normalized image in time."""


class TemporaryVisualQuotaExceeded(RuntimeError):
    """A principal has exhausted the bounded temporary image workspace."""


def _principal_subject(principal: Principal) -> str:
    identity = principal.api_key_id or principal.run_id or principal.user_id or principal.subject
    if identity is None:
        raise VisualContextUnavailable("image processing requires an identified principal")
    value = str(identity)
    if len(value) > 128:
        raise VisualContextUnavailable("image processing identity is invalid")
    return value


async def _ready_asset(
    job: ChatFileProcessingRow, settings: Settings, visual: VisualCapability
) -> bytes:
    manifest = job.output_manifest or {}
    if job.state == "ready" and manifest.get("schema_version") != TEMPORARY_SCHEMA_VERSION:
        raise InvalidAsset("temporary asset schema mismatch")
    request = FileProcessRequest.model_validate(manifest["request"])
    result = FileProcessResult.model_validate(manifest["result"])
    if (
        job.attachment_id is not None
        or request.job_id != job.id
        or request.input_id != uuid.UUID(job.source_key)
        or request.source_sha256 != job.source_sha256
        or request.operation != "inspect"
        or result.detected_type not in {"image/png", "image/jpeg", "image/webp"}
    ):
        raise InvalidAsset("temporary asset identity mismatch")
    validate_result(request, result)
    asset = result.assets[0]
    if (
        asset.bytes > visual.max_encoded_bytes
        or asset.width * asset.height > visual.max_image_pixels
    ):
        raise VisualContextUnavailable("image exceeds this model's measured visual limits")
    return await asyncio.to_thread(verify_asset, Path(settings.chat_derived_root), job.id, asset)


async def _cached(
    principal: Principal, digest: str, settings: Settings, visual: VisualCapability
) -> bytes | None:
    async with session_scope() as session:
        job = await session.scalar(
            select(ChatFileProcessingRow)
            .where(
                ChatFileProcessingRow.attachment_id.is_(None),
                ChatFileProcessingRow.principal_kind == principal.kind.value,
                ChatFileProcessingRow.principal_subject == _principal_subject(principal),
                ChatFileProcessingRow.source_sha256 == digest,
                ChatFileProcessingRow.operation == "inspect",
                ChatFileProcessingRow.state == "ready",
                ChatFileProcessingRow.expires_at > datetime.now(UTC),
            )
            .order_by(ChatFileProcessingRow.updated_at.desc(), ChatFileProcessingRow.id.desc())
            .limit(1)
        )
    if job is None:
        return None
    try:
        return await _ready_asset(job, settings, visual)
    except (InvalidAsset, KeyError, TypeError, ValueError):
        logger.error("temporary visual cache invalid job_id=%s", job.id)
        return None


async def _submit(data: bytes, digest: str, principal: Principal, settings: Settings) -> str:
    subject = _principal_subject(principal)
    try:
        original = await stage_original(
            UploadFile(file=BytesIO(data)),
            Path(settings.chat_original_root),
            settings.chat_upload_max_bytes,
        )
    except ChatQuotaExceeded as exc:
        raise VisualContextUnavailable("inline image exceeds upload byte limit") from exc
    except ChatConflict as exc:
        raise TemporaryVisualUnavailable("image staging unavailable") from exc
    job_id = new_job_id()
    now = datetime.now(UTC)
    try:
        original.publish()
        if original.sha256 != digest:
            raise TemporaryVisualUnavailable("image staging changed during admission")
        async with session_scope() as session:
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtext(f"temporary-visual:{principal.kind.value}:{subject}")
                    )
                )
            )
            active = await session.scalar(
                select(func.count(ChatFileProcessingRow.id)).where(
                    ChatFileProcessingRow.attachment_id.is_(None),
                    ChatFileProcessingRow.principal_kind == principal.kind.value,
                    ChatFileProcessingRow.principal_subject == subject,
                    ChatFileProcessingRow.expires_at > now,
                    ChatFileProcessingRow.state != "purged",
                )
            )
            if int(active or 0) >= MAX_TEMPORARY_JOBS_PER_PRINCIPAL:
                raise TemporaryVisualQuotaExceeded("temporary image quota exceeded")
            session.add(
                ChatFileProcessingRow(
                    id=job_id,
                    attachment_id=None,
                    owner_user_id=principal.user_id,
                    principal_kind=principal.kind.value,
                    principal_subject=subject,
                    request_id=uuid.uuid4(),
                    operation="inspect",
                    source_key=str(original.id),
                    source_sha256=digest,
                    selected_pages=[],
                    state="queued",
                    attempt=1,
                    deadline_at=now + timedelta(seconds=settings.file_worker_process_timeout_s),
                    expires_at=now + TEMPORARY_TTL,
                    created_at=now,
                    updated_at=now,
                )
            )
    except TemporaryVisualQuotaExceeded:
        original.target.unlink(missing_ok=True)
        original.discard()
        raise
    except Exception as exc:
        original.target.unlink(missing_ok=True)
        original.discard()
        raise TemporaryVisualUnavailable("image staging unavailable") from exc
    except BaseException:
        original.target.unlink(missing_ok=True)
        original.discard()
        raise
    return job_id


async def _wait_for_asset(job_id: str, settings: Settings, visual: VisualCapability) -> bytes:
    try:
        return await _poll_asset(job_id, settings, visual)
    except BaseException:
        task = asyncio.create_task(_expire_job(job_id))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(
                "temporary visual cancellation marker failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )
        raise


async def _expire_job(job_id: str) -> None:
    async with session_scope() as session:
        job = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
        if job is not None and job.attachment_id is None:
            job.expires_at = datetime.now(UTC)
            job.updated_at = datetime.now(UTC)


async def _poll_asset(job_id: str, settings: Settings, visual: VisualCapability) -> bytes:
    deadline = asyncio.get_running_loop().time() + settings.file_worker_process_timeout_s + 15
    while asyncio.get_running_loop().time() < deadline:
        async with session_scope() as session:
            job = await session.get(ChatFileProcessingRow, job_id)
        if (
            job is not None
            and job.state == "failed"
            and job.safe_error
            in {
                "animated_image_unsupported",
                "derived_output_too_large",
                "image_too_large",
                "invalid_image",
                "invalid_original_size",
                "unsupported_file_type",
            }
        ):
            raise VisualContextUnavailable("inline image could not be safely normalized")
        if job is None or job.state in {"failed", "cancelled", "purging", "purged"}:
            raise TemporaryVisualUnavailable("image processing unavailable")
        if job.state in {"processed", "ready"}:
            try:
                image = await _ready_asset(job, settings, visual)
            except VisualContextUnavailable:
                raise
            except (InvalidAsset, KeyError, TypeError, ValueError) as exc:
                async with session_scope() as session:
                    failed = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
                    if failed is not None and failed.state == "processed":
                        failed.state = "failed"
                        failed.safe_error = "invalid_worker_output"
                raise TemporaryVisualUnavailable("image processing unavailable") from exc
            if job.state == "processed":
                async with session_scope() as session:
                    ready = await session.get(ChatFileProcessingRow, job_id, with_for_update=True)
                    if ready is not None and ready.state == "processed":
                        ready.output_manifest = {
                            **(ready.output_manifest or {}),
                            "schema_version": TEMPORARY_SCHEMA_VERSION,
                        }
                        ready.state = "ready"
                        ready.updated_at = datetime.now(UTC)
            return image
        await asyncio.sleep(0.25)
    raise TemporaryVisualUnavailable("image processing timed out")


async def normalize_inline_images(
    messages: list[ChatMessage], principal: Principal, settings: Settings, visual: VisualCapability
) -> list[ChatMessage]:
    """Replace bounded inline images with worker-normalized, scoped PNGs."""
    if not visual.verified:
        raise VisualContextUnavailable("selected model has no verified visual input")
    normalized: list[ChatMessage] = []
    count = 0
    for message in messages:
        if not isinstance(message.content, list):
            normalized.append(message)
            continue
        parts: list[OpenAITextPart | OpenAIImagePart] = []
        for part in message.content:
            if not isinstance(part, OpenAIImagePart):
                parts.append(part)
                continue
            count += 1
            if count > visual.max_images:
                raise VisualContextUnavailable("too many images for the selected model")
            encoded = part.image_url.url.partition(",")[2]
            data = base64.b64decode(encoded, validate=True)
            digest = hashlib.sha256(data).hexdigest()
            image = await _cached(principal, digest, settings, visual)
            if image is None:
                job_id = await _submit(data, digest, principal, settings)
                image = await _wait_for_asset(job_id, settings, visual)
            parts.append(
                OpenAIImagePart(
                    image_url=OpenAIImageURL(
                        url="data:image/png;base64," + base64.b64encode(image).decode("ascii"),
                        detail=part.image_url.detail,
                    )
                )
            )
        normalized.append(message.model_copy(update={"content": parts}))
    return normalized
