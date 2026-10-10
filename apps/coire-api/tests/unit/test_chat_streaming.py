"""Native text streaming uses the existing proxy and persists each event before emission."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_api.chat.streaming import _observed_load_state, native_stream, replay_saved_events
from coire_api.chat.turns import Admission
from coire_api.db import ChatConversationRow, ChatEventRow, ChatTurnRow, ModelRow, UserRow
from coire_api.gateway.proxy import EngineProxyError
from coire_api.gateway.resolution import ResolvedModel
from coire_core.errors import ChatNotFound
from coire_core.models.adapters import InferenceTarget
from coire_core.models.chat import (
    ChatEvent,
    ChatMessageDelta,
    ChatTurnAccepted,
    ChatTurnStatus,
    ChatTurnTerminal,
)
from coire_core.models.gateway import ChatMessage as GatewayMessage
from coire_core.models.gateway import UsageOutcome
from coire_core.models.instance import InstanceState
from coire_core.models.registry import ModelSource, ModelState, Reasoning, Visibility
from coire_core.settings import Settings

NOW = datetime.now(UTC)


@pytest.mark.parametrize(
    ("instance_state", "expected"),
    [
        (InstanceState.REQUESTED, "queued"),
        (InstanceState.RESERVING, "queued"),
        (InstanceState.LAUNCHING, "loading"),
        (InstanceState.WARMING, "loading"),
        (None, None),
    ],
)
async def test_load_status_uses_observed_instance_transition(
    monkeypatch: pytest.MonkeyPatch,
    instance_state: InstanceState | None,
    expected: str | None,
) -> None:
    from coire_api.chat import streaming

    class Session:
        async def scalar(self, _statement: object) -> InstanceState | None:
            return instance_state

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(streaming, "session_scope", sessions)
    assert await _observed_load_state(uuid.uuid4()) == expected


@pytest.mark.parametrize("pin_variant", [False, True])
async def test_cold_turn_reports_observed_queue_before_running(
    monkeypatch: pytest.MonkeyPatch,
    pin_variant: bool,
) -> None:
    import asyncio

    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    release_load = asyncio.Event()
    statuses: list[str] = []
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "_stop_requested", AsyncMock(return_value=False))
    target = (
        InferenceTarget(
            model_id=admission.turn.model_id, variant_id=uuid.uuid4(), base_manifest_sha256="a" * 64
        )
        if pin_variant
        else None
    )
    resolver = AsyncMock(
        side_effect=[
            replace(_resolved(admission, cold=True), target=target),
            replace(_resolved(admission), target=target),
        ]
    )
    monkeypatch.setattr(streaming, "_resolve", resolver)
    monkeypatch.setattr(streaming, "_measured_warmup_seconds", AsyncMock(return_value=None))
    monkeypatch.setattr(streaming, "_observed_load_state", AsyncMock(return_value="queued"))

    async def load_model(_model_id: uuid.UUID | InferenceTarget, _settings: Settings) -> None:
        assert _model_id == (target or admission.turn.model_id)
        await release_load.wait()

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        yield b"data: [DONE]\n\n"

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        if kind == "status":
            state = str(kwargs.get("state"))
            statuses.append(state)
            if state == "queued":
                release_load.set()
        return _saved_event(admission, kind, len(statuses) + 1, **kwargs)

    monkeypatch.setattr("coire_api.gateway.execution.load_model", load_model)
    monkeypatch.setattr(streaming, "stream", upstream)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", AsyncMock())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    _ = [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert statuses == ["loading", "queued", "running"]
    if pin_variant:
        assert resolver.await_args_list[-1].kwargs == {"target": target}


async def test_closing_native_cold_stream_cancels_pending_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    cancelled = asyncio.Event()
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "_stop_requested", AsyncMock(return_value=False))
    monkeypatch.setattr(
        streaming, "_resolve", AsyncMock(return_value=_resolved(admission, cold=True))
    )
    monkeypatch.setattr(streaming, "_measured_warmup_seconds", AsyncMock(return_value=None))
    monkeypatch.setattr(streaming, "_observed_load_state", AsyncMock(return_value=None))
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", AsyncMock())

    async def load(_model_id: uuid.UUID, _settings: Settings) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        return _saved_event(admission, kind, 2, **kwargs)

    monkeypatch.setattr("coire_api.gateway.execution.load_model", load)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", gateway_keepalive_interval_s=0.01
    )
    source = native_stream(admission, principal, request, settings)  # type: ignore[arg-type]
    assert b"turn.accepted" in await anext(source)
    assert b"turn.status" in await anext(source)
    assert await anext(source) == b": coire model loading\n\n"
    await cast(AsyncGenerator[bytes], source).aclose()
    assert cancelled.is_set()


def _admission() -> Admission:
    conversation_id, model_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = ChatTurnRow(
        id=turn_id,
        conversation_id=conversation_id,
        client_request_id=uuid.uuid4(),
        request_hash="a" * 64,
        accepted_revision=1,
        input_message_id=uuid.uuid4(),
        assistant_message_id=uuid.uuid4(),
        model_id=model_id,
        model_display_name="Named model",
        action="chat",
        state="accepted",
        created_at=NOW,
        updated_at=NOW,
    )
    from coire_api.chat.turns import project_turn

    event = ChatEvent(
        conversation_id=conversation_id,
        cursor=1,
        turn_id=turn_id,
        created_at=NOW,
        payload=ChatTurnAccepted(turn=project_turn(row)),
    )
    return Admission(row, event, [GatewayMessage(role="user", content="hello")], 7, False)


@asynccontextmanager
async def _sessions() -> AsyncIterator[object]:
    yield object()


def _resolved(admission: Admission, *, cold: bool = False) -> ResolvedModel:
    return ResolvedModel(
        admission.turn.model_id,
        "safe",
        4096,
        None if cold else "/opt/coire/models/safe",
        None if cold else uuid.uuid4(),
        None if cold else "edge",
        None if cold else "http://engine",
    )


def _saved_event(admission: Admission, kind: str, cursor: int, **kwargs: object) -> ChatEvent:
    if kind == "status":
        payload = ChatTurnStatus(state=kwargs["state"])  # type: ignore[arg-type]
    elif kind == "delta":
        payload = ChatMessageDelta(  # type: ignore[assignment]
            message_id=admission.turn.assistant_message_id,
            channel="answer",
            text=str(kwargs["text"]),
            offset=len(str(kwargs["text"])),
        )
    else:
        payload = ChatTurnTerminal(  # type: ignore[assignment]
            state=kwargs["state"],  # type: ignore[arg-type]
            answer_length=0,
            reasoning_length=0,
        )
    return ChatEvent(
        conversation_id=admission.turn.conversation_id,
        cursor=cursor,
        turn_id=admission.turn.id,
        created_at=NOW,
        payload=payload,
    )


async def test_text_stream_persists_before_each_native_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    sequence: list[str] = []
    model_path: list[str] = []
    actual_usage: list[object] = []
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "resolve_model", AsyncMock(return_value=_resolved(admission)))

    async def upstream(
        _url: str, payload: dict[str, object], _settings: Settings, _timing: object
    ) -> AsyncIterator[bytes]:
        model_path.append(str(payload["model"]))
        yield b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        yield b'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":3}}\n\n'
        yield b"data: [DONE]\n\n"

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        sequence.append("persist:" + kind)
        if kind == "terminal":
            actual_usage.append(kwargs.get("usage"))
        return _saved_event(admission, kind, len(sequence) + 1, **kwargs)

    async def persist_usage(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(streaming, "stream", upstream)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist_usage)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    chunks = [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert model_path == ["/opt/coire/models/safe"]
    assert b"/opt/coire/models" not in b"".join(chunks)
    assert b"Hello" not in chunks[0]
    assert sequence == ["persist:status", "persist:delta", "persist:terminal"]
    assert actual_usage[0].prompt_tokens == 8  # type: ignore[attr-defined]
    assert actual_usage[0].completion_tokens == 3  # type: ignore[attr-defined]
    assert chunks[-1].startswith(b"event: turn.terminal")


async def test_native_provider_turn_uses_remote_stream_without_studio_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    kinds: list[str] = []
    reserved: list[uuid.UUID] = []
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "_stop_requested", AsyncMock(return_value=False))
    monkeypatch.setattr(
        streaming,
        "_resolve",
        AsyncMock(
            return_value=ResolvedModel(
                admission.turn.model_id,
                "openai--example",
                4096,
                None,
                None,
                None,
                None,
                source=ModelSource.OPENAI,
                provider_model_id="gpt-example",
                max_output_tokens=64,
                daily_token_budget=1000,
            )
        ),
    )

    async def reserve(
        _session: object, _resolved: object, _history: object, _output: int, request_id: uuid.UUID
    ) -> int:
        reserved.append(request_id)
        return 100

    async def remote(*_args: object) -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        yield b'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":2}}\n\n'
        yield b"data: [DONE]\n\n"

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        kinds.append(kind)
        return _saved_event(admission, kind, len(kinds) + 1, **kwargs)

    monkeypatch.setattr(streaming, "reserve_provider_budget", reserve)
    monkeypatch.setattr(streaming, "provider_stream", remote)
    monkeypatch.setattr(
        streaming, "load_with_ceiling", AsyncMock(side_effect=AssertionError("Studio load"))
    )
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", AsyncMock())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        provider_chat_enabled=True,
        openai_api_key=SecretStr("provider-test-secret"),
    )
    chunks = [chunk async for chunk in native_stream(admission, principal, request, settings)]  # type: ignore[arg-type]
    assert kinds == ["status", "delta", "terminal"]
    assert len(reserved) == 1
    assert b"Hello" in b"".join(chunks)


async def test_native_provider_stop_closes_paid_http_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from pydantic import SecretStr

    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    closed = asyncio.Event()
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "_stop_requested", AsyncMock(return_value=True))
    monkeypatch.setattr(
        streaming,
        "_resolve",
        AsyncMock(
            return_value=ResolvedModel(
                admission.turn.model_id,
                "anthropic--example",
                4096,
                None,
                None,
                None,
                None,
                source=ModelSource.ANTHROPIC,
                provider_model_id="claude-example",
                max_output_tokens=64,
                daily_token_budget=1000,
            )
        ),
    )

    async def reserve(*_args: object) -> int:
        return 100

    async def remote(*_args: object) -> AsyncIterator[bytes]:
        try:
            await asyncio.Event().wait()
            yield b""
        finally:
            closed.set()

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        return _saved_event(admission, kind, 2, **kwargs)

    monkeypatch.setattr(streaming, "reserve_provider_budget", reserve)
    monkeypatch.setattr(streaming, "provider_stream", remote)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", AsyncMock())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        provider_chat_enabled=True,
        anthropic_api_key=SecretStr("provider-test-secret"),
    )
    chunks = [chunk async for chunk in native_stream(admission, principal, request, settings)]  # type: ignore[arg-type]
    assert closed.is_set()
    assert b'"state":"stopped"' in chunks[-1]


@pytest.mark.parametrize(
    ("engine_frames", "reason"),
    [
        ([b"data: {broken}\n\n", b"data: [DONE]\n\n"], "malformed_frame"),
        ([b'data: {"choices":[]}\n\n'], "missing_done"),
    ],
)
async def test_bad_native_engine_stream_counts_one_bounded_parser_failure(
    monkeypatch: pytest.MonkeyPatch, engine_frames: list[bytes], reason: str
) -> None:
    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    recorded: list[tuple[int, dict[str, str]]] = []
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "_resolve", AsyncMock(return_value=_resolved(admission)))
    monkeypatch.setattr(streaming, "_stop_requested", AsyncMock(return_value=False))
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", AsyncMock())
    monkeypatch.setattr(
        streaming,
        "parser_failures_total",
        SimpleNamespace(add=lambda value, attrs: recorded.append((value, attrs))),
    )

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        for frame in engine_frames:
            yield frame

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        return _saved_event(admission, kind, 2, **kwargs)

    monkeypatch.setattr(streaming, "stream", upstream)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    chunks = [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert recorded == [(1, {"reason": reason})]
    assert b'"state":"failed"' in chunks[-1]


async def test_reasoning_stream_saves_split_thinking_in_separate_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.chat import streaming

    admission = _admission()
    admission = Admission(
        admission.turn,
        admission.event,
        admission.history,
        admission.prompt_tokens,
        False,
        admission.output_tokens,
        Reasoning.THINKING,
    )
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    deltas: list[tuple[object, object]] = []
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "resolve_model", AsyncMock(return_value=_resolved(admission)))

    async def upstream(
        _url: str, _payload: dict[str, object], _settings: Settings, _timing: object
    ) -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"<thi"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"nk>private</think>Answer"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        if kind == "delta":
            deltas.append((kwargs.get("channel"), kwargs.get("text")))
        return _saved_event(admission, kind, len(deltas) + 1, **kwargs)

    monkeypatch.setattr(streaming, "stream", upstream)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", AsyncMock())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    chunks = [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert deltas == [("reasoning", "private"), ("answer", "Answer")]
    assert chunks[-1].startswith(b"event: turn.terminal")


async def test_reasoning_content_frame_uses_reasoning_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.chat import streaming

    original = _admission()
    admission = Admission(
        original.turn,
        original.event,
        original.history,
        original.prompt_tokens,
        False,
        original.output_tokens,
        Reasoning.THINKING,
    )
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    deltas: list[tuple[object, object]] = []
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "resolve_model", AsyncMock(return_value=_resolved(admission)))

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"reasoning_content":"private","content":"Public"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        if kind == "delta":
            deltas.append((kwargs.get("channel"), kwargs.get("text")))
        return _saved_event(admission, kind, len(deltas) + 1, **kwargs)

    monkeypatch.setattr(streaming, "stream", upstream)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", AsyncMock())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    _ = [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert deltas == [("answer", "Public"), ("reasoning", "private")]


async def test_stop_with_split_opening_marker_never_emits_reasoning_as_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from coire_api.chat import streaming

    original = _admission()
    admission = Admission(
        original.turn,
        original.event,
        original.history,
        original.prompt_tokens,
        False,
        original.output_tokens,
        Reasoning.THINKING,
    )
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    first_chunk = asyncio.Event()
    deltas: list[object] = []
    states: list[object] = []
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "resolve_model", AsyncMock(return_value=_resolved(admission)))

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"<thi"}}]}\n\n'
        first_chunk.set()
        await asyncio.Event().wait()

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        if kind == "delta":
            deltas.append(kwargs.get("text"))
        if kind == "terminal":
            states.append(kwargs.get("state"))
        return _saved_event(admission, kind, len(deltas) + len(states) + 1, **kwargs)

    async def stop_requested(_turn_id: uuid.UUID) -> bool:
        return first_chunk.is_set()

    monkeypatch.setattr(streaming, "_stop_requested", stop_requested)
    monkeypatch.setattr(streaming, "stream", upstream)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", AsyncMock())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    _ = [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert deltas == []
    assert states == ["stopped"]


@pytest.mark.parametrize("estimate", [None, 42.5])
async def test_cold_then_ready_emits_loading_before_generation(
    monkeypatch: pytest.MonkeyPatch,
    estimate: float | None,
) -> None:
    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(
        streaming,
        "resolve_model",
        AsyncMock(side_effect=[_resolved(admission, cold=True), _resolved(admission)]),
    )
    monkeypatch.setattr("coire_api.gateway.execution.load_model", AsyncMock())
    monkeypatch.setattr(streaming, "_measured_warmup_seconds", AsyncMock(return_value=estimate))

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        yield b"data: [DONE]\n\n"

    cursor = 1
    estimates: list[float | None] = []

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        nonlocal cursor
        cursor += 1
        if kind == "status" and kwargs.get("state") == "loading":
            estimates.append(kwargs.get("estimate_seconds"))  # type: ignore[arg-type]
        return _saved_event(admission, kind, cursor, **kwargs)

    monkeypatch.setattr(streaming, "stream", upstream)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", AsyncMock())
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    chunks = [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert any(b"event: turn.status" in chunk for chunk in chunks)
    assert any(b"turn.terminal" in chunk for chunk in chunks)
    assert estimates == [estimate]


async def test_cold_load_failure_has_safe_actionable_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(
        streaming, "resolve_model", AsyncMock(return_value=_resolved(admission, cold=True))
    )
    monkeypatch.setattr(streaming, "_measured_warmup_seconds", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "coire_api.gateway.execution.load_model",
        AsyncMock(side_effect=RuntimeError("private node failure")),
    )
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", AsyncMock())
    saved_errors: list[str | None] = []

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        if kind == "terminal":
            saved_errors.append(kwargs.get("safe_error"))  # type: ignore[arg-type]
        return _saved_event(admission, kind, len(saved_errors) + 2, **kwargs)

    monkeypatch.setattr(streaming, "persist_native_event", saved)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    chunks = [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert saved_errors == ["model warm-up failed; try again or choose another model"]
    assert b"private node failure" not in b"".join(chunks)


async def test_owner_stop_closes_upstream_and_saves_partial_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "resolve_model", AsyncMock(return_value=_resolved(admission)))
    saved_kinds: list[str] = []
    terminal_states: list[str] = []
    usage_outcomes: list[UsageOutcome] = []
    upstream_closed = False

    async def stop_requested(_turn_id: uuid.UUID) -> bool:
        return "delta" in saved_kinds

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        nonlocal upstream_closed
        try:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await asyncio.Event().wait()
        finally:
            upstream_closed = True

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        saved_kinds.append(kind)
        if kind == "terminal":
            terminal_states.append(str(kwargs.get("state")))
        return _saved_event(admission, kind, len(saved_kinds) + 1, **kwargs)

    async def persist_usage(**kwargs: object) -> None:
        usage_outcomes.append(kwargs["outcome"])  # type: ignore[arg-type]

    monkeypatch.setattr(streaming, "_stop_requested", stop_requested)
    monkeypatch.setattr(streaming, "stream", upstream)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist_usage)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    async def consume() -> list[bytes]:
        return [
            chunk
            async for chunk in native_stream(
                admission,
                principal,
                request,  # type: ignore[arg-type]
                Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
            )
        ]

    chunks = await asyncio.wait_for(consume(), timeout=3)
    assert saved_kinds == ["status", "delta", "terminal"]
    assert terminal_states == ["stopped"]
    assert upstream_closed
    assert usage_outcomes == [UsageOutcome.STOPPED]
    assert b"partial" in b"".join(chunks)


async def test_owner_stop_during_cold_load_finishes_without_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(
        streaming, "resolve_model", AsyncMock(return_value=_resolved(admission, cold=True))
    )
    monkeypatch.setattr(streaming, "_measured_warmup_seconds", AsyncMock(return_value=None))
    monkeypatch.setattr(streaming, "_stop_requested", AsyncMock(return_value=True))
    generated = AsyncMock()
    monkeypatch.setattr(streaming, "stream", generated)
    saved_states: list[object] = []
    usage_outcomes: list[UsageOutcome] = []

    async def load(*_args: object) -> None:
        await asyncio.Event().wait()

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        saved_states.append(kwargs.get("state"))
        return _saved_event(admission, kind, len(saved_states) + 1, **kwargs)

    async def persist_usage(**kwargs: object) -> None:
        usage_outcomes.append(kwargs["outcome"])  # type: ignore[arg-type]

    monkeypatch.setattr("coire_api.gateway.execution.load_model", load)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist_usage)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    chunks = await asyncio.wait_for(consume_native(admission, principal, request), timeout=3)
    assert saved_states == ["loading", "stopped"]
    assert usage_outcomes == [UsageOutcome.STOPPED]
    generated.assert_not_called()
    assert chunks[-1].startswith(b"event: turn.terminal")


async def test_navigation_abort_after_durable_stop_saves_stopped_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "resolve_model", AsyncMock(return_value=_resolved(admission)))
    stopped = False
    terminal_states: list[str] = []
    outcomes: list[UsageOutcome] = []

    async def stop_requested(_turn_id: uuid.UUID) -> bool:
        return stopped

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        await asyncio.Event().wait()

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        if kind == "terminal":
            terminal_states.append(str(kwargs.get("state")))
        return _saved_event(admission, kind, 2, **kwargs)

    async def persist_usage(**kwargs: object) -> None:
        outcomes.append(kwargs["outcome"])  # type: ignore[arg-type]

    monkeypatch.setattr(streaming, "_stop_requested", stop_requested)
    monkeypatch.setattr(streaming, "stream", upstream)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist_usage)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    source = native_stream(admission, principal, request, Settings(_secrets_dir="/nonexistent"))  # type: ignore[arg-type,call-arg]
    assert b"turn.accepted" in await anext(source)
    assert b"turn.status" in await anext(source)
    assert b"partial" in await anext(source)
    stopped = True
    await source.aclose()  # type: ignore[attr-defined]
    assert terminal_states == ["stopped"]
    assert outcomes == [UsageOutcome.STOPPED]


async def consume_native(
    admission: Admission, principal: Principal, request: object
) -> list[bytes]:
    return [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]


@pytest.mark.parametrize("failure", ["engine", "disconnect"])
async def test_failure_or_disconnect_saves_safe_terminal(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    saved_kinds: list[tuple[str, object]] = []
    usage_outcomes: list[UsageOutcome] = []
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "resolve_model", AsyncMock(return_value=_resolved(admission)))

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        if failure == "engine":
            raise EngineProxyError("private stack details")
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'

    async def saved(kind: str, _admission: Admission, **kwargs: object) -> ChatEvent:
        saved_kinds.append((kind, kwargs.get("state")))
        return _saved_event(admission, kind, len(saved_kinds) + 1, **kwargs)

    async def persist_usage(**kwargs: object) -> None:
        usage_outcomes.append(kwargs["outcome"])  # type: ignore[arg-type]

    monkeypatch.setattr(streaming, "stream", upstream)
    monkeypatch.setattr(streaming, "persist_native_event", saved)
    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist_usage)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=failure == "disconnect"))
    chunks = [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert saved_kinds[-1][0] == "terminal"
    assert saved_kinds[-1][1] in {"failed", "interrupted"}
    assert b"private stack details" not in b"".join(chunks)
    assert usage_outcomes == [
        UsageOutcome.FAILED if failure == "engine" else UsageOutcome.DISCONNECTED
    ]


async def test_duplicate_replays_saved_events_without_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.chat import streaming

    admission = _admission()
    owner_id = uuid.uuid4()
    principal = Principal(kind=PrincipalKind.USER, user_id=owner_id)
    conversation = ChatConversationRow(
        id=admission.turn.conversation_id,
        owner_user_id=owner_id,
        title="Saved",
        mode="chat",
        revision=2,
        event_cursor=1,
        created_at=NOW,
        updated_at=NOW,
    )
    turn = admission.turn
    turn.state = "completed"
    event = admission.event
    assert event is not None
    saved = ChatEventRow(
        id=uuid.uuid4(),
        conversation_id=event.conversation_id,
        turn_id=event.turn_id,
        cursor=event.cursor,
        type=event.payload.type,
        payload=event.payload.model_dump(mode="json"),
        created_at=NOW,
        expires_at=NOW,
    )

    class Session:
        async def get(self, model: object, _identifier: object) -> object:
            if model is UserRow:
                return SimpleNamespace(active=True)
            return conversation if model is ChatConversationRow else turn

        async def execute(self, _statement: object) -> object:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [saved]))

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(streaming, "session_scope", sessions)
    monkeypatch.setattr(
        streaming, "stream", AsyncMock(side_effect=AssertionError("duplicate invoked engine"))
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    chunks = [
        chunk
        async for chunk in replay_saved_events(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert len(chunks) == 1
    assert chunks[0].startswith(b"event: turn.accepted")


async def test_entitlement_revocation_stops_stream_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from coire_api.chat import streaming

    owner_id, model_id = uuid.uuid4(), uuid.uuid4()
    principal = Principal(
        kind=PrincipalKind.USER, user_id=owner_id, entitlements=frozenset({"team-a"})
    )
    user = UserRow(id=owner_id, email="owner@example.test", display_name="Owner", active=True)
    model = ModelRow(
        id=model_id,
        repo_id="owner/model",
        slug="owner--model",
        display_name="Model",
        state=ModelState.READY,
        visibility=Visibility.PUBLISHED,
        entitlement=["team-a"],
    )

    class Session:
        async def get(self, cls: object, _identifier: object) -> object:
            return user if cls is UserRow else model

        async def execute(self, _statement: object) -> object:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(streaming, "session_scope", sessions)
    with pytest.raises(ChatNotFound):
        await streaming._ensure_current_access(principal, model_id)


async def test_persistence_failure_closes_stream_without_engine_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from coire_api.chat import streaming

    admission = _admission()
    principal = Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4())
    outcomes: list[UsageOutcome] = []
    monkeypatch.setattr(streaming, "_ensure_current_access", AsyncMock())
    monkeypatch.setattr(streaming, "session_scope", _sessions)
    monkeypatch.setattr(streaming, "resolve_model", AsyncMock(return_value=_resolved(admission)))
    monkeypatch.setattr(
        streaming,
        "persist_native_event",
        AsyncMock(side_effect=RuntimeError("private database error")),
    )

    async def persist_usage(**kwargs: object) -> None:
        outcomes.append(kwargs["outcome"])  # type: ignore[arg-type]

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist_usage)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    chunks = [
        chunk
        async for chunk in native_stream(
            admission,
            principal,
            request,  # type: ignore[arg-type]
            Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        )
    ]
    assert len(chunks) == 1
    assert b"private database error" not in chunks[0]
    assert "private database error" not in caplog.text
    assert outcomes == [UsageOutcome.FAILED]
