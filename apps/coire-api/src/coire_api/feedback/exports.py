"""Private export staging ownership, counted before any file or copied row."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    FeedbackMutationRow,
    ModelRow,
    ModelVariantRow,
    PreferenceExportRow,
    TrainingAdapterRow,
    TrainingStorageReservationRow,
)
from coire_api.feedback.eligibility import authorize_admin
from coire_api.feedback.quota import require_feedback_capacity
from coire_api.training.datasets import analysis_base, upload_identity
from coire_api.training.quota import reserve_admitted_upload, upload_reservation_bytes
from coire_api.training.service import training_id
from coire_core.errors import (
    FeedbackConflict,
    FeedbackNotFound,
    FeedbackQuotaExceeded,
    TrainingUnavailable,
    TrainingValidationError,
)
from coire_core.models.datasets import DatasetFormat, DatasetProvenance, DatasetUploadRequest
from coire_core.models.feedback import (
    PreferenceExportCancel,
    PreferenceExportCreate,
    PreferenceExportDetail,
    PreferenceExportReceipt,
)
from coire_core.models.preference import canonical_bytes
from coire_core.settings import Settings


async def allocate_export_stage(
    session: AsyncSession,
    principal: Principal,
    export: PreferenceExportRow,
    settings: Settings,
    *,
    declared_bytes: int,
) -> TrainingStorageReservationRow:
    if export.state != "staging" or export.staging:
        raise FeedbackConflict("Export stage already exists or is not admitted")
    await authorize_admin(session, principal)
    needed = upload_reservation_bytes(declared_bytes, settings)
    await require_feedback_capacity(session, needed, settings)
    dataset_id = upload_identity(export.owner_user_id, "feedback-export:" + export.id)
    hold = await reserve_admitted_upload(
        session, principal, settings, declared_bytes=declared_bytes, subject_id=dataset_id
    )
    export.staging = {"hold_id": str(hold.id), "dataset_id": str(dataset_id)}
    await session.flush()
    return hold


def export_metadata(body: PreferenceExportCreate, identity: str) -> DatasetUploadRequest:
    return DatasetUploadRequest(
        name=body.name,
        format=DatasetFormat.PREFERENCE,
        provenance=DatasetProvenance(
            source="feedback-export:" + identity, license_note=body.license_note
        ),
        analysis_model_id=body.model_id,
        analysis_variant_id=body.variant_id,
        split_seed=body.split_seed,
        validation_fraction=body.validation_fraction,
    )


async def validate_export_registry(session: AsyncSession, body: PreferenceExportCreate) -> None:
    await analysis_base(session, export_metadata(body, "preflight"))
    filters = body.filters
    if filters.model_id is not None and await session.get(ModelRow, filters.model_id) is None:
        raise TrainingValidationError("Export model filter is not registered")
    if filters.variant_id is not None:
        variant = await session.get(ModelVariantRow, filters.variant_id)
        if variant is None or variant.model_id != filters.model_id:
            raise TrainingValidationError(
                "Export variant filter differs from its registered parent"
            )
    if filters.adapter_id is not None:
        adapter = await session.get(TrainingAdapterRow, filters.adapter_id)
        if (
            adapter is None
            or adapter.model_id != filters.model_id
            or (filters.variant_id is not None and adapter.base_variant_id != filters.variant_id)
        ):
            raise TrainingValidationError(
                "Export adapter filter differs from its registered parent"
            )


def export_receipt(row: PreferenceExportRow) -> PreferenceExportReceipt:
    return PreferenceExportReceipt.model_validate(
        {"id": row.id, "state": row.state, "version": row.version}
    )


def export_detail(row: PreferenceExportRow) -> PreferenceExportDetail:
    return PreferenceExportDetail.model_validate(
        {
            **export_receipt(row).model_dump(),
            "owner_id": row.owner_user_id,
            "request": row.request,
            "selected_count": row.selected_count,
            "matched_count": row.matched_count,
            "excluded_count": row.excluded_count,
            "warnings": row.warnings,
            "dataset_id": row.dataset_id,
            "cleanup_pending": row.cleanup_pending,
            "reason": row.terminal_reason,
            "created_at": row.created_at,
            "deadline": row.execution_deadline_at or row.queue_deadline_at,
        }
    )


async def export_row(
    session: AsyncSession, principal: Principal, identity: str
) -> PreferenceExportRow:
    await authorize_admin(session, principal)
    row = await session.get(
        PreferenceExportRow, identity, populate_existing=True, with_for_update=True
    )
    if row is None:
        raise FeedbackNotFound()
    return row


async def submit_export(
    session: AsyncSession,
    principal: Principal,
    body: PreferenceExportCreate,
    key: str,
    settings: Settings,
) -> PreferenceExportReceipt:
    owner = await authorize_admin(session, principal)
    intent = hashlib.sha256(canonical_bytes(body.model_dump(mode="json"))).hexdigest()
    command = await session.scalar(
        select(FeedbackMutationRow).where(
            FeedbackMutationRow.actor_user_id == owner,
            FeedbackMutationRow.operation == "export.create",
            FeedbackMutationRow.request_id == key,
        )
    )
    if command is not None:
        if command.intent_sha256 != intent:
            raise FeedbackConflict("Export identity was reused with different input")
        return export_receipt(
            await export_row(session, principal, str(command.receipt["export_id"]))
        )
    if not settings.training_enabled or not settings.preference_training_enabled:
        raise TrainingUnavailable("Preference training admissions are disabled")
    await validate_export_registry(session, body)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('feedback.export.queue', 0))")
    )
    queued = await session.scalar(
        select(func.count())
        .select_from(PreferenceExportRow)
        .where(PreferenceExportRow.state == "queued")
    )
    if (queued or 0) >= 100:
        raise FeedbackQuotaExceeded("Feedback export queue is full")
    now = datetime.now(UTC)
    row = PreferenceExportRow(
        id=training_id(),
        owner_user_id=owner,
        authorization_snapshot=principal.model_dump(mode="json"),
        request=body.model_dump(mode="json"),
        state="queued",
        version=1,
        selected_count=0,
        matched_count=0,
        excluded_count=0,
        warnings=[],
        staging={},
        cleanup_pending=False,
        created_at=now,
        queue_deadline_at=now + timedelta(hours=1),
    )
    session.add(row)
    await session.flush()
    session.add(
        FeedbackMutationRow(
            actor_user_id=owner,
            operation="export.create",
            request_id=key,
            intent_sha256=intent,
            receipt={"export_id": row.id},
        )
    )
    await write_principal_audit(
        session,
        principal=principal,
        action="feedback.export.create",
        target_type="feedback_export",
        target_id=row.id,
        context={"state": "queued"},
    )
    return export_receipt(row)


async def cancel_export(
    session: AsyncSession,
    principal: Principal,
    identity: str,
    body: PreferenceExportCancel,
    key: str,
) -> PreferenceExportReceipt:
    row = await export_row(session, principal, identity)
    owner = principal.user_id
    assert owner is not None
    intent = hashlib.sha256(canonical_bytes({"id": identity, **body.model_dump()})).hexdigest()
    command = await session.scalar(
        select(FeedbackMutationRow).where(
            FeedbackMutationRow.actor_user_id == owner,
            FeedbackMutationRow.operation == "export.cancel",
            FeedbackMutationRow.request_id == key,
        )
    )
    if command is not None:
        if command.intent_sha256 != intent:
            raise FeedbackConflict("Cancel identity was reused with different input")
        return export_receipt(row)
    if row.dataset_id is not None:
        raise FeedbackConflict(f"Export is published as dataset {row.dataset_id}")
    if row.version != body.expected_version or row.state not in {"queued", "staging"}:
        raise FeedbackConflict("Export changed or can no longer be cancelled")
    row.state, row.terminal_reason = "cancelled", "cancelled"
    row.version += 1
    row.fence += 1
    row.cleanup_pending = bool(row.staging)
    session.add(
        FeedbackMutationRow(
            actor_user_id=owner,
            operation="export.cancel",
            request_id=key,
            intent_sha256=intent,
            receipt={"export_id": row.id},
        )
    )
    await write_principal_audit(
        session,
        principal=principal,
        action="feedback.export.cancel",
        target_type="feedback_export",
        target_id=row.id,
        context={"cleanup_pending": row.cleanup_pending},
    )
    return export_receipt(row)
