"""Scheduler-minted, node-bound private dataset source grants; secrets are never URLs."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    ModelVariantRow,
    NodeRow,
    TrainingCommandRow,
    TrainingDatasetAnalysisRow,
    TrainingDatasetGrantRow,
    TrainingDatasetRevisionRow,
)
from coire_api.training.authorization import authorize_live_training_action
from coire_api.training.datasets import analysis_base
from coire_core.errors import TrainingConflict, TrainingNotFound, TrainingValidationError
from coire_core.models.auth import UserRole
from coire_core.models.datasets import (
    DatasetAnalysis,
    DatasetAnalysisBinding,
    DatasetAnalysisDispatch,
    DatasetRegistrationCommand,
    SplitManifest,
)
from coire_core.models.training_node import (
    DatasetInputGrant,
    TrainingInputSource,
    TrainingInputsRequest,
    TrainingPrepareRequest,
)
from coire_core.settings import Settings


async def live_analysis_source(
    session: AsyncSession, analysis_id: uuid.UUID
) -> TrainingDatasetRevisionRow:
    analysis = await session.get(TrainingDatasetAnalysisRow, analysis_id, populate_existing=True)
    if analysis is None or analysis.state not in {"queued", "running"}:
        raise TrainingNotFound()
    if (
        analysis.result
        and "dispatch" in analysis.result
        and DatasetAnalysisDispatch.model_validate(analysis.result["dispatch"]).deadline
        <= datetime.now(UTC)
    ):
        raise TrainingNotFound()
    dataset = await session.get(
        TrainingDatasetRevisionRow, analysis.dataset_id, populate_existing=True
    )
    command = await session.get(TrainingCommandRow, analysis.command_id)
    if (
        dataset is None
        or dataset.state not in {"analyzing", "ready", "analysis_failed"}
        or command is None
    ):
        raise TrainingNotFound()
    try:
        payload = DatasetRegistrationCommand.model_validate(command.payload)
    except ValueError:
        raise TrainingNotFound() from None
    if (
        payload.analysis is None
        or payload.analysis.dataset_id != dataset.id
        or payload.analysis.source_sha256 != dataset.source_sha256
        or payload.analysis.split_sha256 != dataset.split_sha256
        or payload.analysis.model_id != analysis.model_id
        or payload.analysis.variant_id != analysis.variant_id
        or payload.analysis.format.value != dataset.format
        or command.subject_id != str(dataset.id)
        or payload.intent.source_sha256 != dataset.source_sha256
        or payload.intent.source_bytes != dataset.source_bytes
        or payload.intent.metadata.analysis_model_id != analysis.model_id
        or payload.intent.metadata.analysis_variant_id != analysis.variant_id
        or payload.intent.metadata.split_seed != dataset.split_seed
        or payload.intent.metadata.validation_fraction != dataset.validation_fraction
    ):
        raise TrainingNotFound()
    principal = Principal(
        kind=PrincipalKind.API_KEY if payload.originating_key_id else PrincipalKind.USER,
        user_id=command.actor_user_id,
        role=UserRole.ADMIN,
        scopes=frozenset({"admin"}) if payload.originating_key_id else frozenset(),
        api_key_id=payload.originating_key_id,
        credential_version=payload.originating_key_version,
    )
    await authorize_live_training_action(session, principal)
    # Identity first, then dataset: match admin management/submission lock order.
    dataset = await session.get(
        TrainingDatasetRevisionRow,
        analysis.dataset_id,
        with_for_update=True,
        populate_existing=True,
    )
    if dataset is None or dataset.state not in {"analyzing", "ready", "analysis_failed"}:
        raise TrainingNotFound()
    try:
        base_sha256 = await analysis_base(session, payload.intent.metadata)
    except TrainingValidationError:
        raise TrainingNotFound() from None
    variant = await session.get(ModelVariantRow, analysis.variant_id)
    if (
        base_sha256 != payload.analysis.base_manifest_sha256
        or variant is None
        or variant.slug != payload.analysis.model_slug
    ):
        raise TrainingNotFound()
    return dataset


async def mint_analysis_grant(
    session: AsyncSession, analysis_id: uuid.UUID, node_id: uuid.UUID, settings: Settings
) -> DatasetInputGrant:
    dataset = await live_analysis_source(session, analysis_id)
    node = await session.get(NodeRow, node_id)
    if (
        node is None
        or node.name not in {"coire-edge-a", "coire-edge-b"}
        or not dataset.source_sha256
        or not dataset.source_bytes
    ):
        raise TrainingNotFound()
    token = secrets.token_urlsafe(32)
    identity = uuid.uuid4()
    expiry = datetime.now(UTC) + timedelta(seconds=settings.training_transfer_grant_s)
    # Do not revoke a still-live credential another executor may have just
    # committed for its idempotent request. Expired grants remain audit metadata.
    for prior in await session.scalars(
        select(TrainingDatasetGrantRow)
        .where(
            TrainingDatasetGrantRow.analysis_id == analysis_id,
            TrainingDatasetGrantRow.revoked_at.is_(None),
            TrainingDatasetGrantRow.expires_at <= datetime.now(UTC),
        )
        .with_for_update()
    ):
        prior.revoked_at = datetime.now(UTC)
    session.add(
        TrainingDatasetGrantRow(
            id=identity,
            secret_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
            node_id=node_id,
            dataset_id=dataset.id,
            analysis_id=analysis_id,
            attempt_id=None,
            source_sha256=dataset.source_sha256,
            max_bytes=dataset.source_bytes,
            expires_at=expiry,
        )
    )
    await session.flush()
    return DatasetInputGrant(
        grant_id=identity,
        node=cast(Literal["coire-edge-a", "coire-edge-b"], node.name),
        dataset_id=dataset.id,
        source_sha256=dataset.source_sha256,
        max_bytes=dataset.source_bytes,
        analysis_id=analysis_id,
        expires_at=expiry,
        secret=token,
    )


async def authorized_source(
    session: AsyncSession, dataset_id: uuid.UUID, node_name: str, secret: str
) -> TrainingDatasetRevisionRow:
    if not 32 <= len(secret) <= 256 or not secret.isascii():
        raise TrainingNotFound()
    digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
    grant = await session.scalar(
        select(TrainingDatasetGrantRow).where(TrainingDatasetGrantRow.secret_hash == digest)
    )
    if (
        grant is None
        or not hmac.compare_digest(grant.secret_hash, digest)
        or grant.dataset_id != dataset_id
        or grant.revoked_at is not None
        or grant.expires_at <= datetime.now(UTC)
    ):
        raise TrainingNotFound()
    node = await session.get(NodeRow, grant.node_id)
    if node is None or node.name != node_name:
        raise TrainingNotFound()
    if grant.attempt_id is not None:
        return await authorized_attempt_source(session, grant)
    if grant.analysis_id is None:
        raise TrainingNotFound()
    dataset = await live_analysis_source(session, grant.analysis_id)
    analysis = await session.get(TrainingDatasetAnalysisRow, grant.analysis_id)
    if (
        analysis is not None
        and analysis.result
        and "dispatch" in analysis.result
        and DatasetAnalysisDispatch.model_validate(analysis.result["dispatch"]).node_id
        != grant.node_id
    ):
        raise TrainingNotFound()
    if dataset.source_sha256 != grant.source_sha256 or dataset.source_bytes != grant.max_bytes:
        raise TrainingNotFound()
    return dataset


async def attempt_sources(
    session: AsyncSession, prepare: TrainingPrepareRequest
) -> list[
    tuple[TrainingDatasetRevisionRow, DatasetAnalysisBinding, SplitManifest, DatasetAnalysis]
]:
    """Recheck the exact persisted participant and immutable inputs under current authority."""
    from coire_api.db import TrainingAttemptRow, TrainingJobRow, TrainingParticipantRow
    from coire_api.training.service import payload_digest, recheck_training_inputs

    job = await session.get(TrainingJobRow, prepare.job_id, populate_existing=True)
    if job is None:
        raise TrainingNotFound()
    await authorize_live_training_action(
        session, Principal.model_validate(job.authorization_snapshot)
    )
    job = await session.get(
        TrainingJobRow, prepare.job_id, populate_existing=True, with_for_update=True
    )
    assert job is not None
    attempt = await session.get(TrainingAttemptRow, prepare.attempt_id, populate_existing=True)
    command = await session.get(TrainingCommandRow, prepare.command_id, populate_existing=True)
    participant = await session.scalar(
        select(TrainingParticipantRow)
        .join(NodeRow, NodeRow.id == TrainingParticipantRow.node_id)
        .where(
            TrainingParticipantRow.attempt_id == prepare.attempt_id,
            NodeRow.name == prepare.node,
            TrainingParticipantRow.rank == prepare.rank,
        )
    )
    now = datetime.now(UTC)
    if (
        job.state not in {"reserving", "running"}
        or job.deleted_at is not None
        or job.fence != prepare.fence
        or attempt is None
        or attempt.job_id != job.id
        or attempt.fence != prepare.fence
        or attempt.state not in {"preparing", "running"}
        or attempt.world_size != prepare.world_size
        or attempt.lease_expires_at <= now
        or prepare.lease_expires_at <= now
        or job.execution_deadline_at <= now
        or participant is None
        or participant.stopped_at is not None
        or participant.command_id != prepare.command_id
        or participant.reservation_id != prepare.reservation_id
        or participant.disk_reservation_id != prepare.disk_reservation_id
        or participant.request_sha256 != prepare.request_sha256
        or command is None
        or command.operation != "node.training.prepare"
        or command.subject_id != prepare.node
        or command.attempt_id != attempt.id
        or command.payload != prepare.model_dump(mode="json")
        or command.request_sha256 != payload_digest(prepare)
        or payload_digest(prepare.resolved) != job.resolved_sha256
    ):
        raise TrainingConflict("Input authority no longer matches the live preparation")
    await recheck_training_inputs(session, prepare.resolved)
    result = []
    for frozen in sorted(prepare.resolved.datasets, key=lambda item: str(item.dataset_id)):
        source = await session.get(TrainingDatasetRevisionRow, frozen.dataset_id)
        analysis_row = await session.get(TrainingDatasetAnalysisRow, frozen.analysis_id)
        assert source is not None and analysis_row is not None
        analysis_command = await session.get(TrainingCommandRow, analysis_row.command_id)
        if analysis_command is None:
            raise TrainingConflict("Frozen analysis binding is unavailable")
        binding = DatasetAnalysisBinding.model_validate(analysis_command.payload.get("analysis"))
        split = SplitManifest.model_validate(source.split_manifest)
        analysis = DatasetAnalysis.model_validate(analysis_row.result)
        if (
            payload_digest(binding) != analysis_row.identity_sha256
            or payload_digest(split) != frozen.split_sha256
            or binding.dataset_id != source.id
            or binding.source_sha256 != frozen.source_sha256
            or binding.split_sha256 != frozen.split_sha256
            or binding.base_manifest_sha256 != prepare.resolved.base_manifest_sha256
            or binding.model_id != job.model_id
            or binding.variant_id != job.base_variant_id
            or binding.enable_thinking != prepare.resolved.enable_thinking
        ):
            raise TrainingConflict("Frozen analysis identity changed")
        result.append((source, binding, split, analysis))
    return result


async def mint_attempt_inputs(
    session: AsyncSession, prepare: TrainingPrepareRequest, settings: Settings
) -> TrainingInputsRequest:
    sources = await attempt_sources(session, prepare)
    node = await session.scalar(select(NodeRow).where(NodeRow.name == prepare.node))
    assert node is not None
    expiry = min(
        prepare.lease_expires_at,
        datetime.now(UTC) + timedelta(seconds=settings.training_transfer_grant_s),
    )
    inputs = []
    for source, binding, split, analysis in sources:
        if source.source_sha256 is None or source.source_bytes <= 0:
            raise TrainingConflict("Ready source has no immutable byte identity")
        token, identity = secrets.token_urlsafe(32), uuid.uuid4()
        session.add(
            TrainingDatasetGrantRow(
                id=identity,
                secret_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
                node_id=node.id,
                dataset_id=source.id,
                attempt_id=prepare.attempt_id,
                source_sha256=source.source_sha256,
                max_bytes=source.source_bytes,
                expires_at=expiry,
            )
        )
        inputs.append(
            TrainingInputSource(
                binding=binding,
                split=split,
                analysis=analysis,
                grant=DatasetInputGrant(
                    grant_id=identity,
                    node=prepare.node,
                    dataset_id=source.id,
                    source_sha256=source.source_sha256,
                    max_bytes=source.source_bytes,
                    attempt_id=prepare.attempt_id,
                    expires_at=expiry,
                    secret=token,
                ),
            )
        )
    await session.flush()
    return TrainingInputsRequest(
        **prepare.model_dump(
            exclude={
                "resolved",
                "reservation_id",
                "disk_reservation_id",
                "resume_checkpoint_id",
                "resume_manifest_sha256",
                "collective",
                "command_id",
            }
        ),
        command_id=uuid.uuid4(),
        sources=inputs,
    )


async def authorized_attempt_source(
    session: AsyncSession, grant: TrainingDatasetGrantRow
) -> TrainingDatasetRevisionRow:
    from coire_api.db import TrainingParticipantRow

    participant = await session.scalar(
        select(TrainingParticipantRow).where(
            TrainingParticipantRow.attempt_id == grant.attempt_id,
            TrainingParticipantRow.node_id == grant.node_id,
        )
    )
    command = await session.get(TrainingCommandRow, participant.command_id) if participant else None
    if command is None:
        raise TrainingNotFound()
    try:
        sources = await attempt_sources(
            session, TrainingPrepareRequest.model_validate(command.payload)
        )
    except (TrainingConflict, ValueError):
        raise TrainingNotFound() from None
    for source, _, _, _ in sources:
        if (
            source.id == grant.dataset_id
            and source.source_sha256 == grant.source_sha256
            and source.source_bytes == grant.max_bytes
        ):
            return source
    raise TrainingNotFound()


async def authorized_input_source(
    session: AsyncSession, dataset_id: uuid.UUID, node_name: str, secret: str
) -> TrainingDatasetRevisionRow:
    """Select grant category before checking authority; a refusal cannot broaden access."""
    if not 32 <= len(secret) <= 256 or not secret.isascii():
        raise TrainingNotFound()
    digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
    grant = await session.scalar(
        select(TrainingDatasetGrantRow.id).where(TrainingDatasetGrantRow.secret_hash == digest)
    )
    if grant is not None:
        return await authorized_source(session, dataset_id, node_name, secret)
    measurement = await session.scalar(
        select(TrainingCommandRow.id).where(
            TrainingCommandRow.operation == "measurement.input.grant",
            TrainingCommandRow.request_sha256 == digest,
        )
    )
    if measurement is None:
        raise TrainingNotFound()
    from coire_api.training.measurements import authorized_measurement_source

    return await authorized_measurement_source(session, dataset_id, node_name, secret)


def open_private_source(settings: Settings, dataset: TrainingDatasetRevisionRow) -> tuple[int, int]:
    if dataset.storage_key != str(dataset.id):
        raise TrainingValidationError("Dataset storage identity differs")
    root = Path(settings.training_dataset_dir)
    descriptors: list[int] = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for component in ("sources", str(dataset.id)):
            descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            descriptors.append(descriptor)
        source = os.open("source.jsonl", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        descriptors.append(source)
        info = os.fstat(source)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size != dataset.source_bytes
        ):
            raise TrainingNotFound()
        with os.fdopen(os.dup(source), "rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != dataset.source_sha256:
                raise TrainingNotFound()
        os.lseek(source, 0, os.SEEK_SET)
        descriptors.pop()
        return source, dataset.source_bytes
    except OSError:
        raise TrainingNotFound() from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
