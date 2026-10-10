"""Private publication freezes membership independently of later withdrawal."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_chat_comparisons import source
from test_training_datasets import API
from test_training_datasets import api as api
from test_training_datasets import training_postgres_url as training_postgres_url

from coire_api.db import ComparisonPairRow, FeedbackRow, PreferenceExportMemberRow
from coire_api.feedback import comparisons, export_execution
from coire_api.feedback.export_execution import execute_export
from coire_api.feedback.export_selection import ExportSource
from coire_api.training.storage import DatasetStore, StagedDataset
from coire_core.settings import Settings


async def explicit_pair(api: API, prompt: str) -> None:
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
        # Inert database fixture represents an independently captured prompt.
        pair.prompt = [{"role": "user", "content": prompt}]
        session.add(
            FeedbackRow(
                owner_user_id=api.owner,
                actor_user_id=api.owner,
                conversation_id=cid,
                pair_id=pair.id,
                kind="pair",
                source="owner",
                judgement="candidate",
                tags=[],
                capture_generation=1,
                version=1,
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()


@pytest.mark.parametrize("groups", [1, 2])
async def test_export_requires_two_groups_and_preserves_published_snapshot(
    api: API,
    monkeypatch: pytest.MonkeyPatch,
    groups: int,
) -> None:
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    for index in range(groups):
        await explicit_pair(api, f"prompt {index}")
    api.settings.preference_training_enabled = True
    response = await api.client.post(
        "/api/v1/admin/feedback/exports",
        headers={**api.headers, "idempotency-key": "publish"},
        json={
            "name": "pairs",
            "license_note": "local",
            "model_id": api.metadata["analysis_model_id"],
            "variant_id": api.metadata["analysis_variant_id"],
        },
    )
    assert response.status_code == 202
    identity = response.json()["id"]
    state = await execute_export(identity, api.settings)
    detail = (
        await api.client.get("/api/v1/admin/feedback/exports/" + identity, headers=api.headers)
    ).json()
    if groups == 1:
        assert state == "failed" and detail["reason"] == "insufficient_groups"
        assert not detail["cleanup_pending"] and detail["dataset_id"] is None
        return
    assert state == "succeeded" and detail["warnings"] == ["small_sample"]
    dataset_id = uuid.UUID(detail["dataset_id"])
    path = DatasetStore(api.settings).source_path(dataset_id)
    before = path.read_bytes()
    async with api.sessions() as session:
        members = list(await session.scalars(select(PreferenceExportMemberRow)))
        assert len(members) == 2 and all(member.published for member in members)
    withdrawn = await api.client.patch(
        "/api/v1/chat/feedback-preference",
        headers=api.headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "expected_version": 1,
            "enabled": False,
            "disclosure_version": "feedback-v1",
        },
    )
    assert withdrawn.status_code == 200
    assert path.read_bytes() == before
    # Registration is publication even while Studio analysis remains queued.
    from coire_api.db import TrainingDatasetAnalysisRow, TrainingDatasetRevisionRow
    from coire_api.feedback.retention import purge_sources

    async with api.sessions() as session:
        dataset = await session.get(TrainingDatasetRevisionRow, dataset_id)
        assert dataset is not None and dataset.state == "analyzing"
        analysis = await session.scalar(
            select(TrainingDatasetAnalysisRow).where(
                TrainingDatasetAnalysisRow.dataset_id == dataset_id
            )
        )
        assert analysis is not None and analysis.state == "queued"
        await purge_sources(session)
        await session.commit()
        members = list(await session.scalars(select(PreferenceExportMemberRow)))
        assert len(members) == 2 and all(member.published for member in members)
        copies = list(await session.scalars(select(ComparisonPairRow)))
        assert all(
            pair.prompt is None and pair.original is None and pair.candidate is None
            for pair in copies
        )
        await session.refresh(dataset)
        await session.refresh(analysis)
        assert dataset.state == "analyzing" and analysis.state == "queued"
    assert path.read_bytes() == before


async def test_withdrawal_after_staging_prevents_publication(
    api: API,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    await explicit_pair(api, "one")
    await explicit_pair(api, "two")
    api.settings.preference_training_enabled = True
    response = await api.client.post(
        "/api/v1/admin/feedback/exports",
        headers={**api.headers, "idempotency-key": "withdraw-race"},
        json={
            "name": "pairs",
            "license_note": "local",
            "model_id": api.metadata["analysis_model_id"],
            "variant_id": api.metadata["analysis_variant_id"],
        },
    )
    original = export_execution.stage_export

    async def stage(
        identity: str, settings: Settings, store: DatasetStore
    ) -> tuple[list[ExportSource], list[PreferenceExportMemberRow], StagedDataset, int]:
        result = await original(identity, settings, store)
        withdrawn = await api.client.patch(
            "/api/v1/chat/feedback-preference",
            headers=api.headers,
            json={
                "client_request_id": str(uuid.uuid4()),
                "expected_version": 1,
                "enabled": False,
                "disclosure_version": "feedback-v1",
            },
        )
        assert withdrawn.status_code == 200
        return result

    monkeypatch.setattr(export_execution, "stage_export", stage)
    identity = response.json()["id"]
    assert await execute_export(identity, api.settings) == "failed"
    detail = (
        await api.client.get("/api/v1/admin/feedback/exports/" + identity, headers=api.headers)
    ).json()
    assert detail["dataset_id"] is None and not detail["cleanup_pending"]
    async with api.sessions() as session:
        assert list(await session.scalars(select(PreferenceExportMemberRow))) == []


async def test_lost_publication_ack_preserves_registered_source(
    api: API,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    await explicit_pair(api, "one")
    await explicit_pair(api, "two")
    api.settings.preference_training_enabled = True
    response = await api.client.post(
        "/api/v1/admin/feedback/exports",
        headers={**api.headers, "idempotency-key": "lost-ack"},
        json={
            "name": "pairs",
            "license_note": "local",
            "model_id": api.metadata["analysis_model_id"],
            "variant_id": api.metadata["analysis_variant_id"],
        },
    )
    original = export_execution.publish_export

    async def publish(
        identity: str,
        sources: list[ExportSource],
        members: list[PreferenceExportMemberRow],
        result: StagedDataset,
        fence: int,
        store: DatasetStore,
    ) -> str:
        await original(identity, sources, members, result, fence, store)
        raise ConnectionError("publication acknowledgement lost")

    monkeypatch.setattr(export_execution, "publish_export", publish)
    identity = response.json()["id"]
    assert await execute_export(identity, api.settings) == "succeeded"
    detail = (
        await api.client.get("/api/v1/admin/feedback/exports/" + identity, headers=api.headers)
    ).json()
    assert DatasetStore(api.settings).source_path(uuid.UUID(detail["dataset_id"])).is_file()
    assert not detail["cleanup_pending"]


async def test_accepted_export_finishes_when_new_admission_is_disabled(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    await explicit_pair(api, "one")
    await explicit_pair(api, "two")
    api.settings.preference_training_enabled = True
    response = await api.client.post(
        "/api/v1/admin/feedback/exports",
        headers={**api.headers, "idempotency-key": "accepted-disabled"},
        json={
            "name": "pairs",
            "license_note": "local",
            "model_id": api.metadata["analysis_model_id"],
            "variant_id": api.metadata["analysis_variant_id"],
        },
    )
    assert response.status_code == 202
    api.settings.preference_training_enabled = False
    api.settings.training_enabled = False
    assert await execute_export(response.json()["id"], api.settings) == "succeeded"


async def submit_export(api: API, key: str) -> str:
    api.settings.preference_training_enabled = True
    response = await api.client.post(
        "/api/v1/admin/feedback/exports",
        headers={**api.headers, "idempotency-key": key},
        json={
            "name": "pairs",
            "license_note": "local",
            "model_id": api.metadata["analysis_model_id"],
            "variant_id": api.metadata["analysis_variant_id"],
        },
    )
    assert response.status_code == 202
    return str(response.json()["id"])


async def test_filesystem_commit_without_database_commit_cleans_orphan_and_hold(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_api.db import TrainingDatasetRevisionRow, TrainingStorageReservationRow

    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    await explicit_pair(api, "one")
    await explicit_pair(api, "two")
    identity = await submit_export(api, "fs-before-db")
    original = DatasetStore.commit
    copied: list[uuid.UUID] = []

    async def commit(
        store: DatasetStore, hold: uuid.UUID, dataset: uuid.UUID, result: StagedDataset
    ) -> int:
        await original(store, hold, dataset, result)
        copied.append(dataset)
        raise ConnectionError("Synthetic failure before database commit")

    monkeypatch.setattr(DatasetStore, "commit", commit)
    assert await execute_export(identity, api.settings) == "failed"
    assert len(copied) == 1 and not DatasetStore(api.settings).source_path(copied[0]).exists()
    async with api.sessions() as session:
        assert await session.get(TrainingDatasetRevisionRow, copied[0]) is None
        hold = await session.scalar(
            select(TrainingStorageReservationRow).where(
                TrainingStorageReservationRow.subject_id == str(copied[0])
            )
        )
        assert hold is not None and hold.state == "released"


async def test_cancel_after_staging_cleans_without_publishing(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_api.db import PreferenceExportRow, TrainingStorageReservationRow

    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    await explicit_pair(api, "one")
    await explicit_pair(api, "two")
    identity = await submit_export(api, "cancel-stage")
    assert await export_execution.prepare_export(identity, api.settings) == "staging"
    store = DatasetStore(api.settings)
    await export_execution.stage_export(identity, api.settings, store)
    detail = (
        await api.client.get("/api/v1/admin/feedback/exports/" + identity, headers=api.headers)
    ).json()
    cancelled = await api.client.post(
        "/api/v1/admin/feedback/exports/" + identity + "/cancel",
        headers={**api.headers, "idempotency-key": "cancel-stage-command"},
        json={"expected_version": detail["version"]},
    )
    assert cancelled.status_code == 200
    pending = await api.client.get(
        "/api/v1/admin/feedback/exports/" + identity, headers=api.headers
    )
    assert pending.json()["cleanup_pending"]
    api.settings.preference_training_enabled = False
    api.settings.training_enabled = False
    assert await execute_export(identity, api.settings) == "cancelled"
    async with api.sessions() as session:
        export = await session.get(PreferenceExportRow, identity)
        assert export is not None and not export.cleanup_pending and not export.staging
        assert export.dataset_id is None
        holds = list(await session.scalars(select(TrainingStorageReservationRow)))
        assert holds and all(hold.state == "released" for hold in holds)


async def test_recovery_keeps_stage_counted_when_publication_read_is_uncertain(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession

    from coire_api.db import PreferenceExportRow, TrainingStorageReservationRow

    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    await explicit_pair(api, "one")
    await explicit_pair(api, "two")
    identity = await submit_export(api, "uncertain-read")
    assert await export_execution.prepare_export(identity, api.settings) == "staging"
    store = DatasetStore(api.settings)
    await export_execution.stage_export(identity, api.settings, store)
    discard = AsyncMock()
    purge = AsyncMock()
    monkeypatch.setattr(store, "discard", discard)
    monkeypatch.setattr(store, "purge_source", purge)

    @asynccontextmanager
    async def unavailable(fail: bool = True) -> AsyncIterator[AsyncSession]:
        async with api.sessions() as session:
            if fail:
                raise ConnectionError("Synthetic database unavailable")
            yield session

    with monkeypatch.context() as patch:
        patch.setattr(export_execution, "session_scope", unavailable)
        with pytest.raises(ConnectionError):
            await export_execution.cleanup_export(identity, store)
    discard.assert_not_called()
    purge.assert_not_called()
    async with api.sessions() as session:
        export = await session.get(PreferenceExportRow, identity)
        assert export is not None and export.cleanup_pending
        hold = await session.get(
            TrainingStorageReservationRow, uuid.UUID(str(export.staging["hold_id"]))
        )
        assert hold is not None and hold.state == "held"


async def test_only_one_export_stages_and_queued_deadline_is_fixed(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from datetime import timedelta

    from coire_api.db import PreferenceExportRow

    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    await explicit_pair(api, "one")
    await explicit_pair(api, "two")
    first = await submit_export(api, "active-a")
    second = await submit_export(api, "active-b")
    states = await asyncio.gather(
        export_execution.prepare_export(first, api.settings),
        export_execution.prepare_export(second, api.settings),
    )
    assert sorted(states) == ["queued", "staging"]
    queued = first if states[0] == "queued" else second
    async with api.sessions() as session:
        row = await session.get(PreferenceExportRow, queued)
        assert row is not None
        row.queue_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await export_execution.prepare_export(queued, api.settings) == "failed"
    detail = (
        await api.client.get("/api/v1/admin/feedback/exports/" + queued, headers=api.headers)
    ).json()
    assert detail["reason"] == "timeout" and not detail["cleanup_pending"]


async def test_source_changes_rebuild_at_most_three_times(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    await explicit_pair(api, "one")
    await explicit_pair(api, "two")
    identity = await submit_export(api, "rebuild-bound")
    original = export_execution.stage_export
    attempts = 0

    async def stage(
        identity: str, settings: Settings, store: DatasetStore
    ) -> tuple[list[ExportSource], list[PreferenceExportMemberRow], StagedDataset, int]:
        nonlocal attempts
        result = await original(identity, settings, store)
        attempts += 1
        async with api.sessions() as session:
            label = await session.scalar(select(FeedbackRow).order_by(FeedbackRow.id).limit(1))
            assert label is not None
            label.version += 1
            label.judgement = "original" if label.judgement == "candidate" else "candidate"
            await session.commit()
        return result

    monkeypatch.setattr(export_execution, "stage_export", stage)
    assert await execute_export(identity, api.settings) == "failed"
    assert attempts == 3
    detail = (
        await api.client.get("/api/v1/admin/feedback/exports/" + identity, headers=api.headers)
    ).json()
    assert detail["reason"] == "source_changed" and not detail["cleanup_pending"]


async def test_twenty_explicit_pairs_publish_once_with_frozen_oriented_rows(
    api: API, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from coire_api.db import TrainingDatasetRevisionRow
    from coire_core.models.preference import PreferenceRow, PreferenceSplitManifest

    monkeypatch.setattr(comparisons, "start_comparison", AsyncMock())
    for index in range(20):
        await explicit_pair(api, f"synthetic prompt {index}")
    identity = await submit_export(api, "twenty-pairs")
    assert await execute_export(identity, api.settings) == "succeeded"
    detail = (
        await api.client.get("/api/v1/admin/feedback/exports/" + identity, headers=api.headers)
    ).json()
    assert detail["selected_count"] == 20 and detail["warnings"] == []
    dataset_id = uuid.UUID(detail["dataset_id"])
    before = DatasetStore(api.settings).source_path(dataset_id).read_bytes()
    rows = [PreferenceRow.model_validate(json.loads(line)) for line in before.splitlines()]
    assert len(rows) == 20 and len({row.prompt_sha256() for row in rows}) == 20
    assert all(row.chosen == "candidate answer" for row in rows)
    assert await execute_export(identity, api.settings) == "succeeded"
    assert DatasetStore(api.settings).source_path(dataset_id).read_bytes() == before
    async with api.sessions() as session:
        datasets = list(await session.scalars(select(TrainingDatasetRevisionRow)))
        assert len(datasets) == 1 and datasets[0].row_count == 20
        split = PreferenceSplitManifest.model_validate(datasets[0].split_manifest)
        assert len(split.train_rows) + len(split.validation_rows) == 20
        members = list(await session.scalars(select(PreferenceExportMemberRow)))
        assert len(members) == 20 and len({member.pair_id for member in members}) == 20
