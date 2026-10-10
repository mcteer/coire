"""Prospective native text provenance; incomplete history never becomes eligible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.chat.turns import Admission
from coire_api.db import (
    ChatConversationRow,
    ChatFeedbackProvenanceRow,
    ChatMessageRow,
    ChatTurnRow,
    FeedbackPreferenceRow,
)
from coire_api.feedback.eligibility import authorize_owner, lock_conversation, lock_preference
from coire_api.feedback.quota import require_feedback_capacity
from coire_api.feedback.telemetry import mutations_total, tracer
from coire_api.gateway.resolution import ResolvedModel
from coire_core.errors import FeedbackForbidden, FeedbackNotFound, FeedbackQuotaExceeded
from coire_core.models.feedback import FeedbackEligibilityReason, FeedbackGenerationSettings
from coire_core.models.preference import PreferenceRow, canonical_bytes
from coire_core.models.registry import EngineBackend, ModelSource, Reasoning
from coire_core.settings import Settings


def comparison_eligibility(
    preference: FeedbackPreferenceRow,
    conversation: ChatConversationRow,
    message: ChatMessageRow,
    provenance: ChatFeedbackProvenanceRow | None,
    turn: ChatTurnRow | None,
    latest_position: int,
) -> FeedbackEligibilityReason:
    if not preference.enabled:
        return "capture_disabled"
    if provenance is None:
        return "source_provenance_unavailable"
    if (
        provenance.withdrawn_at is not None
        or provenance.capture_generation != preference.capture_generation
    ):
        return "withdrawn"
    if provenance.expires_at <= datetime.now(UTC):
        return "expired"
    if (
        provenance.prompt is None
        or not provenance.runtime_sha256
        or not provenance.target
        or message.target != provenance.target
    ):
        return "source_provenance_unavailable"
    if turn is None or turn.state != "completed" or not message.text.strip():
        return "source_incomplete"
    if conversation.mode != "chat" or message.reasoning or turn.action != "chat":
        return "unsupported_content"
    if (
        conversation.active_turn_id is not None
        or message.position != latest_position
        or conversation.context_revision != provenance.context_revision
    ):
        return "source_not_latest"
    if len(message.text.encode()) > 64 * 1024:
        return "source_too_large"
    return "eligible"


async def capture_native_provenance(
    session: AsyncSession,
    principal: Principal,
    admission: Admission,
    resolved: ResolvedModel,
    settings: Settings,
) -> bool:
    with tracer.start_as_current_span(
        "coire.api.feedback.capture", record_exception=False, set_status_on_exception=False
    ):
        captured = await _capture_native_provenance(
            session, principal, admission, resolved, settings
        )
        mutations_total.add(
            1, {"operation": "capture", "outcome": "accepted" if captured else "ineligible"}
        )
        return captured


async def _capture_native_provenance(
    session: AsyncSession,
    principal: Principal,
    admission: Admission,
    resolved: ResolvedModel,
    settings: Settings,
) -> bool:
    """Snapshot only the current admitted prompt before any generation I/O.

    Capture refusal leaves ordinary chat usable; no content is put in audit or
    command receipts. All retained bytes are bounded and charged atomically.
    """
    identity = resolved.rendering_identity
    if (
        admission.capture_generation is None
        or admission.context_revision is None
        or not admission.feedback_prompt_eligible
        or admission.reasoning_mode is not Reasoning.NONE
        or resolved.source is not ModelSource.STUDIO
        or resolved.backend is not EngineBackend.MLX_LM
        or resolved.target is None
        or resolved.model_id != admission.turn.model_id
        or resolved.target.model_id != admission.turn.model_id
        or identity is None
    ):
        return False
    try:
        owner = await authorize_owner(session, principal)
        preference = await lock_preference(session, owner)
        if not preference.enabled or preference.capture_generation != admission.capture_generation:
            return False
        conversation = await lock_conversation(session, owner, admission.turn.conversation_id)
        if (
            conversation.mode != "chat"
            or conversation.context_revision != admission.context_revision
            or conversation.active_turn_id != admission.turn.id
        ):
            return False
        turn = await session.get(ChatTurnRow, admission.turn.id, with_for_update=True)
        if turn is None or turn.state not in {"accepted", "queued", "loading", "running"}:
            return False
        existing = await session.scalar(
            select(ChatFeedbackProvenanceRow).where(
                ChatFeedbackProvenanceRow.source_turn_id == turn.id,
            )
        )
        if existing is not None:
            return (
                existing.withdrawn_at is None
                and existing.capture_generation == preference.capture_generation
            )
        prompt = [
            {"role": message.role, "content": message.content} for message in admission.history
        ]
        pair = PreferenceRow.model_validate(
            {"prompt": prompt, "chosen": "original", "rejected": "candidate"}
        )
        payload = [message.model_dump(mode="json") for message in pair.prompt]
        counted = len(canonical_bytes(payload))
        await require_feedback_capacity(session, counted, settings)
        now = datetime.now(UTC)
        generation = FeedbackGenerationSettings(
            temperature=admission.sampling.temperature if admission.sampling else 0.0,
            top_p=admission.sampling.top_p if admission.sampling else 1.0,
            max_tokens=admission.output_tokens,
            seed=admission.sampling.seed if admission.sampling else None,
            top_k=admission.sampling.top_k if admission.sampling else 0,
            min_p=admission.sampling.min_p if admission.sampling else 0.0,
            enable_thinking=False,
        )
        target = resolved.target.model_dump(mode="json")
        turn.target = target
        message = await session.get(ChatMessageRow, turn.assistant_message_id)
        if message is None:
            return False
        message.target = target
        conversation.selected_target = target
        session.add(
            ChatFeedbackProvenanceRow(
                owner_user_id=owner,
                conversation_id=conversation.id,
                source_turn_id=turn.id,
                source_message_id=turn.assistant_message_id,
                capture_generation=preference.capture_generation,
                context_revision=admission.context_revision,
                target=target,
                settings=generation.model_dump(mode="json"),
                tokenizer_sha256=identity.tokenizer_sha256,
                template_sha256=identity.template_sha256,
                runtime_sha256=identity.runtime_sha256,
                prompt_sha256=pair.prompt_sha256(),
                prompt=payload,
                counted_bytes=counted,
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
        )
        await session.flush()
        return True
    except (
        FeedbackForbidden,
        FeedbackNotFound,
        FeedbackQuotaExceeded,
        ValidationError,
        UnicodeError,
    ):
        return False
