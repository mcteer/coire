"""Disposable Postgres: 10k contributions, ten readers and bounded publication."""

import asyncio
import json
import math
import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import Request
from sqlalchemy import insert
from test_feedback_review import ready_pair
from test_training_datasets import API
from test_training_datasets import api as api
from test_training_datasets import training_postgres_url as training_postgres_url

from coire_api.db import ComparisonPairRow, FeedbackRow
from coire_api.feedback.export_execution import execute_export
from coire_api.training.service import training_id

pytestmark = pytest.mark.integration


def p95(samples: list[float]) -> float:
    return sorted(samples)[math.ceil(len(samples) * 0.95) - 1]


async def test_ten_thousand_pairs_with_ten_readers_and_local_publication(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, cid = await ready_pair(api, monkeypatch)
    now = datetime.now(UTC)
    async with api.sessions.begin() as session:
        source = await session.get(ComparisonPairRow, identity)
        assert source is not None
        message_id = source.source_message_id
        source.selection_state = "chosen"
        source.selected_candidate = "original"
        await session.flush()
        prototype = {
            column.name: getattr(source, column.name) for column in source.__table__.columns
        }
        pairs, labels = [], []
        for index in range(10_000):
            pair_id = identity if index == 0 else training_id()
            if index:
                pairs.append(
                    {
                        **prototype,
                        "id": pair_id,
                        "client_request_id": uuid.uuid4(),
                        "prompt": [{"role": "user", "content": f"private fixture {index}"}],
                    }
                )
            labels.append(
                {
                    "id": uuid.uuid4(),
                    "owner_user_id": api.owner,
                    "actor_user_id": api.owner,
                    "conversation_id": cid,
                    "pair_id": pair_id,
                    "kind": "pair",
                    "source": "owner",
                    "judgement": "original",
                    "tags": [],
                    "capture_generation": source.capture_generation,
                    "version": 1,
                    "updated_at": now,
                }
            )
        await session.execute(insert(ComparisonPairRow), pairs)
        await session.execute(insert(FeedbackRow), labels)

    reads: list[float] = []
    mutations: list[float] = []
    setting_mutations: list[float] = []
    from coire_api import auth
    from coire_api.auth import Principal, PrincipalKind
    from coire_api.db import UserRow
    from coire_core.models.auth import UserRole

    settings_owner = uuid.uuid4()
    async with api.sessions.begin() as session:
        session.add(
            UserRow(
                id=settings_owner,
                email=f"{settings_owner}@performance.test",
                display_name="Settings fixture",
                role=UserRole.USER,
                active=True,
            )
        )
    original_authenticate = auth.authenticate_request

    async def authenticate(request: Request) -> Principal:
        if request.headers.get("authorization") == "Bearer settings-fixture":
            return Principal(kind=PrincipalKind.USER, user_id=settings_owner, role=UserRole.USER)
        return await original_authenticate(request)

    monkeypatch.setattr(auth, "authenticate_request", authenticate)
    stop = asyncio.Event()

    async def reader() -> None:
        while not stop.is_set():
            started = time.perf_counter()
            response = await api.client.get(
                "/api/v1/admin/feedback/comparisons", headers=api.headers
            )
            assert response.status_code == 200, response.text
            assert len(response.json()["items"]) == 25 and response.json()["next_cursor"]
            reads.append(time.perf_counter() - started)

    readers = [asyncio.create_task(reader()) for _ in range(10)]
    try:
        for version in range(20):
            started = time.perf_counter()
            response = await api.client.put(
                f"/api/v1/chat/conversations/{cid}/messages/{message_id}/feedback",
                headers=api.headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "expected_version": version,
                    "judgement": "up" if version % 2 == 0 else "down",
                    "tags": [],
                },
            )
            assert response.status_code == 200, response.text
            mutations.append(time.perf_counter() - started)
        headers = {**api.headers, "authorization": "Bearer settings-fixture"}
        for _ in range(20):
            state = (
                await api.client.get("/api/v1/chat/feedback-preference", headers=headers)
            ).json()
            started = time.perf_counter()
            response = await api.client.patch(
                "/api/v1/chat/feedback-preference",
                headers=headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "expected_version": state["version"],
                    "enabled": True,
                    "disclosure_version": "feedback-v1",
                },
            )
            assert response.status_code == 200, response.text
            setting_mutations.append(time.perf_counter() - started)
    finally:
        stop.set()
        await asyncio.gather(*readers)

    api.settings.preference_training_enabled = True
    response = await api.client.post(
        "/api/v1/admin/feedback/exports",
        headers={**api.headers, "idempotency-key": "performance-export"},
        json={
            "name": "private-performance",
            "license_note": "synthetic local fixture",
            "model_id": api.metadata["analysis_model_id"],
            "variant_id": api.metadata["analysis_variant_id"],
        },
    )
    assert response.status_code == 202, response.text
    export_id = response.json()["id"]
    started = time.perf_counter()
    terminal = await asyncio.wait_for(execute_export(export_id, api.settings), timeout=300)
    duration = time.perf_counter() - started
    assert terminal in {"succeeded", "failed"}
    detail = (
        await api.client.get(f"/api/v1/admin/feedback/exports/{export_id}", headers=api.headers)
    ).json()
    if terminal == "succeeded":
        assert detail["matched_count"] == 10_000 and detail["selected_count"] == 10_000
    print(
        json.dumps(
            {
                "pairs": 10_000,
                "readers": 10,
                "review_samples": len(reads),
                "review_p95_seconds": p95(reads),
                "mutation_p95_seconds": p95(mutations),
                "settings_p95_seconds": p95(setting_mutations),
                "export_seconds": duration,
                "export_state": terminal,
            }
        )
    )
    assert p95(reads) <= 1.0
    assert p95(mutations) <= 0.5
    assert p95(setting_mutations) <= 0.5
