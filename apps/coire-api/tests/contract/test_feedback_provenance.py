"""Prospective native snapshots obey the live capture generation and whole prompt."""

import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import select
from test_training_datasets import API
from test_training_datasets import api as api
from test_training_datasets import training_postgres_url as training_postgres_url

from coire_api.auth import Principal, PrincipalKind
from coire_api.chat.turns import Admission
from coire_api.db import (
    ChatConversationRow,
    ChatFeedbackProvenanceRow,
    ChatMessageRow,
    ChatTurnRow,
    ModelVariantRow,
)
from coire_api.feedback.provenance import capture_native_provenance
from coire_api.feedback.service import change_preference
from coire_api.gateway.resolution import ResolvedModel
from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineRenderingIdentity
from coire_core.models.feedback import FeedbackPreferenceUpdate
from coire_core.models.gateway import ChatMessage


async def seed_admission(api: API) -> tuple[Admission, ResolvedModel, Principal]:
    cid, uid, aid, tid = [uuid.uuid4() for _ in range(4)]
    target = InferenceTarget(
        model_id=uuid.UUID(cast(str, api.metadata["analysis_model_id"])),
        variant_id=uuid.UUID(cast(str, api.metadata["analysis_variant_id"])),
        base_manifest_sha256="a" * 64,
    )
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=api.owner)
    async with api.sessions() as session:
        variant = await session.get(ModelVariantRow, target.variant_id)
        assert variant is not None
        variant.validated = True
        session.add(
            ChatConversationRow(
                id=cid,
                owner_user_id=api.owner,
                title="Private",
                mode="chat",
                revision=2,
                context_revision=2,
            )
        )
        await session.flush()
        session.add_all(
            [
                ChatMessageRow(id=uid, conversation_id=cid, role="user", position=1, text="hello"),
                ChatMessageRow(id=aid, conversation_id=cid, role="assistant", position=2, text=""),
            ]
        )
        await session.flush()
        turn = ChatTurnRow(
            id=tid,
            conversation_id=cid,
            input_message_id=uid,
            assistant_message_id=aid,
            client_request_id=uuid.uuid4(),
            request_hash="a" * 64,
            accepted_revision=1,
            model_id=target.model_id,
            model_display_name="Synthetic",
            state="accepted",
        )
        session.add(turn)
        await session.flush()
        conversation = await session.get(ChatConversationRow, cid)
        assert conversation is not None
        conversation.active_turn_id = tid
        await session.commit()
    admission = Admission(
        turn,
        None,
        [ChatMessage(role="user", content="hello")],
        1,
        False,
        capture_generation=1,
        context_revision=2,
        feedback_prompt_eligible=True,
    )
    resolved = ResolvedModel(
        target.model_id,
        "safe",
        4096,
        "/safe",
        uuid.uuid4(),
        "studio",
        "http://node",
        target=target,
        rendering_identity=EngineRenderingIdentity(
            tokenizer_sha256="b" * 64, template_sha256="c" * 64, runtime_sha256="d" * 64
        ),
    )
    return admission, resolved, principal


async def test_native_snapshot_records_exact_prompt_and_unseeded_settings(api: API) -> None:
    admission, resolved, principal = await seed_admission(api)
    async with api.sessions() as session:
        assert await capture_native_provenance(
            session, principal, admission, resolved, api.settings
        )
        await session.commit()
    async with api.sessions() as session:
        row = await session.scalar(select(ChatFeedbackProvenanceRow))
        assert row is not None and row.prompt == [{"role": "user", "content": "hello"}]
        assert row.settings["seed"] is None and row.settings["temperature"] == 0.0
        assert row.target == resolved.target.model_dump(mode="json") if resolved.target else False
        assert row.runtime_sha256 == "d" * 64
        assert row.counted_bytes > 0 and row.expires_at > datetime.now(UTC)
        turn = await session.get(ChatTurnRow, admission.turn.id)
        message = await session.get(ChatMessageRow, admission.turn.assistant_message_id)
        conversation = await session.get(ChatConversationRow, admission.turn.conversation_id)
        assert turn is not None and message is not None and conversation is not None
        turn.state = "completed"
        message.text = "synthetic answer"
        conversation.active_turn_id = None
        await session.commit()
    api.settings.chat_enabled = True
    page = await api.client.get(
        f"/api/v1/chat/conversations/{admission.turn.conversation_id}/feedback", headers=api.headers
    )
    assert page.status_code == 200 and page.json()["items"][0]["eligibility"] == "eligible"


@pytest.mark.parametrize(
    "reason", ["withdrawal", "unproven_history", "missing_identity", "coding_context"]
)
async def test_capture_never_recovers_old_or_unproven_source(api: API, reason: str) -> None:
    from dataclasses import replace

    admission, resolved, principal = await seed_admission(api)
    async with api.sessions() as session:
        if reason == "withdrawal":
            await change_preference(
                session,
                principal,
                FeedbackPreferenceUpdate(
                    client_request_id=uuid.uuid4(),
                    expected_version=1,
                    enabled=False,
                    disclosure_version="feedback-v1",
                ),
            )
            await change_preference(
                session,
                principal,
                FeedbackPreferenceUpdate(
                    client_request_id=uuid.uuid4(),
                    expected_version=2,
                    enabled=True,
                    disclosure_version="feedback-v1",
                ),
            )
        elif reason == "coding_context":
            conversation = await session.get(ChatConversationRow, admission.turn.conversation_id)
            assert conversation is not None
            conversation.mode = "code"
        elif reason == "unproven_history":
            admission = replace(admission, feedback_prompt_eligible=False)
        else:
            resolved = replace(resolved, rendering_identity=None)
        assert not await capture_native_provenance(
            session, principal, admission, resolved, api.settings
        )
        await session.commit()
        assert await session.scalar(select(ChatFeedbackProvenanceRow)) is None


async def test_native_snapshot_freezes_explicit_sampling(api: API) -> None:
    from dataclasses import replace

    from coire_core.models.chat import NativeChatSampling

    admission, resolved, principal = await seed_admission(api)
    admission = replace(
        admission, sampling=NativeChatSampling(temperature=0.7, top_p=0.95, seed=42)
    )
    async with api.sessions() as session:
        assert await capture_native_provenance(
            session, principal, admission, resolved, api.settings
        )
        await session.commit()
    async with api.sessions() as session:
        row = await session.scalar(select(ChatFeedbackProvenanceRow))
        assert row is not None
        assert row.settings["temperature"] == 0.7 and row.settings["top_p"] == 0.95
        assert row.settings["seed"] == 42 and row.settings["max_tokens"] == admission.output_tokens
