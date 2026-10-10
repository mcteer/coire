"""Export lifecycle admission; only identities/status cross workflow boundaries."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import anyio
from sqlalchemy import Text, func, select, text
from sqlalchemy import cast as sql_cast

from coire_api.audit import write_audit, write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    ChatConversationRow,
    ComparisonPairRow,
    FeedbackRow,
    PreferenceExportMemberRow,
    PreferenceExportRow,
    TrainingDatasetRevisionRow,
    session_scope,
)
from coire_api.feedback.eligibility import authorize_admin, lock_owners
from coire_api.feedback.export_selection import (
    ExportSource,
    materialize_source,
    select_export_sources,
)
from coire_api.feedback.exports import allocate_export_stage, export_metadata
from coire_api.training.datasets import register_source
from coire_api.training.quota import finish_upload_hold
from coire_api.training.storage import DatasetStore, StagedDataset
from coire_core.errors import (
    FeedbackConflict,
    FeedbackForbidden,
    FeedbackQuotaExceeded,
    TrainingQuotaExceeded,
    TrainingUploadTooLarge,
    TrainingValidationError,
)
from coire_core.models.feedback import PreferenceExportCreate
from coire_core.models.preference import canonical_bytes
from coire_core.settings import Settings


async def prepare_export(identity: str, settings: Settings) -> str:
    """Claim one admitted export, or terminalize metadata-only refusals."""
    async with session_scope() as session:
        export = await session.get(PreferenceExportRow, identity)
        if export is None:
            return "missing"
        principal = Principal.model_validate(export.authorization_snapshot)
        try:
            await authorize_admin(session, principal)
        except FeedbackForbidden:
            export = await session.get(
                PreferenceExportRow, identity, populate_existing=True, with_for_update=True
            )
            assert export is not None
            if export.state == "queued":
                export.state, export.terminal_reason = "failed", "unauthorized"
                export.version += 1
            await session.commit()
            return export.state
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('feedback.export.queue', 0))")
        )
        export = await session.get(
            PreferenceExportRow, identity, populate_existing=True, with_for_update=True
        )
        assert export is not None
        if export.state != "queued":
            return export.state
        now = datetime.now(UTC)
        if export.queue_deadline_at <= now:
            export.state, export.terminal_reason = "failed", "timeout"
        else:
            active = await session.scalar(
                select(func.count())
                .select_from(PreferenceExportRow)
                .where(PreferenceExportRow.state.in_(["staging", "publishing"]))
            )
            if active:
                return "queued"
            # Disabling admission does not strand already accepted commands.
            body = PreferenceExportCreate.model_validate(export.request)
            sources, count = await select_export_sources(session, body)
            export.matched_count = count
            export.selected_count = count if count <= 10000 else 0
            if count == 0 or count > 10000:
                export.state = "failed"
                export.terminal_reason = "empty" if count == 0 else "oversize"
                export.excluded_count = count if count > 10000 else 0
            else:
                export.state = "staging"
                export.fence += 1
                export.execution_deadline_at = now + timedelta(minutes=5)
                export.warnings = ["small_sample"] if len(sources) < 20 else []
        export.version += 1
        await session.commit()
        return export.state


async def cleanup_export(identity: str, store: DatasetStore) -> bool:
    """Prove publication status before erasing any possibly committed source."""
    async with session_scope() as session:
        export = await session.get(PreferenceExportRow, identity, with_for_update=True)
        if export is None or not export.staging:
            return True
        stage_id = uuid.UUID(str(export.staging["hold_id"]))
        dataset_id = uuid.UUID(str(export.staging["dataset_id"]))
        dataset = await session.get(TrainingDatasetRevisionRow, dataset_id)
        if dataset is not None:
            # Atomic final publication records both identities. An inconsistent
            # committed registration remains counted for explicit reconciliation.
            if export.dataset_id != dataset_id:
                export.cleanup_pending = True
                return False
            await store.discard(stage_id)
            export.staging = {}
            export.cleanup_pending = False
            return True
        await store.discard(stage_id)
        await store.purge_source(dataset_id)
        await finish_upload_hold(
            session, stage_id, retained_bytes=0, private_staging_cleanup_proven=True
        )
        export.staging = {}
        export.cleanup_pending = False
        return True


async def fail_export(identity: str, reason: str, store: DatasetStore) -> str:
    # A database read failure propagates before cleanup; uncertainty keeps bytes
    # and reservation ownership for recovery.
    await cleanup_export(identity, store)
    async with session_scope() as session:
        export = await session.get(PreferenceExportRow, identity, with_for_update=True)
        assert export is not None
        if export.dataset_id is not None:
            return export.state
        if export.state != "cancelled":
            export.state, export.terminal_reason = "failed", reason
            export.version += 1
            await write_audit(
                session,
                actor="coire-scheduler",
                action="feedback.export.fail",
                target_type="feedback_export",
                target_id=identity,
                context={"state": "failed", "reason": reason},
            )
        return export.state


async def stage_export(
    identity: str,
    settings: Settings,
    store: DatasetStore,
) -> tuple[list[ExportSource], list[PreferenceExportMemberRow], StagedDataset, int]:
    async with session_scope() as session:
        export = await session.get(PreferenceExportRow, identity)
        assert export is not None
        principal = Principal.model_validate(export.authorization_snapshot)
        await authorize_admin(session, principal)
        export = await session.get(
            PreferenceExportRow, identity, populate_existing=True, with_for_update=True
        )
        assert export is not None
        if export.state != "staging":
            raise FeedbackConflict("Export is no longer staging")
        body = PreferenceExportCreate.model_validate(export.request)
        sources, count = await select_export_sources(session, body)
        export.excluded_count = max(export.matched_count - count, 0)
        export.matched_count = max(export.matched_count, count)
        export.selected_count = count if count <= 10000 else 0
        if not sources or count > 10000:
            await session.commit()
            raise FeedbackConflict("Export source selection changed")
        # PostgreSQL JSON rendering includes delimiters and escaping. The fixed
        # per-row allowance bounds canonical key overhead without reading bodies.
        size = await session.scalar(
            select(
                func.sum(
                    func.octet_length(sql_cast(ComparisonPairRow.prompt, Text))
                    + func.octet_length(sql_cast(func.to_jsonb(ComparisonPairRow.original), Text))
                    + func.octet_length(sql_cast(func.to_jsonb(ComparisonPairRow.candidate), Text))
                    + 64
                )
            ).where(ComparisonPairRow.id.in_([source.pair_id for source in sources]))
        )
        if size is None or size > 256 * 1024**2:
            raise TrainingUploadTooLarge()
        hold = await allocate_export_stage(
            session, principal, export, settings, declared_bytes=int(size)
        )
        stage_id = hold.id
        dataset_id = uuid.UUID(str(export.staging["dataset_id"]))
        fence = export.fence
        export.cleanup_pending = True
        export.selected_count = len(sources)
        await session.commit()
    members: list[PreferenceExportMemberRow] = []

    async def chunks() -> AsyncIterator[bytes]:
        for index, source in enumerate(sources):
            async with session_scope() as session:
                materialized = await materialize_source(session, source)
                members.append(
                    PreferenceExportMemberRow(
                        export_id=identity,
                        pair_id=source.pair_id,
                        owner_user_id=source.owner_id,
                        capture_generation=source.capture_generation,
                        conversation_id=source.conversation_id,
                        source_message_id=materialized.source_message_id,
                        judgement_source=source.judgement_source,
                        judgement_version=source.judgement_version,
                        actor_user_id=source.actor_id,
                        target=materialized.target,
                        prompt_sha256=materialized.prompt_sha256,
                        content_sha256=materialized.content_sha256,
                        row_index=index,
                        published=True,
                    )
                )
                encoded = canonical_bytes(materialized.row.model_dump(mode="json")) + b"\n"
            yield encoded

    result = await store.stage(
        stage_id, dataset_id, export_metadata(body, identity), chunks(), byte_ceiling=int(size)
    )
    return sources, members, result, fence


async def publish_export(
    identity: str,
    sources: list[ExportSource],
    members: list[PreferenceExportMemberRow],
    result: StagedDataset,
    fence: int,
    store: DatasetStore,
) -> str:
    async with session_scope() as session:
        export = await session.get(PreferenceExportRow, identity)
        assert export is not None
        principal = Principal.model_validate(export.authorization_snapshot)
        assert principal.user_id is not None
        await lock_owners(session, {source.owner_id for source in sources} | {principal.user_id})
        await authorize_admin(session, principal)
        for model, identities in (
            (ChatConversationRow, {s.conversation_id for s in sources}),
            (ComparisonPairRow, {s.pair_id for s in sources}),
            (FeedbackRow, {s.judgement_id for s in sources}),
        ):
            await session.execute(
                select(model).where(model.id.in_(identities)).order_by(model.id).with_for_update()
            )
        export = await session.get(
            PreferenceExportRow, identity, populate_existing=True, with_for_update=True
        )
        assert export is not None
        if export.state != "staging" or export.fence != fence:
            raise FeedbackConflict("Export publication fence changed")
        if export.execution_deadline_at is None or export.execution_deadline_at <= datetime.now(
            UTC
        ):
            raise TimeoutError()
        body = PreferenceExportCreate.model_validate(export.request)
        fresh, count = await select_export_sources(session, body)
        if fresh != sources or count != len(sources):
            raise FeedbackConflict("Export source changed before publication")
        stage_id = uuid.UUID(str(export.staging["hold_id"]))
        dataset_id = uuid.UUID(str(export.staging["dataset_id"]))
        export.state = "publishing"
        await register_source(
            session,
            principal,
            export_metadata(body, identity),
            result,
            dataset_id=dataset_id,
            hold_id=stage_id,
            key="feedback-export:" + identity,
        )
        retained = await store.commit(stage_id, dataset_id, result)
        session.add_all(members)
        await finish_upload_hold(
            session, stage_id, retained_bytes=retained, private_staging_cleanup_proven=True
        )
        await write_principal_audit(
            session,
            principal=principal,
            action="feedback.export.publish",
            target_type="feedback_export",
            target_id=identity,
            context={"dataset_id": str(dataset_id), "pair_count": len(members)},
        )
        export.dataset_id, export.state = dataset_id, "succeeded"
        export.version += 1
        export.staging, export.cleanup_pending = {}, False
        await session.commit()
    return "succeeded"


async def execute_export(identity: str, settings: Settings) -> str:
    state = await prepare_export(identity, settings)
    if state != "staging":
        async with session_scope() as session:
            export = await session.get(PreferenceExportRow, identity)
            pending = export is not None and bool(export.staging)
        if pending and state != "queued":
            store = await anyio.to_thread.run_sync(DatasetStore, settings)
            await cleanup_export(identity, store)
        return state
    store = await anyio.to_thread.run_sync(DatasetStore, settings)
    for _attempt in range(3):
        try:
            # Recovery removes only an unregistered stage after checking its
            # immutable dataset identity. DBOS owns serialization of this ID.
            if not await cleanup_export(identity, store):
                return "staging"
            async with session_scope() as session:
                export = await session.get(PreferenceExportRow, identity)
                assert export is not None and export.execution_deadline_at is not None
                remaining = (export.execution_deadline_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return await fail_export(identity, "timeout", store)
            async with asyncio.timeout(remaining):
                sources, members, result, fence = await stage_export(identity, settings, store)
                if len({member.prompt_sha256 for member in members}) < 2:
                    return await fail_export(identity, "insufficient_groups", store)
                if result.invalid_count:
                    return await fail_export(identity, "invalid_data", store)
                if result.split is None:
                    return await fail_export(identity, "insufficient_groups", store)
                return await publish_export(identity, sources, members, result, fence, store)
        except FeedbackConflict:
            await cleanup_export(identity, store)
            async with session_scope() as session:
                export = await session.get(PreferenceExportRow, identity, with_for_update=True)
                assert export is not None
                if export.state != "staging":
                    return export.state
                export.fence += 1
        except FeedbackForbidden:
            return await fail_export(identity, "unauthorized", store)
        except (FeedbackQuotaExceeded, TrainingQuotaExceeded):
            return await fail_export(identity, "quota", store)
        except TrainingUploadTooLarge:
            return await fail_export(identity, "oversize", store)
        except TrainingValidationError:
            return await fail_export(identity, "invalid_data", store)
        except TimeoutError:
            return await fail_export(identity, "timeout", store)
        except Exception:
            # Resolving commit uncertainty precedes all file removal.
            return await fail_export(identity, "internal", store)
    return await fail_export(identity, "source_changed", store)
