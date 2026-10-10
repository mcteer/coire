"""Selection excludes withdrawn copies and applies filters to one effective label."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_chat_comparisons import source
from test_training_datasets import API
from test_training_datasets import api as api
from test_training_datasets import training_postgres_url as training_postgres_url

from coire_api.db import ComparisonPairRow, FeedbackRow
from coire_api.feedback import comparisons
from coire_api.feedback.export_selection import materialize_source, select_export_sources
from coire_core.models.feedback import PreferenceExportCreate


@pytest.mark.parametrize("filter_kind", ["tag", "date"])
async def test_source_selection_never_exports_thumb_or_admin_filter_fallback(
    api: API,
    monkeypatch: pytest.MonkeyPatch,
    filter_kind: str,
) -> None:
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    cid, mid = await source(api)
    created = await api.client.post(
        f"/api/v1/chat/conversations/{cid}/comparisons",
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_revision": 2,
            "source_message_id": str(mid),
        },
    )
    assert created.status_code == 202
    async with api.sessions() as session:
        pair = await session.get(ComparisonPairRow, created.json()["id"])
        assert pair is not None
        pair.generation_state = "ready"
        pair.candidate = "candidate answer"
        session.add_all(
            [
                FeedbackRow(
                    owner_user_id=api.owner,
                    actor_user_id=api.owner,
                    conversation_id=cid,
                    pair_id=pair.id,
                    kind="pair",
                    source=side,
                    judgement=choice,
                    tags=tags,
                    capture_generation=1,
                    version=1,
                    updated_at=datetime.now(UTC)
                    - (timedelta(days=1) if side == "owner" else timedelta()),
                )
                for side, choice, tags in [
                    ("owner", "original", []),
                    ("admin", "candidate", ["clear"]),
                ]
            ]
        )
        await session.commit()
    request = PreferenceExportCreate.model_validate(
        {
            "name": "pairs",
            "license_note": "local",
            "model_id": api.metadata["analysis_model_id"],
            "variant_id": api.metadata["analysis_variant_id"],
            "filters": {"tag": "clear"}
            if filter_kind == "tag"
            else {"from": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()},
        }
    )
    async with api.sessions() as session:
        sources, count = await select_export_sources(session, request)
        assert sources == [] and count == 0
        sources, count = await select_export_sources(
            session, request.model_copy(update={"source": "admin"})
        )
        assert count == 1 and sources[0].judgement == "candidate"
        materialized = await materialize_source(session, sources[0])
        assert materialized.row.chosen == "candidate answer"
        assert materialized.row.rejected == "original answer"
        assert materialized.content_sha256 == materialized.row.content_sha256()
        pair = await session.scalar(select(ComparisonPairRow))
        assert pair is not None
        pair.withdrawn_at = datetime.now(UTC)
        await session.commit()
        sources, count = await select_export_sources(
            session, request.model_copy(update={"source": "admin"})
        )
        assert sources == [] and count == 0
