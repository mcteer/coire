"""Owner-authorized explicit pairs and immutable first-choice chat context."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    ChatActiveAnswerRow,
    ChatConversationRow,
    ChatEventRow,
    ChatFeedbackProvenanceRow,
    ChatMessageRow,
    ChatTurnRow,
    ComparisonPairRow,
    FeedbackMutationRow,
    FeedbackRow,
)
from coire_api.feedback.eligibility import (
    authorize_owner,
    lock_conversation,
    lock_preference,
    refresh_owner_principal,
)
from coire_api.feedback.provenance import comparison_eligibility
from coire_api.feedback.quota import feedback_copy_bytes, require_feedback_capacity
from coire_api.gateway.resolution import ModelNotFoundError, resolve_exact_target
from coire_api.training.service import training_id
from coire_core.errors import (
    FeedbackConflict,
    FeedbackForbidden,
    FeedbackNotFound,
    FeedbackUnavailable,
    FeedbackValidationError,
)
from coire_core.models.adapters import InferenceTarget
from coire_core.models.chat import ChatEvent
from coire_core.models.feedback import (
    ComparisonAccounting,
    ComparisonCreate,
    ComparisonDetail,
    ComparisonDismiss,
    ComparisonEvent,
    ComparisonReceipt,
    ComparisonSelect,
    ComparisonSelectionReceipt,
    ComparisonState,
    FeedbackReceipt,
    PairChoice,
    SelectionState,
)
from coire_core.models.gateway import TextCompletionUsage
from coire_core.models.preference import canonical_bytes
from coire_core.settings import Settings


def pair_receipt(pair: ComparisonPairRow) -> ComparisonReceipt:
    return ComparisonReceipt(
        id=pair.id,
        version=pair.version,
        state=cast(ComparisonState, pair.generation_state),
        selection_state=cast(SelectionState, pair.selection_state),
        events_path=f"/api/v1/chat/conversations/{pair.conversation_id}/events",
    )


def judgement_receipt(row: FeedbackRow) -> FeedbackReceipt:
    return FeedbackReceipt(
        id=row.id,
        version=row.version,
        source=cast(Literal["owner", "admin"], row.source),
        judgement=cast(PairChoice | None, row.judgement),
    )


async def pair_event(
    session: AsyncSession,
    conversation: ChatConversationRow,
    pair: ComparisonPairRow,
    kind: Literal[
        "comparison.accepted",
        "comparison.status",
        "comparison.delta",
        "comparison.ready",
        "comparison.selection",
        "comparison.terminal",
    ],
    *,
    offset: int = 0,
    length: int = 0,
) -> None:
    # Stored replay frames reference bounded current candidate bytes. They never
    # keep another copied answer after withdrawal, cancellation or body erasure.
    payload = ComparisonEvent(
        type=kind,
        comparison_id=pair.id,
        version=pair.version,
        state=cast(ComparisonState, pair.generation_state),
        selection_state=cast(SelectionState, pair.selection_state),
        text_offset=offset,
        text_length=length,
    )
    conversation.event_cursor += 1
    session.add(
        ChatEventRow(
            conversation_id=conversation.id,
            cursor=conversation.event_cursor,
            type=kind,
            payload=payload.model_dump(mode="json"),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )


async def erase_pair(session: AsyncSession, pair: ComparisonPairRow) -> None:
    now = datetime.now(UTC)
    pair.withdrawn_at = now
    pair.purge_after = now
    pair.purged_at = now
    pair.prompt = None
    pair.original = None
    pair.candidate = None
    pair.execution = {}
    pair.counted_bytes = 0
    await session.execute(
        update(FeedbackRow)
        .where(FeedbackRow.pair_id == pair.id)
        .values(withdrawn_at=now, purged_at=now, judgement=None, tags=[])
    )


async def locked_pair(
    session: AsyncSession,
    principal: Principal,
    cid: uuid.UUID,
    pair_id: str,
    *,
    live: bool,
) -> tuple[ChatConversationRow, ComparisonPairRow]:
    owner = await authorize_owner(session, principal)
    preference = await lock_preference(session, owner)
    conversation = await lock_conversation(session, owner, cid)
    pair = await session.get(
        ComparisonPairRow, pair_id, with_for_update=True, populate_existing=True
    )
    if pair is None or pair.owner_user_id != owner or pair.conversation_id != cid:
        raise FeedbackNotFound()
    if pair.selection_state == "pending" and pair.expires_at <= datetime.now(UTC):
        pair.selection_state = "expired"
        pair.generation_state = "expired"
        pair.version += 1
        await erase_pair(session, pair)
        await pair_event(session, conversation, pair, "comparison.terminal")
    if live and (
        not preference.enabled
        or pair.capture_generation != preference.capture_generation
        or pair.withdrawn_at is not None
    ):
        raise FeedbackForbidden("This contribution was withdrawn")
    return conversation, pair


async def create_comparison(
    session: AsyncSession,
    principal: Principal,
    cid: uuid.UUID,
    body: ComparisonCreate,
    settings: Settings,
) -> ComparisonReceipt:
    owner = await authorize_owner(session, principal)
    preference = await lock_preference(session, owner)
    conversation = await lock_conversation(session, owner, cid)
    intent = hashlib.sha256(
        canonical_bytes({"conversation_id": str(cid), "body": body.model_dump(mode="json")})
    ).hexdigest()
    existing = await session.scalar(
        select(ComparisonPairRow).where(
            ComparisonPairRow.owner_user_id == owner,
            ComparisonPairRow.client_request_id == body.client_request_id,
        )
    )
    if existing is not None:
        if existing.request_sha256 != intent:
            raise FeedbackConflict("Request identity was reused with different input")
        _, existing = await locked_pair(session, principal, cid, existing.id, live=False)
        return pair_receipt(existing)
    if not preference.enabled:
        raise FeedbackForbidden("Feedback capture is disabled")
    if conversation.revision != body.expected_revision:
        raise FeedbackConflict("Conversation changed; reload before comparing")
    pending = await session.scalar(
        select(ComparisonPairRow)
        .where(
            ComparisonPairRow.conversation_id == cid, ComparisonPairRow.selection_state == "pending"
        )
        .with_for_update()
    )
    if pending is not None:
        if pending.expires_at > datetime.now(UTC):
            raise FeedbackConflict("comparison_pending")
        pending.selection_state = "expired"
        pending.generation_state = "expired"
        pending.version += 1
        await erase_pair(session, pending)
        await session.flush()
    message = await session.get(ChatMessageRow, body.source_message_id)
    if message is None or message.conversation_id != cid or message.role != "assistant":
        raise FeedbackNotFound()
    provenance = await session.scalar(
        select(ChatFeedbackProvenanceRow).where(
            ChatFeedbackProvenanceRow.source_message_id == message.id,
            ChatFeedbackProvenanceRow.owner_user_id == owner,
        )
    )
    turn = await session.scalar(
        select(ChatTurnRow).where(ChatTurnRow.assistant_message_id == message.id)
    )
    latest = int(
        await session.scalar(
            select(func.max(ChatMessageRow.position)).where(ChatMessageRow.conversation_id == cid)
        )
        or 0
    )
    reason = comparison_eligibility(preference, conversation, message, provenance, turn, latest)
    if reason != "eligible":
        raise FeedbackValidationError(reason)
    assert provenance is not None and provenance.prompt is not None and turn is not None
    current = await refresh_owner_principal(session, principal)
    try:
        await resolve_exact_target(
            session, InferenceTarget.model_validate(provenance.target), current
        )
    except ModelNotFoundError:
        raise FeedbackUnavailable() from None
    counted = feedback_copy_bytes(provenance.prompt, message.text)
    await require_feedback_capacity(session, counted, settings)
    seed = secrets.randbelow(2**32)
    now = datetime.now(UTC)
    pair = ComparisonPairRow(
        id=training_id(),
        owner_user_id=owner,
        conversation_id=cid,
        source_turn_id=turn.id,
        source_message_id=message.id,
        provenance_id=provenance.id,
        capture_generation=preference.capture_generation,
        context_revision=provenance.context_revision,
        client_request_id=body.client_request_id,
        request_sha256=intent,
        target=provenance.target,
        prompt=provenance.prompt,
        original=message.text,
        counted_bytes=counted,
        generation_state="queued",
        selection_state="pending",
        version=1,
        execution={
            "seed": seed,
            "settings": provenance.settings,
            "tokenizer_sha256": provenance.tokenizer_sha256,
            "template_sha256": provenance.template_sha256,
            "runtime_sha256": provenance.runtime_sha256,
        },
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    session.add(pair)
    await session.flush()
    await pair_event(session, conversation, pair, "comparison.accepted")
    await write_principal_audit(
        session,
        principal=principal,
        action="feedback.comparison.create",
        target_type="comparison",
        target_id=pair.id,
        context={"version": pair.version},
    )
    return pair_receipt(pair)


async def comparison_detail(
    session: AsyncSession,
    principal: Principal,
    cid: uuid.UUID,
    pair_id: str,
) -> ComparisonDetail:
    _, pair = await locked_pair(session, principal, cid, pair_id, live=False)
    preference = await lock_preference(session, pair.owner_user_id)
    visible = (
        preference.enabled
        and preference.capture_generation == pair.capture_generation
        and pair.withdrawn_at is None
    )
    labels = (
        list(
            await session.scalars(
                select(FeedbackRow).where(
                    FeedbackRow.pair_id == pair.id, FeedbackRow.withdrawn_at.is_(None)
                )
            )
        )
        if visible
        else []
    )
    by_source = {row.source: row for row in labels}
    accounting = (
        ComparisonAccounting.model_validate(pair.accounting)
        if visible and pair.accounting
        else None
    )
    return ComparisonDetail(
        **pair_receipt(pair).model_dump(),
        conversation_id=cid,
        source_message_id=pair.source_message_id,
        target=InferenceTarget.model_validate(pair.target) if visible and pair.target else None,
        original=pair.original if visible else None,
        candidate=pair.candidate if visible else None,
        selected=cast(PairChoice | None, pair.selected_candidate),
        usage=TextCompletionUsage(
            prompt_tokens=accounting.prompt_tokens,
            completion_tokens=accounting.completion_tokens,
            total_tokens=accounting.prompt_tokens + accounting.completion_tokens,
        )
        if accounting
        else None,
        owner_judgement=judgement_receipt(by_source["owner"]) if "owner" in by_source else None,
        admin_judgement=judgement_receipt(by_source["admin"]) if "admin" in by_source else None,
        owner_tags=list(by_source["owner"].tags) if "owner" in by_source else [],
        admin_tags=list(by_source["admin"].tags) if "admin" in by_source else [],
        eligibility="eligible" if visible else "withdrawn",
        created_at=pair.created_at,
        expires_at=pair.expires_at,
    )


async def command_receipt(
    session: AsyncSession,
    principal: Principal,
    pair: ComparisonPairRow,
    body: ComparisonSelect | ComparisonDismiss,
    operation: str,
) -> bool:
    intent = hashlib.sha256(
        canonical_bytes({"pair_id": pair.id, "body": body.model_dump(mode="json")})
    ).hexdigest()
    row = await session.scalar(
        select(FeedbackMutationRow).where(
            FeedbackMutationRow.actor_user_id == principal.user_id,
            FeedbackMutationRow.operation == operation,
            FeedbackMutationRow.request_id == str(body.client_request_id),
        )
    )
    if row is not None:
        if row.intent_sha256 != intent:
            raise FeedbackConflict("Request identity was reused with different input")
        return True
    if pair.version != body.expected_version:
        raise FeedbackConflict("Comparison changed; reload before saving")
    assert principal.user_id is not None
    session.add(
        FeedbackMutationRow(
            actor_user_id=principal.user_id,
            operation=operation,
            request_id=str(body.client_request_id),
            intent_sha256=intent,
            receipt={"pair_id": pair.id},
        )
    )
    return False


async def select_comparison(
    session: AsyncSession,
    principal: Principal,
    cid: uuid.UUID,
    pair_id: str,
    body: ComparisonSelect,
) -> ComparisonSelectionReceipt:
    conversation, pair = await locked_pair(session, principal, cid, pair_id, live=True)
    replay = await command_receipt(session, principal, pair, body, "selection")
    feedback = await session.scalar(
        select(FeedbackRow)
        .where(FeedbackRow.pair_id == pair.id, FeedbackRow.source == "owner")
        .with_for_update()
    )
    if replay:
        if feedback is None or feedback.withdrawn_at is not None:
            raise FeedbackForbidden("This contribution was withdrawn")
        return ComparisonSelectionReceipt(
            comparison=pair_receipt(pair),
            feedback=judgement_receipt(feedback),
            conversation_revision=conversation.revision,
        )
    if pair.generation_state != "ready" or pair.selection_state not in {"pending", "chosen"}:
        raise FeedbackConflict("Comparison is not ready for selection")
    if pair.selection_state == "pending":
        if (
            conversation.mode != "chat"
            or conversation.context_revision != pair.context_revision
            or conversation.active_turn_id is not None
        ):
            raise FeedbackConflict("Conversation changed; selection cannot alter context")
        selected = pair.source_message_id
        if body.candidate == "candidate":
            if not pair.candidate:
                raise FeedbackConflict("Candidate answer is unavailable")
            position = (
                int(
                    await session.scalar(
                        select(func.max(ChatMessageRow.position)).where(
                            ChatMessageRow.conversation_id == cid
                        )
                    )
                    or 0
                )
                + 1
            )
            selected = uuid.uuid4()
            session.add(
                ChatMessageRow(
                    id=selected,
                    conversation_id=cid,
                    position=position,
                    role="assistant",
                    text=pair.candidate,
                    target=pair.target,
                    model_id=uuid.UUID(str(pair.target["model_id"])) if pair.target else None,
                    attachment_ids=[],
                    attachment_selections=[],
                )
            )
            pair.candidate_message_id = selected
        conversation.revision += 1
        conversation.context_revision += 1
        session.add(
            ChatActiveAnswerRow(
                source_turn_id=pair.source_turn_id,
                conversation_id=cid,
                selected_message_id=selected,
                selection_revision=conversation.context_revision,
            )
        )
        pair.selected_candidate = body.candidate
        pair.selection_state = "chosen"
    if feedback is None:
        feedback = FeedbackRow(
            owner_user_id=pair.owner_user_id,
            actor_user_id=pair.owner_user_id,
            conversation_id=cid,
            pair_id=pair.id,
            kind="pair",
            source="owner",
            version=1,
            capture_generation=pair.capture_generation,
        )
        session.add(feedback)
    else:
        feedback.version += 1
    feedback.judgement = body.candidate
    feedback.tags = list(body.tags)
    feedback.updated_at = datetime.now(UTC)
    pair.version += 1
    await session.flush()
    await pair_event(session, conversation, pair, "comparison.selection")
    await write_principal_audit(
        session,
        principal=principal,
        action="feedback.comparison.select",
        target_type="comparison",
        target_id=pair.id,
        context={"version": pair.version},
    )
    return ComparisonSelectionReceipt(
        comparison=pair_receipt(pair),
        feedback=judgement_receipt(feedback),
        conversation_revision=conversation.revision,
    )


async def dismiss_comparison(
    session: AsyncSession,
    principal: Principal,
    cid: uuid.UUID,
    pair_id: str,
    body: ComparisonDismiss,
) -> ComparisonReceipt:
    conversation, pair = await locked_pair(session, principal, cid, pair_id, live=False)
    replay = await command_receipt(session, principal, pair, body, "dismiss")
    if replay:
        return pair_receipt(pair)
    if pair.selection_state != "pending":
        raise FeedbackConflict("Only a pending comparison can be dismissed")
    pair.generation_state = "cancelled"
    pair.selection_state = "dismissed"
    pair.version += 1
    await erase_pair(session, pair)
    await pair_event(session, conversation, pair, "comparison.terminal")
    await write_principal_audit(
        session,
        principal=principal,
        action="feedback.comparison.dismiss",
        target_type="comparison",
        target_id=pair.id,
        context={"version": pair.version},
    )
    return pair_receipt(pair)


_running: set[asyncio.Task[None]] = set()


async def start_comparison(pair_id: str, principal: Principal, settings: Settings) -> None:
    from coire_api.feedback.comparison_execution import run_comparison

    task = asyncio.create_task(
        run_comparison(pair_id, principal, settings), name=f"comparison-{pair_id}"
    )
    _running.add(task)
    task.add_done_callback(_running.discard)


async def replay_comparison_event(principal: Principal, row: ChatEventRow) -> ChatEvent | None:
    """Resolve each content-free cursor against current live private source bytes."""
    from coire_api.db import session_scope
    from coire_core.errors import FeedbackForbidden, FeedbackNotFound
    from coire_core.models.chat import ChatEvent

    payload = ComparisonEvent.model_validate(row.payload)
    try:
        async with session_scope() as session:
            _, pair = await locked_pair(
                session, principal, row.conversation_id, payload.comparison_id, live=False
            )
            preference = await lock_preference(session, pair.owner_user_id)
            visible = (
                preference.enabled
                and pair.capture_generation == preference.capture_generation
                and pair.withdrawn_at is None
                and row.expires_at > datetime.now(UTC)
            )
            text = None
            if visible and payload.type == "comparison.delta" and pair.candidate is not None:
                end = payload.text_offset + payload.text_length
                if end <= len(pair.candidate):
                    text = pair.candidate[payload.text_offset : end]
            updates: dict[str, object] = {"text_delta": text}
            if not visible:
                updates.update(
                    state=pair.generation_state,
                    selection_state=pair.selection_state,
                    version=pair.version,
                    text_offset=0,
                    text_length=0,
                )
            current = payload.model_copy(update=updates)
            await session.commit()
            return ChatEvent(
                conversation_id=row.conversation_id,
                cursor=row.cursor,
                created_at=row.created_at,
                payload=current,
            )
    except (FeedbackForbidden, FeedbackNotFound):
        return None
