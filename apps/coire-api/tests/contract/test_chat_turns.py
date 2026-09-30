"""Locked native text-turn admission contracts."""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.chat.turns import admit_turn, project_message, project_turn, read_turn_detail
from coire_api.db import (
    ChatAttachmentRow,
    ChatConversationRow,
    ChatEventRow,
    ChatMessageRow,
    ChatTurnRow,
    ModelRow,
    get_session,
)
from coire_core.errors import ChatConflict, ChatContextExceeded, ChatNotFound
from coire_core.models.chat import ChatTurnCreate, ChatTurnDetail
from coire_core.models.files import ChatAttachmentSelection, FileProcessAsset, FileProcessResult
from coire_core.models.registry import ModelState, Visibility
from coire_core.settings import Settings

NOW = datetime.now(UTC)


def _model(
    *, context_window: int = 4096, visibility: Visibility = Visibility.PUBLISHED
) -> ModelRow:
    return ModelRow(
        id=uuid.uuid4(),
        repo_id="owner/model",
        slug="owner--model",
        display_name="Named model",
        state=ModelState.READY,
        visibility=visibility,
        entitlement=[],
        tags=["general"],
        precision="4bit",
        weight_bytes=1,
        memory_estimate_bytes=2,
        context_window=context_window,
        capability_profile={},
        backend="mlx_lm",
    )


class Session:
    def __init__(self, conversation: ChatConversationRow, model: ModelRow) -> None:
        self.conversation = conversation
        self.model = model
        self.messages: list[ChatMessageRow] = []
        self.turns: list[ChatTurnRow] = []
        self.events: list[ChatEventRow] = []
        self.attachments: dict[uuid.UUID, ChatAttachmentRow] = {}
        self.sql: list[str] = []
        self.commits = 0

    async def scalar(self, statement: object) -> object:
        sql = str(statement)
        self.sql.append(sql)
        if "FROM chat_conversations" in sql:
            return self.conversation
        if "FROM chat_turns" in sql:
            values = statement.compile().params.values()  # type: ignore[attr-defined]
            return next((turn for turn in self.turns if turn.client_request_id in values), None)
        return None

    async def execute(self, statement: object) -> object:
        sql = str(statement)
        self.sql.append(sql)
        rows = self.turns if "FROM chat_turns" in sql else self.messages
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))

    async def get(self, model: object, identifier: uuid.UUID) -> object | None:
        if model is ChatAttachmentRow:
            return self.attachments.get(identifier)
        if model is ChatTurnRow:
            return next((turn for turn in self.turns if turn.id == identifier), None)
        return self.model if self.model.id == identifier else None

    def add(self, row: object) -> None:
        if isinstance(row, ChatMessageRow):
            self.messages.append(row)
        elif isinstance(row, ChatTurnRow):
            self.turns.append(row)
        elif isinstance(row, ChatEventRow):
            self.events.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def _case() -> tuple[Session, Principal, ChatTurnCreate]:
    owner_id = uuid.uuid4()
    conversation = ChatConversationRow(
        id=uuid.uuid4(),
        owner_user_id=owner_id,
        title="Draft",
        mode="chat",
        revision=1,
        event_cursor=0,
        created_at=NOW,
        updated_at=NOW,
    )
    model = _model()
    session = Session(conversation, model)
    principal = Principal(kind=PrincipalKind.USER, user_id=owner_id)
    body = ChatTurnCreate(
        client_request_id=uuid.uuid4(), expected_revision=1, model_id=model.id, content="Hello"
    )
    return session, principal, body


async def test_send_persists_input_assistant_turn_and_event_before_stream() -> None:
    session, principal, body = _case()
    session.messages.append(
        ChatMessageRow(
            id=uuid.uuid4(),
            conversation_id=session.conversation.id,
            position=1,
            role="user",
            text="Earlier",
            reasoning="",
            attachment_ids=[],
            created_at=NOW,
        )
    )
    admission = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    assert not admission.replay
    assert session.commits == 1
    assert any("FOR UPDATE" in sql for sql in session.sql)
    assert [message.content for message in admission.history] == ["Earlier", "Hello"]
    assert [message.position for message in session.messages] == [1, 2, 3]
    assert session.messages[-1].role == "assistant" and session.messages[-1].text == ""
    assert session.turns[0].model_display_name == "Named model"
    assert session.messages[-1].model_id == body.model_id
    assert session.events[0].type == "turn.accepted"
    assert session.conversation.event_cursor == 1
    assert session.conversation.revision == 2
    assert session.conversation.active_turn_id == session.turns[0].id


async def test_text_file_selection_is_owner_scoped_and_saved_for_history() -> None:
    session, principal, body = _case()
    file_id = uuid.uuid4()
    result = FileProcessResult(
        job_id="0" * 26,
        input_id=file_id,
        source_sha256="a" * 64,
        detected_type="text/plain",
        extracted_text="file evidence",
        assets=[],
    )
    session.attachments[file_id] = ChatAttachmentRow(
        id=file_id,
        owner_user_id=principal.user_id,
        conversation_id=session.conversation.id,
        filename="notes.txt",
        detected_type="text/plain",
        original_bytes=13,
        original_sha256="a" * 64,
        original_key=str(file_id),
        derived_bytes=0,
        state="ready",
        asset_manifest={"job_id": result.job_id, "result": result.model_dump(mode="json")},
        created_at=NOW,
        updated_at=NOW,
    )
    body.attachments = [ChatAttachmentSelection(file_id=file_id, mode="text")]
    admission = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    assert "file evidence" in str(admission.history[-1].content)
    saved = session.messages[0]
    assert saved.text == "Hello"
    assert saved.prompt_content == admission.history[-1].content
    assert project_message(saved).attachment_selections == body.attachments


async def test_explicit_retry_uses_original_input_without_duplicate_user_message() -> None:
    session, principal, body = _case()
    first = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    first.turn.state = "interrupted"
    session.conversation.active_turn_id = None
    session.messages[-1].text = "Saved partial answer"
    retry = ChatTurnCreate(
        client_request_id=uuid.uuid4(),
        expected_revision=2,
        model_id=body.model_id,
        content="Hello",
        retry_of=first.turn.id,
        recovery_mode="retry",
    )
    admission = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        retry,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    assert [message.role for message in session.messages] == ["user", "assistant", "assistant"]
    assert [message.content for message in admission.history] == ["Hello"]
    assert admission.turn.input_message_id == first.turn.input_message_id
    assert admission.turn.assistant_message_id != first.turn.assistant_message_id
    assert project_turn(admission.turn).retry_of == first.turn.id


async def test_retry_refuses_changed_input_or_non_latest_response() -> None:
    session, principal, body = _case()
    first = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    first.turn.state = "interrupted"
    session.conversation.active_turn_id = None
    retry = ChatTurnCreate(
        client_request_id=uuid.uuid4(),
        expected_revision=2,
        model_id=body.model_id,
        content="Changed",
        retry_of=first.turn.id,
        recovery_mode="retry",
    )
    with pytest.raises(ChatConflict, match="original input"):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            retry,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    retry.content = body.content
    session.messages.append(
        ChatMessageRow(
            id=uuid.uuid4(),
            conversation_id=session.conversation.id,
            position=3,
            role="assistant",
            text="Later",
            reasoning="",
            created_at=NOW,
        )
    )
    with pytest.raises(ChatConflict, match="latest interrupted"):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            retry,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )


async def test_later_turn_uses_only_latest_response_attempt_in_history() -> None:
    session, principal, body = _case()
    first = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    first.turn.state = "interrupted"
    session.messages[-1].text = "Discarded partial"
    session.conversation.active_turn_id = None
    retried = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        ChatTurnCreate(
            client_request_id=uuid.uuid4(),
            expected_revision=2,
            model_id=body.model_id,
            content=body.content,
            retry_of=first.turn.id,
            recovery_mode="retry",
        ),
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    retried.turn.state = "completed"
    session.messages[-1].text = "Complete answer"
    session.conversation.active_turn_id = None
    next_turn = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        ChatTurnCreate(
            client_request_id=uuid.uuid4(),
            expected_revision=3,
            model_id=body.model_id,
            content="Next question",
        ),
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    assert [message.content for message in next_turn.history] == [
        "Hello",
        "Complete answer",
        "Next question",
    ]


async def test_explicit_continuation_uses_saved_partial_as_context() -> None:
    session, principal, body = _case()
    first = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    first.turn.state = "interrupted"
    session.messages[-1].text = "The first half"
    session.conversation.active_turn_id = None
    continuation = ChatTurnCreate(
        client_request_id=uuid.uuid4(),
        expected_revision=2,
        model_id=body.model_id,
        content="Continue the previous response.",
        retry_of=first.turn.id,
        recovery_mode="continue",
    )
    admitted = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        continuation,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    assert [message.content for message in admitted.history] == [
        "Hello",
        "The first half",
        "Continue the previous response.",
    ]
    assert [message.role for message in session.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert admitted.turn.input_message_id != first.turn.input_message_id
    assert project_turn(admitted.turn).recovery_mode == "continue"


async def test_continuation_refuses_empty_partial_or_changed_prompt() -> None:
    session, principal, body = _case()
    first = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    first.turn.state = "failed"
    session.conversation.active_turn_id = None
    continuation = ChatTurnCreate(
        client_request_id=uuid.uuid4(),
        expected_revision=2,
        model_id=body.model_id,
        content="Continue the previous response.",
        retry_of=first.turn.id,
        recovery_mode="continue",
    )
    with pytest.raises(ChatConflict, match="saved partial"):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            continuation,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    session.messages[-1].text = "Partial"
    continuation.content = "Ignore the user"
    with pytest.raises(ChatConflict, match="saved partial"):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            continuation,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )


async def test_text_selection_refuses_foreign_file_and_empty_scan() -> None:
    session, principal, body = _case()
    file_id = uuid.uuid4()
    body.attachments = [ChatAttachmentSelection(file_id=file_id, mode="text")]
    with pytest.raises(ChatNotFound):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            body,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    result = FileProcessResult(
        job_id="0" * 26,
        input_id=file_id,
        source_sha256="a" * 64,
        detected_type="application/pdf",
        page_count=1,
        extracted_text="[Page 1]\n\n",
        assets=[],
    )
    session.attachments[file_id] = ChatAttachmentRow(
        id=file_id,
        owner_user_id=principal.user_id,
        conversation_id=session.conversation.id,
        filename="scan.pdf",
        detected_type="application/pdf",
        original_bytes=13,
        original_sha256="a" * 64,
        original_key=str(file_id),
        derived_bytes=0,
        state="ready",
        page_count=1,
        asset_manifest={"job_id": result.job_id, "result": result.model_dump(mode="json")},
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(ChatConflict, match="no extracted text"):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            body,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )


async def test_rendered_pdf_keeps_verified_text_context() -> None:
    session, principal, body = _case()
    file_id = uuid.uuid4()
    prior = FileProcessResult(
        job_id="0" * 26,
        input_id=file_id,
        source_sha256="a" * 64,
        detected_type="application/pdf",
        page_count=1,
        extracted_text="[Page 1]\nReadable words\n",
        assets=[],
    )
    rendered = FileProcessResult(
        job_id="1" * 26,
        input_id=file_id,
        source_sha256="a" * 64,
        detected_type="application/pdf",
        page_count=1,
        assets=[
            FileProcessAsset(
                id=uuid.uuid4(),
                sha256="b" * 64,
                bytes=100,
                media_type="image/png",
                width=10,
                height=10,
                page=1,
            )
        ],
    )
    session.attachments[file_id] = ChatAttachmentRow(
        id=file_id,
        owner_user_id=principal.user_id,
        conversation_id=session.conversation.id,
        filename="document.pdf",
        detected_type="application/pdf",
        original_bytes=200,
        original_sha256="a" * 64,
        original_key=str(file_id),
        derived_bytes=100,
        state="ready",
        page_count=1,
        asset_manifest={
            "job_id": rendered.job_id,
            "result": rendered.model_dump(mode="json"),
            "text_result": prior.model_dump(mode="json"),
        },
        created_at=NOW,
        updated_at=NOW,
    )
    body.attachments = [ChatAttachmentSelection(file_id=file_id, mode="text")]
    admission = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    assert "Readable words" in str(admission.history[-1].content)


async def test_visual_turn_keeps_verified_image_in_later_context(tmp_path: Path) -> None:
    session, principal, body = _case()
    session.model.backend = "mlx_vlm"
    session.model.visual_capability = {
        "verified": True,
        "max_images": 2,
        "max_image_pixels": 256,
        "max_encoded_bytes": 90,
    }
    file_id, asset_id = uuid.uuid4(), uuid.uuid4()
    job_id = "01K00000000000000000000000"
    output = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 0, 0)).save(output, format="PNG", optimize=True)
    image = output.getvalue()
    folder = tmp_path / job_id
    folder.mkdir()
    (folder / f"{asset_id}.png").write_bytes(image)
    result = FileProcessResult(
        job_id=job_id,
        input_id=file_id,
        source_sha256="a" * 64,
        detected_type="image/png",
        assets=[
            FileProcessAsset(
                id=asset_id,
                sha256=hashlib.sha256(image).hexdigest(),
                bytes=len(image),
                media_type="image/png",
                width=16,
                height=16,
            )
        ],
    )
    session.attachments[file_id] = ChatAttachmentRow(
        id=file_id,
        owner_user_id=principal.user_id,
        conversation_id=session.conversation.id,
        filename="red.png",
        detected_type="image/png",
        original_bytes=len(image),
        original_sha256="a" * 64,
        original_key=str(file_id),
        derived_bytes=len(image),
        state="ready",
        asset_manifest={"job_id": job_id, "result": result.model_dump(mode="json")},
        created_at=NOW,
        updated_at=NOW,
    )
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", chat_derived_root=str(tmp_path)
    )
    body.attachments = [ChatAttachmentSelection(file_id=file_id, mode="visual")]
    first = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        settings,
    )
    assert isinstance(first.history[-1].content, list)
    assert first.history[-1].content[-1].type == "image_url"
    assert "data:image/png" not in (session.messages[0].prompt_content or "")
    session.turns[0].state = "completed"
    session.conversation.active_turn_id = None
    session.messages[-1].text = "Red."
    followup = ChatTurnCreate(
        client_request_id=uuid.uuid4(),
        expected_revision=session.conversation.revision,
        model_id=session.model.id,
        content="What was shown?",
    )
    second = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        followup,
        settings,
    )
    assert isinstance(second.history[0].content, list)
    assert second.history[0].content[-1].type == "image_url"


async def test_text_model_refuses_visual_selection_before_turn_write() -> None:
    session, principal, body = _case()
    body.attachments = [ChatAttachmentSelection(file_id=uuid.uuid4(), mode="visual")]
    with pytest.raises(ChatConflict, match="cannot accept visual"):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            body,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    assert session.commits == 0 and not session.turns


async def test_replay_is_idempotent_but_changed_body_conflicts() -> None:
    session, principal, body = _case()
    first = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    second = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    assert first.turn.id == second.turn.id and second.replay
    assert len(session.turns) == 1 and session.commits == 1
    with pytest.raises(ChatConflict):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            body.model_copy(update={"content": "Changed"}),
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )


async def test_owner_revision_active_and_model_rules_precede_writes() -> None:
    session, principal, body = _case()
    with pytest.raises(ChatNotFound):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal.model_copy(update={"user_id": uuid.uuid4()}),
            body,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    with pytest.raises(ChatConflict):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            body.model_copy(update={"expected_revision": 2}),
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    session.conversation.active_turn_id = uuid.uuid4()
    with pytest.raises(ChatConflict):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            body,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    session.conversation.active_turn_id = None
    session.model.visibility = Visibility.ADMIN_ONLY
    with pytest.raises(ChatNotFound):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            body,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    session.model.visibility = Visibility.PUBLISHED
    with pytest.raises(ChatConflict):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            body.model_copy(update={"action": "apply"}),
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    assert session.commits == 0


async def test_full_history_context_preflight_refuses_without_dropping() -> None:
    session, principal, body = _case()
    session.model.context_window = 20
    session.messages.append(
        ChatMessageRow(
            id=uuid.uuid4(),
            conversation_id=session.conversation.id,
            position=1,
            role="user",
            text="Earlier " * 20,
            reasoning="",
            attachment_ids=[],
            created_at=NOW,
        )
    )
    with pytest.raises(ChatContextExceeded):
        await admit_turn(
            session,  # type: ignore[arg-type]
            session.conversation.id,
            principal,
            body,
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    assert len(session.messages) == 1 and not session.turns and session.commits == 0


async def test_small_context_gets_bounded_output_allowance() -> None:
    session, principal, body = _case()
    session.model.context_window = 128
    admission = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    assert admission.output_tokens == 32


async def test_model_switch_preserves_earlier_model_snapshot_in_history() -> None:
    session, principal, body = _case()
    previous_id = session.model.id
    session.messages.extend(
        [
            ChatMessageRow(
                id=uuid.uuid4(),
                conversation_id=session.conversation.id,
                position=1,
                role="user",
                text="First question",
                reasoning="",
                model_id=previous_id,
                model_display_name="Previous model",
                attachment_ids=[],
                created_at=NOW,
            ),
            ChatMessageRow(
                id=uuid.uuid4(),
                conversation_id=session.conversation.id,
                position=2,
                role="assistant",
                text="First answer",
                reasoning="",
                model_id=previous_id,
                model_display_name="Previous model",
                attachment_ids=[],
                created_at=NOW,
            ),
        ]
    )
    session.model = _model()
    session.conversation.revision = 2
    switched = body.model_copy(update={"expected_revision": 2, "model_id": session.model.id})
    admission = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        switched,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    assert [message.content for message in admission.history] == [
        "First question",
        "First answer",
        "Hello",
    ]
    assert session.messages[1].model_id == previous_id
    assert session.messages[-1].model_id == session.model.id
    assert admission.turn.model_display_name == session.model.display_name


async def test_turn_status_refuses_cross_conversation_id() -> None:
    session, principal, body = _case()
    admission = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    admission.turn.conversation_id = uuid.uuid4()

    class StatusSession:
        async def get(self, model: object, _identifier: object) -> object:
            return session.conversation if model is ChatConversationRow else admission.turn

    with pytest.raises(ChatNotFound):
        await read_turn_detail(
            StatusSession(),  # type: ignore[arg-type]
            principal,
            session.conversation.id,
            admission.turn.id,
        )


async def test_send_and_status_routes_use_native_sse_and_owner_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.routes import chat as chat_routes

    session, principal, body = _case()
    admission = await admit_turn(
        session,  # type: ignore[arg-type]
        session.conversation.id,
        principal,
        body,
        Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
    )
    settings = Settings(
        _secrets_dir="/nonexistent",
        chat_enabled=True,
        chat_browser_origin="http://localhost",
        identity_legacy_admin_enabled=True,
        admin_token=SecretStr("chat-turn-contract"),
    )  # type: ignore[call-arg]
    app = create_app(settings)
    app.dependency_overrides[require_principal] = lambda: principal

    async def fake_session():  # type: ignore[no-untyped-def]
        yield session

    async def fake_admit(*_args: object) -> object:
        return admission

    async def fake_stream(*_args: object):  # type: ignore[no-untyped-def]
        yield b"event: turn.accepted\ndata: {}\n\n"

    async def fake_status(*_args: object) -> ChatTurnDetail:
        return ChatTurnDetail(
            turn=project_turn(session.turns[0]),
            input_message=project_message(session.messages[0]),
            assistant_message=project_message(session.messages[1]),
            event_cursor=1,
        )

    app.dependency_overrides[get_session] = fake_session
    monkeypatch.setattr(chat_routes, "admit_turn", fake_admit)
    monkeypatch.setattr(chat_routes, "native_stream", fake_stream)
    monkeypatch.setattr(chat_routes, "read_turn_detail", fake_status)
    path = f"/api/v1/chat/conversations/{session.conversation.id}/turns"
    schema = app.openapi()
    stream_schema = schema["paths"]["/api/v1/chat/conversations/{conversation_id}/turns"]["post"][
        "responses"
    ]["200"]["content"]["text/event-stream"]["schema"]
    assert stream_schema["$ref"] == "#/components/schemas/ChatEvent"
    headers = {"Authorization": "Bearer chat-turn-contract"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        denied = await client.post(path, json=body.model_dump(mode="json"), headers=headers)
        assert denied.status_code == 403
        accepted = await client.post(
            path,
            json=body.model_dump(mode="json"),
            headers={**headers, "Origin": "http://localhost"},
        )
        assert accepted.status_code == 200
        assert accepted.headers["content-type"].startswith("text/event-stream")
        assert accepted.text.startswith("event: turn.accepted")
        status = await client.get(f"{path}/{admission.turn.id}", headers=headers)
        assert status.status_code == 200
        assert status.json()["turn"]["model_display_name"] == "Named model"
