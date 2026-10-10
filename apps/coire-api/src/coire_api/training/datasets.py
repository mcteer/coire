"""Transactional dataset registration and bounded administrator projections."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import (
    ModelRow,
    ModelVariantRow,
    NodeRow,
    TrainingDatasetAnalysisRow,
    TrainingDatasetReferenceRow,
    TrainingDatasetRevisionRow,
    TrainingJobRow,
    TrainingStorageReservationRow,
    VariantCopyRow,
)
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.service import begin_command, record_receipt
from coire_api.training.storage import DatasetStore, StagedDataset
from coire_core.errors import TrainingConflict, TrainingNotFound, TrainingValidationError
from coire_core.models.acquisition import VariantState
from coire_core.models.datasets import (
    DatasetAnalysisBinding,
    DatasetAnalysisReceipt,
    DatasetAnalyzeRequest,
    DatasetCursor,
    DatasetDeleteRequest,
    DatasetDeletionReceipt,
    DatasetDetail,
    DatasetDiagnostic,
    DatasetFormat,
    DatasetPage,
    DatasetProvenance,
    DatasetReceipt,
    DatasetRegistrationCommand,
    DatasetState,
    DatasetUploadIntent,
    DatasetUploadRequest,
)
from coire_core.models.preference import PreferenceSplitManifest
from coire_core.models.registry import ModelState

tracer = trace.get_tracer("coire.api.training.datasets")


def analysis_identity(binding: DatasetAnalysisBinding) -> str:
    return hashlib.sha256(
        json.dumps(
            binding.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def upload_identity(owner: uuid.UUID, key: str) -> uuid.UUID:
    if not 1 <= len(key) <= 128 or len(key.encode()) > 512 or any(ord(char) < 32 for char in key):
        raise TrainingValidationError("Idempotency key must be a bounded opaque value")
    return uuid.uuid5(owner, "dataset-upload:" + hashlib.sha256(key.encode()).hexdigest())


async def analysis_base(session: AsyncSession, metadata: DatasetUploadRequest) -> str:
    model = await session.get(
        ModelRow, metadata.analysis_model_id, populate_existing=True, with_for_update=True
    )
    variant = await session.get(
        ModelVariantRow, metadata.analysis_variant_id, populate_existing=True, with_for_update=True
    )
    if (
        model is None
        or variant is None
        or variant.model_id != model.id
        or model.state is not ModelState.READY
        or variant.state is not VariantState.READY
        or model.source != "studio"
        or model.kind != "language_model"
        or model.backend != "mlx_lm"
        or variant.backend != "mlx_lm"
    ):
        raise TrainingValidationError(
            "Dataset analysis requires a ready registered local text variant"
        )
    copies = (
        await session.execute(
            select(NodeRow.name, VariantCopyRow.manifest_sha256)
            .join(
                VariantCopyRow,
                VariantCopyRow.node_id == NodeRow.id,
            )
            .where(VariantCopyRow.variant_id == variant.id, VariantCopyRow.verified.is_(True))
        )
    ).all()
    selected = {name: digest for name, digest in copies if name in {"coire-edge-a", "coire-edge-b"}}
    if (
        set(selected) != {"coire-edge-a", "coire-edge-b"}
        or len(set(selected.values())) != 1
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in selected.values()
        )
    ):
        raise TrainingValidationError(
            "Dataset tokenizer variant needs matching verified Studio copies"
        )
    return str(selected["coire-edge-a"])


async def register_source(
    session: AsyncSession,
    principal: Principal,
    metadata: DatasetUploadRequest,
    result: StagedDataset,
    *,
    dataset_id: uuid.UUID,
    hold_id: uuid.UUID,
    key: str,
) -> tuple[DatasetReceipt, bool]:
    """Register intent before filesystem publication; caller commits both or cleans its orphan.

    Returned bool distinguishes a new registration from an immutable command replay.
    The caller keeps its quota hold until source/spool cleanup is actually proved.
    """
    owner = await authorize_live_training_action(session, principal)
    if upload_identity(owner, key) != dataset_id:
        raise TrainingValidationError(
            "Dataset upload identity differs from its authenticated actor"
        )
    hold = await session.get(TrainingStorageReservationRow, hold_id, with_for_update=True)
    if (
        hold is None
        or hold.owner_user_id != owner
        or hold.subject_id != str(dataset_id)
        or hold.state != "held"
    ):
        raise TrainingValidationError("Dataset upload has no matching pre-body storage hold")
    intent = DatasetUploadIntent(
        metadata=metadata, source_sha256=result.source_sha256, source_bytes=result.source_bytes
    )
    command = await begin_command(
        session,
        principal,
        operation="dataset.upload",
        subject_id=str(dataset_id),
        idempotency_key=key,
        request=intent,
    )
    if command.receipt is not None:
        return DatasetReceipt.model_validate(command.receipt), False
    base_sha256 = await analysis_base(session, metadata)
    if not result.invalid_count and (result.split is None or result.split_sha256 is None):
        raise TrainingValidationError("Dataset upload requires a complete validated split")
    if result.split is not None and (
        (metadata.format is DatasetFormat.PREFERENCE)
        != isinstance(result.split, PreferenceSplitManifest)
    ):
        raise TrainingValidationError("Dataset format differs from its versioned split")
    state = DatasetState.FAILED if result.invalid_count else DatasetState.ANALYZING
    row = TrainingDatasetRevisionRow(
        id=dataset_id,
        owner_user_id=owner,
        name=metadata.name,
        format=metadata.format.value,
        state=state.value,
        source_sha256=result.source_sha256,
        source_bytes=result.source_bytes,
        storage_key=str(dataset_id),
        provenance=metadata.provenance.model_dump(mode="json"),
        row_count=result.row_count,
        split_seed=metadata.split_seed,
        validation_fraction=metadata.validation_fraction,
        split_manifest=result.split.model_dump(mode="json") if result.split else None,
        split_sha256=result.split_sha256,
        version=1,
        invalid_count=result.invalid_count,
        diagnostics=[item.model_dump(mode="json") for item in result.diagnostics],
        safe_failure_code="invalid_rows" if result.invalid_count else None,
    )
    session.add(row)
    await session.flush()
    if result.split is not None:
        model = await session.get(ModelRow, metadata.analysis_model_id)
        variant = await session.get(ModelVariantRow, metadata.analysis_variant_id)
        assert model is not None
        assert variant is not None
        binding = DatasetAnalysisBinding(
            dataset_id=dataset_id,
            model_id=model.id,
            variant_id=metadata.analysis_variant_id,
            base_manifest_sha256=base_sha256,
            source_sha256=result.source_sha256,
            split_sha256=result.split_sha256 or "",
            format=metadata.format,
            model_slug=variant.slug,
            template_override=model.chat_template,
        )
        command.payload = DatasetRegistrationCommand(
            intent=intent,
            analysis=binding,
            originating_key_id=principal.api_key_id,
            originating_key_version=principal.credential_version if principal.api_key_id else None,
        ).model_dump(mode="json")
        identity = analysis_identity(binding)
        session.add(
            TrainingDatasetAnalysisRow(
                id=uuid.uuid4(),
                dataset_id=dataset_id,
                model_id=metadata.analysis_model_id,
                variant_id=metadata.analysis_variant_id,
                command_id=command.id,
                identity_sha256=identity,
                state="queued",
            )
        )
    receipt = DatasetReceipt(dataset_id=dataset_id, state=state, version=1)
    await record_receipt(session, principal, command, receipt)
    return receipt, True


async def project_dataset(session: AsyncSession, row: TrainingDatasetRevisionRow) -> DatasetDetail:
    analysis = await session.scalar(
        select(TrainingDatasetAnalysisRow.id)
        .where(
            TrainingDatasetAnalysisRow.dataset_id == row.id,
        )
        .order_by(
            TrainingDatasetAnalysisRow.created_at.desc(), TrainingDatasetAnalysisRow.id.desc()
        )
        .limit(1)
    )
    return DatasetDetail(
        warnings=["small_sample"] if row.format == "preference" and 0 < row.row_count < 20 else [],
        id=row.id,
        name=row.name,
        format=DatasetFormat(row.format),
        state=DatasetState(row.state),
        provenance=DatasetProvenance.model_validate(row.provenance),
        source_sha256=row.source_sha256,
        source_bytes=row.source_bytes,
        row_count=row.row_count,
        split_seed=row.split_seed,
        split_manifest_sha256=row.split_sha256,
        analysis_id=analysis,
        invalid_count=row.invalid_count,
        diagnostics=[DatasetDiagnostic.model_validate(item) for item in row.diagnostics],
        version=row.version,
        created_at=row.created_at,
    )


async def dataset_detail(
    session: AsyncSession, principal: Principal, identity: uuid.UUID
) -> DatasetDetail:
    await authorize_live_training_action(session, principal)
    row = await session.get(TrainingDatasetRevisionRow, identity)
    if row is None:
        raise TrainingNotFound()
    return await project_dataset(session, row)


async def dataset_page(
    session: AsyncSession, principal: Principal, *, cursor: str | None, limit: int
) -> DatasetPage:
    await authorize_live_training_action(session, principal)
    if not 1 <= limit <= 100:
        raise TrainingValidationError("Dataset page limit exceeds its bound")
    query = (
        select(TrainingDatasetRevisionRow)
        .order_by(TrainingDatasetRevisionRow.created_at, TrainingDatasetRevisionRow.id)
        .limit(limit + 1)
    )
    if cursor:
        try:
            value = DatasetCursor.model_validate_json(
                base64.b64decode(cursor.encode(), altchars=b"-_", validate=True)
            )
        except (ValueError, UnicodeError):
            raise TrainingValidationError("Invalid dataset page cursor") from None
        query = query.where(
            or_(
                TrainingDatasetRevisionRow.created_at > value.created_at,
                and_(
                    TrainingDatasetRevisionRow.created_at == value.created_at,
                    TrainingDatasetRevisionRow.id > value.id,
                ),
            )
        )
    rows = list(await session.scalars(query))
    next_cursor = (
        base64.urlsafe_b64encode(
            DatasetCursor(created_at=rows[limit - 1].created_at, id=rows[limit - 1].id)
            .model_dump_json()
            .encode()
        ).decode()
        if len(rows) > limit
        else None
    )
    return DatasetPage(
        items=[await project_dataset(session, row) for row in rows[:limit]], next_cursor=next_cursor
    )


async def analyze_dataset(
    session: AsyncSession,
    principal: Principal,
    identity: uuid.UUID,
    request: DatasetAnalyzeRequest,
    key: str,
) -> DatasetAnalysisReceipt:
    command = await begin_command(
        session,
        principal,
        operation="dataset.analyze",
        subject_id=str(identity),
        idempotency_key=key,
        request=request,
    )
    if command.receipt is not None:
        return DatasetAnalysisReceipt.model_validate(command.receipt)
    row = await session.get(TrainingDatasetRevisionRow, identity, with_for_update=True)
    if row is None:
        raise TrainingNotFound()
    if (
        row.state not in {"ready", "analysis_failed", "analyzing"}
        or row.split_sha256 is None
        or row.source_sha256 is None
    ):
        raise TrainingConflict("Dataset has no retained validated source to analyze")
    if await session.scalar(
        select(TrainingDatasetAnalysisRow.id)
        .where(
            TrainingDatasetAnalysisRow.dataset_id == identity,
            TrainingDatasetAnalysisRow.state.in_(("queued", "running")),
        )
        .limit(1)
    ):
        raise TrainingConflict("Dataset already has an active analysis")
    metadata = DatasetUploadRequest(
        name=row.name,
        format=DatasetFormat(row.format),
        provenance=DatasetProvenance.model_validate(row.provenance),
        analysis_model_id=request.model_id,
        analysis_variant_id=request.variant_id,
        split_seed=row.split_seed,
        validation_fraction=row.validation_fraction,
    )
    base_sha256 = await analysis_base(session, metadata)
    model = await session.get(ModelRow, request.model_id)
    variant = await session.get(ModelVariantRow, request.variant_id)
    assert model is not None and variant is not None
    binding = DatasetAnalysisBinding(
        dataset_id=identity,
        model_id=model.id,
        variant_id=variant.id,
        base_manifest_sha256=base_sha256,
        source_sha256=row.source_sha256,
        split_sha256=row.split_sha256,
        format=metadata.format,
        model_slug=variant.slug,
        template_override=model.chat_template,
    )
    command.payload = DatasetRegistrationCommand(
        intent=DatasetUploadIntent(
            metadata=metadata, source_sha256=row.source_sha256, source_bytes=row.source_bytes
        ),
        analysis=binding,
        originating_key_id=principal.api_key_id,
        originating_key_version=principal.credential_version if principal.api_key_id else None,
    ).model_dump(mode="json")
    analysis = TrainingDatasetAnalysisRow(
        id=uuid.uuid4(),
        dataset_id=identity,
        model_id=model.id,
        variant_id=variant.id,
        command_id=command.id,
        identity_sha256=analysis_identity(binding),
        state="queued",
    )
    session.add(analysis)
    row.state = "analyzing"
    row.version += 1
    receipt = DatasetAnalysisReceipt(analysis_id=analysis.id, state="queued")
    await record_receipt(session, principal, command, receipt)
    return receipt


async def retire_dataset(
    session: AsyncSession,
    principal: Principal,
    identity: uuid.UUID,
    request: DatasetDeleteRequest,
    key: str,
) -> DatasetDeletionReceipt:
    command = await begin_command(
        session,
        principal,
        operation="dataset.delete",
        subject_id=str(identity),
        idempotency_key=key,
        request=request,
    )
    if command.receipt is not None:
        return DatasetDeletionReceipt.model_validate(command.receipt)
    row = await session.get(TrainingDatasetRevisionRow, identity, with_for_update=True)
    if row is None:
        raise TrainingNotFound()
    if row.version != request.expected_version:
        raise TrainingConflict("Dataset version changed; reload its current state")
    live_reference = await session.scalar(
        select(TrainingDatasetReferenceRow.job_id)
        .join(TrainingJobRow, TrainingJobRow.id == TrainingDatasetReferenceRow.job_id)
        .where(
            TrainingDatasetReferenceRow.dataset_id == identity,
            TrainingJobRow.state.not_in(("succeeded", "failed", "cancelled")),
        )
        .limit(1)
    )
    active_analysis = await session.scalar(
        select(TrainingDatasetAnalysisRow.id)
        .where(
            TrainingDatasetAnalysisRow.dataset_id == identity,
            TrainingDatasetAnalysisRow.state.in_(("queued", "running")),
        )
        .limit(1)
    )
    if live_reference or active_analysis:
        raise TrainingConflict("Dataset is pinned by an active analysis or resumable training job")
    if row.state not in {"retired", "purged"}:
        row.state = "retired"
        row.version += 1
    receipt = DatasetDeletionReceipt(
        dataset_id=identity,
        state="purged" if row.state == "purged" else "retired",
        version=row.version,
    )
    await record_receipt(session, principal, command, receipt)
    return receipt


async def purge_retired_dataset(
    session: AsyncSession, identity: uuid.UUID, store: DatasetStore
) -> None:
    """Retirement is durable first; cleanup failure leaves all disk bytes counted.

    Only terminal analyses (whose node receipt proves private cache cleanup) can
    be retired. Source and split identities remain as historical provenance.
    """
    row = await session.get(TrainingDatasetRevisionRow, identity, with_for_update=True)
    if row is None or row.state != "retired":
        return
    await store.purge_source(identity)
    for hold in await session.scalars(
        select(TrainingStorageReservationRow)
        .where(
            TrainingStorageReservationRow.subject_id == str(identity),
            TrainingStorageReservationRow.node_id.is_(None),
            TrainingStorageReservationRow.state.in_(("retained", "releasing", "held")),
        )
        .with_for_update()
    ):
        # A held/releasing upload may still have a request spool/staging files.
        # Purging a source alone is not proof that its entire hold is free.
        if hold.state != "retained":
            continue
        hold.state = "released"
        hold.released_at = datetime.now(UTC)
    row.state = "purged"
    row.purged_at = datetime.now(UTC)
    row.version += 1
    # Keep terminal lineage readable, but make loss of its source explicit.
    for job in await session.scalars(
        select(TrainingJobRow)
        .join(TrainingDatasetReferenceRow, TrainingDatasetReferenceRow.job_id == TrainingJobRow.id)
        .where(TrainingDatasetReferenceRow.dataset_id == identity)
        .with_for_update()
    ):
        job.reproducible = False
