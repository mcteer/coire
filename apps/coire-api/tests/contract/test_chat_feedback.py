"""Thumb decisions are explicit, owner scoped and never preference pairs."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from test_training_datasets import API
from test_training_datasets import api as api
from test_training_datasets import training_postgres_url as training_postgres_url

from coire_api.db import ChatConversationRow, ChatMessageRow, ComparisonPairRow, FeedbackRow


async def seed_chat(api: API) -> tuple[uuid.UUID, uuid.UUID]:
    api.settings.chat_enabled = True
    conversation_id, message_id = uuid.uuid4(), uuid.uuid4()
    async with api.sessions() as session:
        session.add(
            ChatConversationRow(
                id=conversation_id, owner_user_id=api.owner, title="Private", mode="chat"
            )
        )
        await session.flush()
        session.add(
            ChatMessageRow(
                id=message_id,
                conversation_id=conversation_id,
                position=1,
                role="assistant",
                text="private answer",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return conversation_id, message_id


async def test_thumbs_clear_replay_and_foreign_identity(api: API) -> None:
    conversation_id, message_id = await seed_chat(api)
    path = f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback"
    body = {
        "client_request_id": str(uuid.uuid4()),
        "expected_version": 0,
        "judgement": "up",
        "tags": ["helpful"],
    }
    response = await api.client.put(path, json=body, headers=api.headers)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["judgement"] == "up" and receipt["version"] == 1
    assert (await api.client.put(path, json=body, headers=api.headers)).json() == receipt
    changed = {**body, "judgement": "down"}
    assert (await api.client.put(path, json=changed, headers=api.headers)).status_code == 409
    clear = {
        "client_request_id": str(uuid.uuid4()),
        "expected_version": 1,
        "judgement": None,
        "tags": [],
    }
    assert (await api.client.put(path, json=clear, headers=api.headers)).json()["judgement"] is None
    assert (
        await api.client.put(
            path.replace(str(message_id), str(uuid.uuid4())), json=body, headers=api.headers
        )
    ).status_code == 404
    current = await api.client.get(
        f"/api/v1/chat/conversations/{conversation_id}/feedback", headers=api.headers
    )
    assert (
        current.status_code == 200
        and current.json()["items"][0]["eligibility"] == "source_provenance_unavailable"
    )
    async with api.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ComparisonPairRow)) == 0
        row = await session.scalar(select(FeedbackRow))
        assert row is not None and row.judgement is None and row.version == 2


async def test_capture_disabled_refuses_thumb_and_replay_without_source_text(api: API) -> None:
    conversation_id, message_id = await seed_chat(api)
    setting = await api.client.get("/api/v1/chat/feedback-preference", headers=api.headers)
    assert setting.status_code == 200
    disabled = await api.client.patch(
        "/api/v1/chat/feedback-preference",
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_version": setting.json()["version"],
            "enabled": False,
            "disclosure_version": "feedback-v1",
        },
    )
    assert disabled.status_code == 200
    response = await api.client.put(
        f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
        headers=api.headers,
        json={"client_request_id": str(uuid.uuid4()), "expected_version": 0, "judgement": "up"},
    )
    assert response.status_code == 403 and "private answer" not in response.text


async def test_reenable_does_not_revive_old_thumb_command(api: API) -> None:
    conversation_id, message_id = await seed_chat(api)
    path = f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback"
    old = {"client_request_id": str(uuid.uuid4()), "expected_version": 0, "judgement": "up"}
    first = await api.client.put(path, json=old, headers=api.headers)
    assert first.status_code == 200
    for version, enabled in [(1, False), (2, True)]:
        changed = await api.client.patch(
            "/api/v1/chat/feedback-preference",
            headers=api.headers,
            json={
                "client_request_id": str(uuid.uuid4()),
                "expected_version": version,
                "enabled": enabled,
                "disclosure_version": "feedback-v1",
            },
        )
        assert changed.status_code == 200, changed.text
    fresh = {"client_request_id": str(uuid.uuid4()), "expected_version": 1, "judgement": "down"}
    page = await api.client.get(
        f"/api/v1/chat/conversations/{conversation_id}/feedback", headers=api.headers
    )
    assert page.json()["items"][0]["feedback"] is None
    fresh["expected_version"] = 0
    assert (await api.client.put(path, json=fresh, headers=api.headers)).status_code == 200
    assert (await api.client.put(path, json=old, headers=api.headers)).status_code == 403


async def test_thumb_replay_projects_current_decision_and_bounded_page(api: API) -> None:
    conversation_id, message_id = await seed_chat(api)
    path = f"/api/v1/chat/conversations/{conversation_id}/messages/{message_id}/feedback"
    first = {"client_request_id": str(uuid.uuid4()), "expected_version": 0, "judgement": "up"}
    assert (await api.client.put(path, json=first, headers=api.headers)).status_code == 200
    second = {"client_request_id": str(uuid.uuid4()), "expected_version": 1, "judgement": "down"}
    assert (await api.client.put(path, json=second, headers=api.headers)).status_code == 200
    replay = await api.client.put(path, json=first, headers=api.headers)
    assert replay.json()["judgement"] == "down" and replay.json()["version"] == 2
    page = f"/api/v1/chat/conversations/{conversation_id}/feedback"
    assert (await api.client.get(page + "?limit=101", headers=api.headers)).status_code == 422
    assert (await api.client.get(page + "?cursor=invalid", headers=api.headers)).status_code == 422
    assert (
        await api.client.get(
            page.replace(str(conversation_id), str(uuid.uuid4())), headers=api.headers
        )
    ).status_code == 404
    assert (await api.client.get(page, headers={})).status_code == 401
