"""Explicit comparison admission and owner selection remain separate from retries."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_feedback_provenance import seed_admission
from test_training_datasets import API
from test_training_datasets import api as api
from test_training_datasets import training_postgres_url as training_postgres_url

from coire_api.db import (
    ChatConversationRow,
    ChatMessageRow,
    ChatTurnRow,
    ComparisonPairRow,
    FeedbackRow,
)
from coire_api.feedback import comparisons
from coire_api.feedback.provenance import capture_native_provenance


async def source(api: API) -> tuple[uuid.UUID, uuid.UUID]:
    admission, resolved, principal = await seed_admission(api)
    api.settings.chat_enabled = True
    async with api.sessions() as session:
        assert await capture_native_provenance(
            session, principal, admission, resolved, api.settings
        )
        turn = await session.get(ChatTurnRow, admission.turn.id)
        message = await session.get(ChatMessageRow, admission.turn.assistant_message_id)
        conversation = await session.get(ChatConversationRow, admission.turn.conversation_id)
        assert turn is not None and message is not None and conversation is not None
        turn.state = "completed"
        message.text = "original answer"
        conversation.active_turn_id = None
        await session.commit()
    return admission.turn.conversation_id, admission.turn.assistant_message_id


async def test_explicit_create_replay_and_one_pending_pair(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, mid = await source(api)
    start = AsyncMock()
    monkeypatch.setattr(comparisons, "start_comparison", start)
    path = f"/api/v1/chat/conversations/{cid}/comparisons"
    body = {
        "client_request_id": str(uuid.uuid4()),
        "expected_revision": 2,
        "source_message_id": str(mid),
    }
    first = await api.client.post(path, json=body, headers=api.headers)
    assert first.status_code == 202, first.text
    receipt = first.json()
    assert receipt["state"] == "queued" and receipt["selection_state"] == "pending"
    assert (await api.client.post(path, json=body, headers=api.headers)).json() == receipt
    changed = {**body, "source_message_id": str(uuid.uuid4())}
    assert (await api.client.post(path, json=changed, headers=api.headers)).status_code == 409
    other = {**body, "client_request_id": str(uuid.uuid4())}
    assert (await api.client.post(path, json=other, headers=api.headers)).status_code == 409
    assert start.await_count == 1
    async with api.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ComparisonPairRow)) == 1
        assert await session.scalar(select(func.count()).select_from(FeedbackRow)) == 0
        pair = await session.get(ComparisonPairRow, receipt["id"])
        assert pair is not None and pair.original == "original answer" and pair.candidate is None
        assert isinstance(pair.execution["seed"], int)
        generation = pair.execution["settings"]
        assert isinstance(generation, dict) and generation["temperature"] == 0.0


async def test_owner_selection_then_rejudgement_does_not_rewrite_context(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, mid = await source(api)
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    path = f"/api/v1/chat/conversations/{cid}/comparisons"
    response = await api.client.post(
        path,
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_revision": 2,
            "source_message_id": str(mid),
        },
    )
    assert response.status_code == 202, response.text
    pair_id = response.json()["id"]
    async with api.sessions() as session:
        pair = await session.get(ComparisonPairRow, pair_id)
        assert pair is not None
        pair.generation_state = "ready"
        pair.candidate = "candidate answer"
        pair.version += 1
        await session.commit()
    selected = await api.client.post(
        path + f"/{pair_id}/selection",
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_version": 2,
            "candidate": "candidate",
            "tags": ["clear"],
        },
    )
    assert selected.status_code == 200, selected.text
    detail = await api.client.get(path + f"/{pair_id}", headers=api.headers)
    assert detail.status_code == 200 and detail.json()["selected"] == "candidate"
    assert detail.json()["owner_tags"] == ["clear"]
    revision = selected.json()["conversation_revision"]
    relabel = await api.client.post(
        path + f"/{pair_id}/selection",
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_version": detail.json()["version"],
            "candidate": "original",
            "tags": [],
        },
    )
    assert relabel.status_code == 200 and relabel.json()["conversation_revision"] == revision
    detail = await api.client.get(path + f"/{pair_id}", headers=api.headers)
    assert (
        detail.json()["selected"] == "candidate"
        and detail.json()["owner_judgement"]["judgement"] == "original"
    )


async def test_dismissal_stops_pending_and_withdraws_copied_detail(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, mid = await source(api)
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    path = f"/api/v1/chat/conversations/{cid}/comparisons"
    response = await api.client.post(
        path,
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_revision": 2,
            "source_message_id": str(mid),
        },
    )
    assert response.status_code == 202, response.text
    pair_id = response.json()["id"]
    body = {"client_request_id": str(uuid.uuid4()), "expected_version": 1}
    dismissed = await api.client.post(path + f"/{pair_id}/dismiss", headers=api.headers, json=body)
    assert dismissed.status_code == 200 and dismissed.json()["selection_state"] == "dismissed"
    assert (
        await api.client.post(path + f"/{pair_id}/dismiss", headers=api.headers, json=body)
    ).json() == dismissed.json()
    detail = await api.client.get(path + f"/{pair_id}", headers=api.headers)
    assert (
        detail.status_code == 200
        and detail.json()["original"] is None
        and detail.json()["candidate"] is None
    )


@pytest.mark.parametrize("answer", ["candidate answer", "original answer"])
async def test_owned_generation_charges_once_and_stores_content_free_events(
    api: API,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    from collections.abc import AsyncIterator

    from coire_api.auth import Principal, PrincipalKind
    from coire_api.db import ChatEventRow, UsageRecordRow
    from coire_api.feedback import comparison_execution

    cid, mid = await source(api)
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    path = f"/api/v1/chat/conversations/{cid}/comparisons"
    response = await api.client.post(
        path,
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_revision": 2,
            "source_message_id": str(mid),
        },
    )
    assert response.status_code == 202
    pair_id = response.json()["id"]
    # Fake registry/proxy results exercise API ownership; no native model work.
    _, resolved, _ = await seed_admission(api)
    async with api.sessions() as session:
        pair = await session.get(ComparisonPairRow, pair_id)
        assert pair is not None
        from dataclasses import replace

        from coire_core.models.adapters import InferenceTarget

        resolved = replace(
            resolved, target=InferenceTarget.model_validate(pair.target), engine_id=None
        )
    monkeypatch.setattr(
        comparison_execution, "resolve_exact_target", AsyncMock(return_value=resolved)
    )

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        yield (
            "data: " + json.dumps({"choices": [{"delta": {"content": answer}}]}) + "\n\n"
        ).encode()
        yield b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(comparison_execution, "stream", upstream)
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=api.owner)
    await comparison_execution.run_comparison(pair_id, principal, api.settings)
    await comparison_execution.run_comparison(pair_id, principal, api.settings)
    async with api.sessions() as session:
        pair = await session.get(ComparisonPairRow, pair_id)
        assert (
            pair is not None
            and pair.generation_state == ("ready" if answer != "original answer" else "identical")
            and pair.candidate == (answer if answer != "original answer" else None)
        )
        assert await session.scalar(select(func.count()).select_from(UsageRecordRow)) == 1
        events = list(
            await session.scalars(select(ChatEventRow).where(ChatEventRow.conversation_id == cid))
        )
        assert any(event.type == "comparison.delta" for event in events)
        assert all(answer not in str(event.payload) for event in events)
        delta = next(event for event in events if event.type == "comparison.delta")
    projected = await comparisons.replay_comparison_event(principal, delta)
    from coire_core.models.feedback import ComparisonEvent

    assert projected is not None and isinstance(projected.payload, ComparisonEvent)
    assert projected.payload.text_delta == (answer if answer != "original answer" else None)
    disabled = await api.client.patch(
        "/api/v1/chat/feedback-preference",
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_version": 1,
            "enabled": False,
            "disclosure_version": "feedback-v1",
        },
    )
    assert disabled.status_code == 200
    tombstone = await comparisons.replay_comparison_event(principal, delta)
    assert tombstone is not None and isinstance(tombstone.payload, ComparisonEvent)
    assert tombstone.payload.text_delta is None


async def test_compare_quota_extra_inputs_stale_and_legacy_source(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, mid = await source(api)
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    path = f"/api/v1/chat/conversations/{cid}/comparisons"
    body = {
        "client_request_id": str(uuid.uuid4()),
        "expected_revision": 2,
        "source_message_id": str(mid),
    }
    assert (
        await api.client.post(path, headers=api.headers, json={**body, "model": "caller/model"})
    ).status_code == 422
    assert (
        await api.client.post(path, headers=api.headers, json={**body, "expected_revision": 1})
    ).status_code == 409
    assert (
        await api.client.post(
            path.replace(str(cid), str(uuid.uuid4())), headers=api.headers, json=body
        )
    ).status_code == 404
    api.settings.feedback_storage_quota_bytes = 1
    assert (await api.client.post(path, headers=api.headers, json=body)).status_code == 429
    api.settings.feedback_storage_quota_bytes = 1024**3
    from sqlalchemy import delete

    from coire_api.db import ChatFeedbackProvenanceRow

    async with api.sessions() as session:
        await session.execute(delete(ChatFeedbackProvenanceRow))
        await session.commit()
    unavailable = await api.client.post(path, headers=api.headers, json=body)
    assert unavailable.status_code == 422 and "source_provenance_unavailable" in unavailable.text
    assert "original answer" not in unavailable.text


async def test_retired_exact_source_is_unavailable_before_admission(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_api.db import ModelRow
    from coire_core.models.registry import ModelState

    cid, mid = await source(api)
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    async with api.sessions() as session:
        model = await session.scalar(select(ModelRow))
        assert model is not None
        model.state = ModelState.RETIRED
        await session.commit()
    response = await api.client.post(
        f"/api/v1/chat/conversations/{cid}/comparisons",
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_revision": 2,
            "source_message_id": str(mid),
        },
    )
    assert response.status_code == 503 and "original answer" not in response.text
    async with api.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ComparisonPairRow)) == 0


async def test_crash_after_reply_commit_settles_once_after_body_erasure(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import AsyncIterator
    from dataclasses import replace

    from coire_api.db import UsageRecordRow
    from coire_api.feedback import comparison_execution
    from coire_api.feedback.accounting import settle_comparison
    from coire_api.gateway.usage import UsageTracker
    from coire_core.models.adapters import InferenceTarget

    cid, mid = await source(api)
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    response = await api.client.post(
        f"/api/v1/chat/conversations/{cid}/comparisons",
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_revision": 2,
            "source_message_id": str(mid),
        },
    )
    assert response.status_code == 202
    pair_id = response.json()["id"]
    _, resolved, principal = await seed_admission(api)
    async with api.sessions() as session:
        pair = await session.get(ComparisonPairRow, pair_id)
        assert pair is not None
        resolved = replace(
            resolved, target=InferenceTarget.model_validate(pair.target), engine_id=None
        )
    monkeypatch.setattr(
        comparison_execution, "resolve_exact_target", AsyncMock(return_value=resolved)
    )

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"durable candidate"}}]}\n\n'
        yield b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(comparison_execution, "stream", upstream)
    monkeypatch.setattr(
        UsageTracker,
        "finish",
        AsyncMock(side_effect=RuntimeError("simulated process loss")),
    )
    with pytest.raises(RuntimeError, match="simulated process loss"):
        await comparison_execution.run_comparison(pair_id, principal, api.settings)
    async with api.sessions() as session:
        pair = await session.get(ComparisonPairRow, pair_id)
        assert pair is not None and pair.generation_state == "ready"
        await comparisons.erase_pair(session, pair)
        await session.commit()
        assert pair.original is None and pair.candidate is None
    assert await settle_comparison(pair_id)
    assert not await settle_comparison(pair_id)
    async with api.sessions() as session:
        records = list(await session.scalars(select(UsageRecordRow)))
        assert (
            len(records) == 1
            and records[0].prompt_tokens == 2
            and records[0].completion_tokens == 3
        )


@pytest.mark.parametrize("withdrawal", ["disable", "delete"])
async def test_withdrawal_between_stream_frames_refuses_late_copy_and_replay(
    api: API, monkeypatch: pytest.MonkeyPatch, withdrawal: str
) -> None:
    import time
    from collections.abc import AsyncIterator
    from dataclasses import replace

    from coire_api.auth import Principal, PrincipalKind
    from coire_api.db import ChatEventRow
    from coire_api.feedback import comparison_execution
    from coire_api.feedback.retention import purge_sources
    from coire_core.models.adapters import InferenceTarget

    cid, mid = await source(api)
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    response = await api.client.post(
        f"/api/v1/chat/conversations/{cid}/comparisons",
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_revision": 2,
            "source_message_id": str(mid),
        },
    )
    assert response.status_code == 202
    pair_id = response.json()["id"]
    _, resolved, _ = await seed_admission(api)
    async with api.sessions() as session:
        pair = await session.get(ComparisonPairRow, pair_id)
        assert pair is not None
        resolved = replace(
            resolved, target=InferenceTarget.model_validate(pair.target), engine_id=None
        )
    monkeypatch.setattr(
        comparison_execution, "resolve_exact_target", AsyncMock(return_value=resolved)
    )

    async def upstream(*_args: object) -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"private first frame"}}]}\n\n'
        if withdrawal == "disable":
            result = await api.client.patch(
                "/api/v1/chat/feedback-preference",
                headers=api.headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "expected_version": 1,
                    "enabled": False,
                    "disclosure_version": "feedback-v1",
                },
            )
            assert result.status_code == 200
        else:
            async with api.sessions() as session:
                conversation = await session.get(ChatConversationRow, cid)
                assert conversation is not None
                revision = conversation.revision
            result = await api.client.request(
                "DELETE",
                f"/api/v1/chat/conversations/{cid}",
                headers=api.headers,
                json={"expected_revision": revision},
            )
            assert result.status_code == 202, result.text
        yield b'data: {"choices":[{"delta":{"content":"private late frame"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(comparison_execution, "stream", upstream)
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=api.owner)
    await comparison_execution.run_comparison(pair_id, principal, api.settings)
    refused = await api.client.get(
        f"/api/v1/admin/feedback/comparisons/{pair_id}", headers=api.headers
    )
    assert refused.status_code in {403, 404} and "private" not in refused.text
    started = time.monotonic()
    async with api.sessions() as session:
        pair = await session.get(ComparisonPairRow, pair_id)
        assert pair is not None and pair.generation_state == "withdrawn"
        assert "late frame" not in (pair.candidate or "")
        events = list(
            await session.scalars(
                select(ChatEventRow).where(
                    ChatEventRow.conversation_id == cid, ChatEventRow.type == "comparison.delta"
                )
            )
        )
        assert len(events) == 1 and "private" not in str(events[0].payload)
        await purge_sources(session, batch_size=100)
        await session.commit()
        await session.refresh(pair)
        assert pair.original is None and pair.candidate is None and pair.prompt is None
        assert pair.purged_at is not None and pair.counted_bytes == 0
    assert time.monotonic() - started < 24 * 3600
    replay = await comparisons.replay_comparison_event(principal, events[0])
    assert replay is None or "private" not in replay.model_dump_json()
