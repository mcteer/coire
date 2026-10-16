"""Resolution qualification reuses pinned copies and preserves usable capability."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import DownloadJobRow, ModelCopyRow, ModelRow, NodeRow
from coire_api.images import resolution_qualification as qualification
from coire_api.registry.reconciler import RegistryReconciler
from coire_core.errors import ImageConflict
from coire_core.models.images import ImageResolutionQualificationRequest
from coire_core.models.jobs import DownloadStage
from coire_core.models.registry import ModelKind, ModelState

PROFILE = {
    "modes": ["txt2img", "img2img"],
    "min_width": 512,
    "max_width": 512,
    "min_height": 512,
    "max_height": 512,
    "max_pixels": 512**2,
    "min_steps": 4,
    "max_steps": 4,
    "min_guidance": "0",
    "max_guidance": "0",
    "max_outputs": 1,
}


class Rows:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def all(self) -> list[object]:
        return self.rows


class Session:
    def __init__(self) -> None:
        self.model = ModelRow(
            id=uuid.uuid4(),
            kind=ModelKind.IMAGE_MODEL,
            state=ModelState.READY,
            image_capability_profile=PROFILE.copy(),
            memory_estimate_bytes=32 * 1024**3,
            manifest_sha256="a" * 64,
            source_revision="b" * 40,
        )
        self.nodes = [
            NodeRow(id=uuid.uuid4(), name="coire-edge-a"),
            NodeRow(id=uuid.uuid4(), name="coire-edge-b"),
        ]
        self.copies = [
            ModelCopyRow(
                node_id=n.id, model_id=self.model.id, verified=True, manifest_sha256="a" * 64
            )
            for n in self.nodes
        ]
        self.pending: uuid.UUID | None = None
        self.added: list[object] = []
        self.count = 0

    async def scalar(self, query: object) -> Any:
        self.count += 1
        return self.model if self.count == 1 else self.pending

    async def scalars(self, query: object) -> Rows:
        return Rows(cast(list[object], self.copies if "model_copies" in str(query) else self.nodes))

    def add(self, obj: object) -> None:
        self.added.append(obj)


async def test_intake_uses_validation_cursor_only_and_preserves_ready_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    audit = AsyncMock()
    monkeypatch.setattr(qualification, "write_principal_audit", audit)
    request = ImageResolutionQualificationRequest()
    receipt = await qualification.submit_qualification(
        cast(Any, session),
        session.model.id,
        request,
        principal=Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
        views=cast(
            Any,
            [
                SimpleNamespace(name=n.name, healthy=True, memory_free_bytes=256 * 1024**3)
                for n in session.nodes
            ],
        ),
    )
    job = cast(DownloadJobRow, session.added[0])
    assert job.id == receipt.job_id and job.stage is DownloadStage.VERIFY_REPLICA
    assert (
        session.model.state is ModelState.READY
        and session.model.image_capability_profile == PROFILE
    )
    assert job.image_validation and job.image_validation["node_names"] == [
        n.name for n in session.nodes
    ]
    assert job.transfer_grant is None
    audit.assert_awaited_once()


async def test_duplicate_qualification_is_rejected_before_any_node_work() -> None:
    session = Session()
    session.pending = uuid.uuid4()
    with pytest.raises(ImageConflict, match="already running"):
        await qualification.submit_qualification(
            cast(Any, session),
            session.model.id,
            ImageResolutionQualificationRequest(),
            principal=Principal(),
            views=[],
        )
    assert not session.added


async def test_failed_qualification_preserves_ready_model_and_cleanup_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    job = DownloadJobRow(
        id=uuid.uuid4(),
        model_id=session.model.id,
        stage=DownloadStage.VERIFY_REPLICA,
        image_validation={
            "resolution_qualification": True,
            "previous_profile": PROFILE,
            "previous_memory": 32 * 1024**3,
            "reservation_cleanup_pending": True,
            "node_names": ["coire-edge-a", "coire-edge-b"],
        },
    )
    monkeypatch.setattr("coire_api.registry.reconciler.write_audit", AsyncMock())
    reconciler = RegistryReconciler(cast(Any, SimpleNamespace(registry_reconcile_interval_s=30)))
    await reconciler._fail(cast(Any, session), job, session.model, "invalid resolution pixels")
    assert job.stage is DownloadStage.FAILED
    assert (
        session.model.state is ModelState.READY
        and session.model.image_capability_profile == PROFILE
    )
    client = SimpleNamespace(release_reservation=AsyncMock())
    await reconciler._cleanup_resolution_holds(cast(Any, client), job)
    assert (
        client.release_reservation.await_count == 2
        and job.image_validation
        and not job.image_validation["reservation_cleanup_pending"]
    )
    await reconciler._cleanup_resolution_holds(cast(Any, client), job)
    assert client.release_reservation.await_count == 2


async def test_terminal_cleanup_retries_after_node_connection_loss() -> None:
    from coire_api.nodes_client import NodeError, NodeErrorKind

    job = DownloadJobRow(
        id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        stage=DownloadStage.FAILED,
        image_validation={
            "resolution_qualification": True,
            "reservation_cleanup_pending": True,
            "node_names": ["coire-edge-a", "coire-edge-b"],
        },
    )
    reconciler = RegistryReconciler(cast(Any, SimpleNamespace(registry_reconcile_interval_s=30)))
    release = AsyncMock(side_effect=NodeError(NodeErrorKind.UNREACHABLE, "coire-edge-a"))
    client = SimpleNamespace(release_reservation=release)
    await reconciler._cleanup_resolution_holds(cast(Any, client), job)
    assert dict(job.image_validation or {})["reservation_cleanup_pending"] is True
    release.side_effect = None
    await reconciler._cleanup_resolution_holds(cast(Any, client), job)
    assert dict(job.image_validation or {})["reservation_cleanup_pending"] is False
    assert release.await_count == 3


async def test_completed_qwen_copy_releases_only_its_hold_and_does_not_repeat_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from coire_core.models.jobs import JobKind, JobStage, JobStatus

    session = Session()
    model = session.model
    model.repo_id = "mflux-community/qwen-image-edit-2509-mflux-q8"
    model.slug = "mflux-community--qwen-image-edit-2509-mflux-q8"
    job = DownloadJobRow(
        id=uuid.uuid4(),
        model_id=model.id,
        stage=DownloadStage.VERIFY_REPLICA,
        image_validation={
            "origin": {
                "validated": True,
                "kind": "image_model",
                "manifest_sha256": "a" * 64,
                "source_revision": "b" * 40,
                "validator_version": "qwen-edit-v1",
                "peak_rss_bytes": 1000,
                "peak_physical_bytes": 2000,
                "peak_physical_delta_bytes": 1900,
                "thumbnail_sha256": "c" * 64,
                "image_capability_profile": {
                    **PROFILE,
                    "modes": ["edit"],
                    "min_guidance": "4",
                    "max_guidance": "4",
                },
            }
        },
    )
    origin, replica = session.nodes
    replica_id = uuid.uuid5(job.id, "image-validation:replica")
    now = datetime.now(UTC)
    status = JobStatus(
        job_id=replica_id,
        kind=JobKind.IMAGE_VALIDATE,
        slug=model.slug,
        stage=JobStage.RESOLVING,
        started_at=now,
        updated_at=now,
    )
    client = SimpleNamespace(
        release_reservation=AsyncMock(),
        hold_reservation=AsyncMock(),
        start_image_validate=AsyncMock(return_value=status),
        get_job=AsyncMock(return_value=status),
    )
    audit = AsyncMock()
    monkeypatch.setattr("coire_api.registry.reconciler.write_audit", audit)
    reconciler = RegistryReconciler(cast(Any, SimpleNamespace(registry_reconcile_interval_s=30)))
    assert not await reconciler._validate_image_copies(
        cast(Any, session), cast(Any, client), job, model, origin, replica
    )
    client.release_reservation.assert_awaited_once_with(
        origin.name, uuid.uuid5(job.id, "image-validation:origin")
    )
    assert job.image_validation and job.image_validation["origin_reservation_released"] is True
    client.start_image_validate.assert_not_awaited()
    assert (
        audit.await_args
        and audit.await_args.kwargs["action"] == "image.photo_edit.copy_reservation_released"
    )
    assert not await reconciler._validate_image_copies(
        cast(Any, session), cast(Any, client), job, model, origin, replica
    )
    client.release_reservation.assert_awaited_once()
    assert (
        client.start_image_validate.await_args
        and client.start_image_validate.await_args.args[0] == replica.name
    )
    assert audit.await_count == 1
