"""Publish an entire verified image batch through one fenced database transaction."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.auth import Principal, PrincipalKind, audit_actor
from coire_api.db import (
    ApiKeyRow,
    ImageExecutionLeaseRow,
    ImageJobEventRow,
    ImageJobRow,
    ImageOutputRow,
    ImageTransferRow,
    ModelRow,
    NodeRow,
)
from coire_api.images.authorization import authorize_live_image_action
from coire_api.images.input_references import release_image_input_references
from coire_api.images.jobs import _policy
from coire_api.images.maintenance import purge_terminal_transfer_staging
from coire_api.images.quota import _QUOTA_LOCK, release_storage_hold, settle_storage_hold
from coire_api.images.transfer import _verify_staged
from coire_core.errors import ImageConflict, ImageForbidden, ImageStorageUnavailable
from coire_core.models.audit import AuditOutcome
from coire_core.models.image_worker import ImageTransferReceipt
from coire_core.models.images import (
    ImageContentTag,
    ImageJobEvent,
    ImageJobState,
    ImageOutput,
    ImageRecipe,
    ResolvedImageSpec,
)
from coire_core.models.registry import (
    AUXILIARY_IMAGE_KINDS,
    EngineBackend,
    ModelKind,
    ModelSource,
    ModelState,
    Visibility,
)
from coire_core.settings import Settings


class ImagePublicationRevoked(ImageConflict):
    """A pinned registry manifest was retired after execution began."""


async def _publication_principal(session: AsyncSession, row: ImageJobRow) -> Principal:
    """Rebuild the originating identity from current key state, never from a saved scope."""
    if row.originating_key_id is None:
        return Principal(kind=PrincipalKind.USER, user_id=row.owner_user_id)
    key = await session.get(
        ApiKeyRow, row.originating_key_id, populate_existing=True, with_for_update=True
    )
    if (
        key is None
        or key.user_id != row.owner_user_id
        or key.revoked_at is not None
        or key.credential_version != row.originating_key_version
    ):
        raise ImageForbidden()
    return Principal(
        kind=PrincipalKind.API_KEY,
        user_id=row.owner_user_id,
        api_key_id=key.id,
        credential_version=key.credential_version,
        scopes=frozenset(key.scopes),
    )


async def _verify_current_models(session: AsyncSession, resolved: ResolvedImageSpec) -> None:
    """A ready receipt cannot publish after its registry manifests are retired."""
    base = await session.get(ModelRow, resolved.spec.model_id)
    if (
        base is None
        or base.kind != ModelKind.IMAGE_MODEL
        or base.backend != EngineBackend.MFLUX
        or base.source != ModelSource.STUDIO
        or base.state != ModelState.READY
        or base.visibility != Visibility.PUBLISHED
        or base.manifest_sha256 != resolved.model_sha256
    ):
        raise ImagePublicationRevoked("image base changed before publication")
    for dependency in resolved.dependencies:
        asset = await session.get(ModelRow, dependency.model_id)
        if (
            asset is None
            or asset.kind not in AUXILIARY_IMAGE_KINDS
            or asset.backend != EngineBackend.AUXILIARY
            or asset.source != ModelSource.STUDIO
            or asset.state != ModelState.READY
            or asset.manifest_sha256 != dependency.sha256
        ):
            raise ImagePublicationRevoked("image dependency changed before publication")


async def publish_image_batch(session: AsyncSession, job_id: str, settings: Settings) -> bool:
    """Make all outputs visible only after durable receipts and node scratch cleanup.

    Transfer staging files become immutable private blobs when the output rows commit.
    No filesystem rename can leave a partly published batch after a crash.
    """
    await session.execute(_QUOTA_LOCK)
    row = await session.get(ImageJobRow, job_id, populate_existing=True, with_for_update=True)
    if row is None or row.state == ImageJobState.SUCCEEDED:
        return False
    if (
        row.state != ImageJobState.TRANSFERRING
        or row.cancel_requested_at is not None
        or row.receipt_state != "complete"
        or row.cleanup_state != "cleaned"
        or row.selected_node_id is None
        or row.instance_id is None
        or row.fence < 1
    ):
        raise ImageConflict("image publication is not ready")
    snapshot, required, explicit = _policy(row)
    resolved = snapshot.resolved
    if resolved is None:
        raise ImageConflict("image runtime is not bound")
    await _verify_current_models(session, resolved)
    principal = await _publication_principal(session, row)
    await authorize_live_image_action(
        session, principal, explicit=explicit, required_entitlements=required
    )
    node = await session.get(NodeRow, row.selected_node_id)
    if node is None:
        raise ImageConflict("image node is unavailable")
    active_leases = (
        await session.scalars(
            select(ImageExecutionLeaseRow)
            .where(
                ImageExecutionLeaseRow.job_id == job_id,
                ImageExecutionLeaseRow.released_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    if len(active_leases) != 1:
        raise ImageConflict("image execution lease is unavailable")
    lease = active_leases[0]
    if (
        lease.mode != "image"
        or lease.node_id != row.selected_node_id
        or lease.fence != row.fence
        or lease.expires_at.tzinfo is None
        or lease.expires_at <= datetime.now(UTC)
    ):
        raise ImageConflict("image execution lease differs from attempt")
    transfers = (
        await session.scalars(
            select(ImageTransferRow)
            .where(ImageTransferRow.job_id == job_id, ImageTransferRow.attempt == row.attempt)
            .order_by(ImageTransferRow.output_index)
            .with_for_update()
        )
    ).all()
    if len(transfers) != resolved.spec.n or [item.output_index for item in transfers] != list(
        range(resolved.spec.n)
    ):
        raise ImageConflict("image transfer batch is incomplete")
    if (
        await session.scalar(
            select(ImageOutputRow.id).where(ImageOutputRow.job_id == job_id).limit(1)
        )
        is not None
    ):
        raise ImageConflict("image output batch already exists")

    verified: list[tuple[ImageTransferRow, ImageTransferReceipt, ImageRecipe]] = []
    total_bytes = 0
    for transfer in transfers:
        if transfer.receipt is None or transfer.node_cleanup_ack_at is None:
            raise ImageConflict("image transfer cleanup is incomplete")
        try:
            receipt = ImageTransferReceipt.model_validate(transfer.receipt)
        except ValueError as exc:
            raise ImageConflict("image transfer receipt is invalid") from exc
        if (
            receipt.job_id != job_id
            or receipt.attempt != row.attempt
            or receipt.fence != row.fence
            or receipt.node != node.name
            or receipt.index != transfer.output_index
            or receipt.byte_count != transfer.expected_bytes
            or receipt.sha256 != transfer.expected_sha256
            or transfer.staging_key
            != f"image-staging/{job_id}/{row.attempt}/{transfer.output_index}.png"
        ):
            raise ImageConflict("image transfer receipt differs from attempt")
        recipe = await asyncio.to_thread(
            _verify_staged, Path(settings.image_blob_root), transfer, receipt
        )
        if recipe.resolved != resolved or recipe.output_index != receipt.index:
            raise ImageStorageUnavailable()
        verified.append((transfer, receipt, recipe))
        total_bytes += receipt.byte_count

    held = row.authorization_snapshot.get("output_hold_bytes")
    if not isinstance(held, int) or isinstance(held, bool) or total_bytes > held:
        raise ImageConflict("image capacity hold unavailable")
    latest = await session.scalar(
        select(func.max(ImageJobEventRow.sequence)).where(ImageJobEventRow.job_id == job_id)
    )
    if latest is None or latest < 1:
        raise ImageConflict("image event history unavailable")
    now = datetime.now(UTC)
    published_outputs: list[ImageOutput] = []
    for transfer, receipt, recipe in verified:
        classification = receipt.classification
        tag = (
            ImageContentTag.EXPLICIT
            if explicit
            else classification.tag
            if classification is not None
            else ImageContentTag.UNKNOWN
        )
        provenance = (
            classification.model_dump(mode="json")
            if classification is not None
            else {"status": "unavailable"}
        )
        diagnostic = (
            classification.safe_error if classification is not None else "classifier_unavailable"
        )
        session.add(
            ImageOutputRow(
                id=receipt.output_id,
                job_id=job_id,
                owner_user_id=row.owner_user_id,
                output_index=receipt.index,
                blob_key=transfer.staging_key,
                size_bytes=receipt.byte_count,
                file_sha256=receipt.sha256,
                pixel_sha256=recipe.pixel_sha256,
                recipe=recipe.model_dump(mode="json"),
                content_tag=tag,
                classifier_provenance=provenance,
                entitlement_snapshot={"required_entitlements": sorted(required)},
                state="published",
                created_at=now,
                published_at=now,
            )
        )
        published_outputs.append(
            ImageOutput(
                id=receipt.output_id,
                job_id=job_id,
                index=receipt.index,
                recipe=recipe,
                tag=tag,
                classifier_diagnostic=diagnostic,
                byte_count=receipt.byte_count,
                file_sha256=receipt.sha256,
                created_at=now,
            )
        )
        transfer.state = "published"
    await settle_storage_hold(session, row.owner_user_id, held, total_bytes)
    await release_image_input_references(session, row)
    lease.released_at = now
    lease.release_evidence = {
        "state": "succeeded",
        "node": node.name,
        "scratch_cleaned": True,
        "receipts": len(verified),
    }
    row.state = ImageJobState.SUCCEEDED
    row.progress = 1.0
    row.receipt_state = "complete"
    row.cleanup_state = "cleaned"
    row.updated_at = now
    row.finished_at = now
    row.version += 1
    event = ImageJobEvent(
        job_id=job_id,
        sequence=latest + 1,
        at=now,
        type="done",
        state=ImageJobState.SUCCEEDED,
        outputs=published_outputs,
    )
    session.add(
        ImageJobEventRow(
            job_id=job_id,
            sequence=event.sequence,
            event_type=event.type,
            payload=event.model_dump(mode="json"),
            created_at=now,
        )
    )
    actor, actor_type, actor_user_id = audit_actor(principal)
    await write_audit(
        session,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        action="image.complete",
        target_type="image_job",
        target_id=job_id,
        outcome=AuditOutcome.OK,
        context={
            "explicit": explicit,
            "required_entitlements": sorted(required),
            "output_count": len(published_outputs),
        },
    )
    return True


async def fail_revoked_image_batch(
    session: AsyncSession, job_id: str, settings: Settings, *, safe_code: str
) -> bool:
    """Delete private staging before releasing a started job's storage hold."""
    if safe_code not in {"authorization_revoked", "model_unavailable"}:
        raise ImageConflict("invalid image failure code")
    await session.execute(_QUOTA_LOCK)
    row = await session.get(ImageJobRow, job_id, populate_existing=True, with_for_update=True)
    if row is None or row.state in {ImageJobState.SUCCEEDED, ImageJobState.FAILED}:
        return False
    if (
        row.state != ImageJobState.TRANSFERRING
        or row.cancel_requested_at is not None
        or row.receipt_state != "complete"
        or row.cleanup_state != "cleaned"
        or row.selected_node_id is None
        or row.instance_id is None
        or row.fence < 1
    ):
        raise ImageConflict("image failure cleanup is not ready")
    if (
        await session.scalar(
            select(ImageOutputRow.id).where(ImageOutputRow.job_id == job_id).limit(1)
        )
        is not None
    ):
        raise ImageConflict("image publication already won")
    leases = (
        await session.scalars(
            select(ImageExecutionLeaseRow)
            .where(
                ImageExecutionLeaseRow.job_id == job_id,
                ImageExecutionLeaseRow.released_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    if len(leases) != 1:
        raise ImageConflict("image execution lease is unavailable")
    lease = leases[0]
    if lease.mode != "image" or lease.node_id != row.selected_node_id or lease.fence != row.fence:
        raise ImageConflict("image execution lease differs from attempt")
    transfers = (
        await session.scalars(
            select(ImageTransferRow)
            .where(ImageTransferRow.job_id == job_id, ImageTransferRow.attempt == row.attempt)
            .with_for_update()
        )
    ).all()
    snapshot, _, _ = _policy(row)
    if (
        snapshot.resolved is None
        or len(transfers) != snapshot.resolved.spec.n
        or any(item.node_cleanup_ack_at is None for item in transfers)
    ):
        raise ImageConflict("image transfer cleanup is incomplete")
    held = row.authorization_snapshot.get("output_hold_bytes")
    if not isinstance(held, int) or isinstance(held, bool) or held <= 0:
        raise ImageConflict("image capacity hold unavailable")
    latest = await session.scalar(
        select(func.max(ImageJobEventRow.sequence)).where(ImageJobEventRow.job_id == job_id)
    )
    if latest is None or latest < 1:
        raise ImageConflict("image event history unavailable")
    await asyncio.to_thread(
        purge_terminal_transfer_staging,
        Path(settings.image_blob_root),
        job_id,
        row.attempt,
    )
    await release_storage_hold(session, row.owner_user_id, held)
    await release_image_input_references(session, row)
    now = datetime.now(UTC)
    for transfer in transfers:
        transfer.state = "failed"
        transfer.lease_expires_at = now
    lease.released_at = now
    lease.release_evidence = {
        "state": "failed",
        "reason": safe_code,
        "node_cleanup_ack": True,
    }
    row.state = ImageJobState.FAILED
    row.safe_failure_code = safe_code
    row.receipt_state = "none"
    row.cleanup_state = "cleaned"
    row.updated_at = now
    row.finished_at = now
    row.version += 1
    event = ImageJobEvent(
        job_id=job_id,
        sequence=latest + 1,
        at=now,
        type="error",
        state=ImageJobState.FAILED,
        safe_code=safe_code,
    )
    session.add(
        ImageJobEventRow(
            job_id=job_id,
            sequence=event.sequence,
            event_type=event.type,
            payload=event.model_dump(mode="json"),
            created_at=now,
        )
    )
    await write_audit(
        session,
        actor="coire-scheduler",
        action="image.publish.refused",
        target_type="image_job",
        target_id=job_id,
        outcome=AuditOutcome.REFUSED,
        context={"reason": safe_code},
    )
    return True
