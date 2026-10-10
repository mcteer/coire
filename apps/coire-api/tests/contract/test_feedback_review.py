"""Review is a separate versioned label; owner withdrawal remains authoritative."""

import uuid
from unittest.mock import AsyncMock

import pytest
from test_chat_comparisons import source
from test_training_datasets import API
from test_training_datasets import api as api
from test_training_datasets import training_postgres_url as training_postgres_url

from coire_api.db import ChatConversationRow, ComparisonPairRow
from coire_api.feedback import comparisons


async def ready_pair(api: API, monkeypatch: pytest.MonkeyPatch) -> tuple[str, uuid.UUID]:
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    cid, mid = await source(api)
    response = await api.client.post(
        f"/api/v1/chat/conversations/{cid}/comparisons",
        headers={**api.headers, "idempotency-key": str(uuid.uuid4())},
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
        await session.commit()
    return pair_id, cid


async def test_admin_label_conflict_skip_revisit_and_no_chat_context_mutation(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_id, cid = await ready_pair(api, monkeypatch)
    root = "/api/v1/admin/feedback/comparisons"
    queue = await api.client.get(
        root, headers={**api.headers, "idempotency-key": str(uuid.uuid4())}
    )
    assert queue.status_code == 200, queue.text
    assert [item["id"] for item in queue.json()["items"]] == [pair_id]
    original = queue.json()["items"][0]
    assert original["prompt"] and original["original"] == "original answer"
    skipped = await api.client.put(
        f"{root}/{pair_id}/judgement",
        headers={**api.headers, "idempotency-key": str(uuid.uuid4())},
        json={"expected_version": 0, "choice": "skip"},
    )
    assert skipped.status_code == 200 and skipped.json()["skipped"]
    assert (
        await api.client.get(root, headers={**api.headers, "idempotency-key": str(uuid.uuid4())})
    ).json()["items"] == []
    assert (
        len(
            (
                await api.client.get(
                    root + "?state=skipped",
                    headers={**api.headers, "idempotency-key": str(uuid.uuid4())},
                )
            ).json()["items"]
        )
        == 1
    )
    async with api.sessions() as session:
        convo = await session.get(ChatConversationRow, cid)
        assert convo is not None
        revision = convo.context_revision
    judged = await api.client.put(
        f"{root}/{pair_id}/judgement",
        headers={**api.headers, "idempotency-key": str(uuid.uuid4())},
        json={"expected_version": 0, "choice": "candidate", "tags": ["helpful"]},
    )
    assert judged.status_code == 200, judged.text
    assert judged.json()["judgement"]["version"] == 1
    assert (
        await api.client.put(
            f"{root}/{pair_id}/judgement",
            headers={**api.headers, "idempotency-key": str(uuid.uuid4())},
            json={"expected_version": 0, "choice": "original"},
        )
    ).status_code == 409
    assert (
        len(
            (
                await api.client.get(
                    root + "?state=reviewed",
                    headers={**api.headers, "idempotency-key": str(uuid.uuid4())},
                )
            ).json()["items"]
        )
        == 1
    )
    async with api.sessions() as session:
        convo = await session.get(ChatConversationRow, cid)
        pair = await session.get(ComparisonPairRow, pair_id)
        assert convo is not None and convo.context_revision == revision
        assert (
            pair is not None
            and pair.selection_state == "pending"
            and pair.selected_candidate is None
        )


async def test_withdrawn_pair_is_absent_and_cannot_be_reviewed(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_id, _ = await ready_pair(api, monkeypatch)
    preference = (
        await api.client.get(
            "/api/v1/chat/feedback-preference",
            headers={**api.headers, "idempotency-key": str(uuid.uuid4())},
        )
    ).json()
    response = await api.client.patch(
        "/api/v1/chat/feedback-preference",
        headers={**api.headers, "idempotency-key": str(uuid.uuid4())},
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_version": preference["version"],
            "enabled": False,
            "disclosure_version": "feedback-v1",
        },
    )
    assert response.status_code == 200
    root = "/api/v1/admin/feedback/comparisons"
    assert (
        await api.client.get(root, headers={**api.headers, "idempotency-key": str(uuid.uuid4())})
    ).json()["items"] == []
    detail = await api.client.get(
        f"{root}/{pair_id}", headers={**api.headers, "idempotency-key": str(uuid.uuid4())}
    )
    assert detail.status_code in {403, 404} and "original answer" not in detail.text
    judgement = await api.client.put(
        f"{root}/{pair_id}/judgement",
        headers={**api.headers, "idempotency-key": str(uuid.uuid4())},
        json={"expected_version": 0, "choice": "candidate"},
    )
    assert judgement.status_code in {403, 404}


@pytest.mark.parametrize("token,status", [(None, 401), ("user", 403), ("node", 403), ("ops", 403)])
async def test_review_requires_a_live_human_admin(api: API, token: str | None, status: int) -> None:
    headers = {**api.headers}
    if token is None:
        headers.pop("authorization", None)
    else:
        headers["authorization"] = f"Bearer {token}"
    response = await api.client.get("/api/v1/admin/feedback/comparisons", headers=headers)
    assert response.status_code == status


async def test_two_admins_conflict_and_opposing_owner_labels_keep_source_priority(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from coire_api.auth import Principal, PrincipalKind
    from coire_api.db import UserRow
    from coire_api.feedback.export_selection import materialize_source, select_export_sources
    from coire_api.feedback.review import judge_pair
    from coire_core.errors import FeedbackConflict
    from coire_core.models.auth import UserRole
    from coire_core.models.feedback import AdminPairJudgement, PreferenceExportCreate

    pair_id, cid = await ready_pair(api, monkeypatch)
    second = uuid.uuid4()
    async with api.sessions.begin() as session:
        session.add(
            UserRow(
                id=second,
                email=f"{second}@review.test",
                display_name="Reviewer",
                role=UserRole.ADMIN,
                active=True,
            )
        )

    async def decide(identity: uuid.UUID) -> str:
        try:
            async with api.sessions.begin() as session:
                await judge_pair(
                    session,
                    Principal(kind=PrincipalKind.USER, user_id=identity, role=UserRole.ADMIN),
                    pair_id,
                    AdminPairJudgement(expected_version=0, choice="candidate", tags=["admin-tag"]),
                    str(uuid.uuid4()),
                )
            return "saved"
        except FeedbackConflict:
            return "conflict"

    assert sorted(await asyncio.gather(decide(api.owner), decide(second))) == ["conflict", "saved"]
    async with api.sessions() as session:
        pair = await session.get(ComparisonPairRow, pair_id)
        assert pair is not None
        version = pair.version
    response = await api.client.post(
        f"/api/v1/chat/conversations/{cid}/comparisons/{pair_id}/selection",
        headers={**api.headers, "idempotency-key": str(uuid.uuid4())},
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_version": version,
            "candidate": "original",
            "tags": ["owner-tag"],
        },
    )
    assert response.status_code == 200, response.text
    async with api.sessions() as session:
        for mode, chosen, tag in [
            ("owner_preferred", "original answer", "owner-tag"),
            ("owner", "original answer", "owner-tag"),
            ("admin", "candidate answer", "admin-tag"),
        ]:
            request = PreferenceExportCreate.model_validate(
                {
                    "name": "review-pairs",
                    "license_note": "local",
                    "model_id": api.metadata["analysis_model_id"],
                    "variant_id": api.metadata["analysis_variant_id"],
                    "source": mode,
                    "filters": {"tag": tag},
                }
            )
            sources, count = await select_export_sources(session, request)
            assert count == 1 and len(sources) == 1
            materialized = await materialize_source(session, sources[0])
            assert materialized.row.chosen == chosen
        request = request.model_copy(update={"source": "owner_preferred"})
        assert (await select_export_sources(session, request))[1] == 0


async def test_review_receipt_replay_rechecks_privacy_and_rejects_changed_input(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair_id, _ = await ready_pair(api, monkeypatch)
    path = f"/api/v1/admin/feedback/comparisons/{pair_id}/judgement"
    headers = {**api.headers, "idempotency-key": "same-decision"}
    body = {"expected_version": 0, "choice": "candidate"}
    first = await api.client.put(path, headers=headers, json=body)
    assert first.status_code == 200
    assert (await api.client.put(path, headers=headers, json=body)).json() == first.json()
    assert (
        await api.client.put(path, headers=headers, json={**body, "choice": "original"})
    ).status_code == 409
    preference = (
        await api.client.get("/api/v1/chat/feedback-preference", headers=api.headers)
    ).json()
    assert (
        await api.client.patch(
            "/api/v1/chat/feedback-preference",
            headers=api.headers,
            json={
                "client_request_id": str(uuid.uuid4()),
                "expected_version": preference["version"],
                "enabled": False,
                "disclosure_version": "feedback-v1",
            },
        )
    ).status_code == 200
    replay = await api.client.put(path, headers=headers, json=body)
    assert replay.status_code in {403, 404}


@pytest.mark.parametrize("change", ["inactive", "demoted"])
async def test_review_rechecks_live_admin_authority_before_reads_and_writes(
    api: API, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from coire_api.db import UserRow
    from coire_core.models.auth import UserRole

    pair_id, _ = await ready_pair(api, monkeypatch)
    async with api.sessions.begin() as session:
        owner = await session.get(UserRow, api.owner)
        assert owner is not None
        if change == "inactive":
            owner.active = False
        else:
            owner.role = UserRole.USER
    path = f"/api/v1/admin/feedback/comparisons/{pair_id}"
    detail = await api.client.get(path, headers=api.headers)
    assert detail.status_code == 403 and "original answer" not in detail.text
    assert (
        await api.client.put(
            path + "/judgement",
            headers=api.headers,
            json={"expected_version": 0, "choice": "original"},
        )
    ).status_code == 403
