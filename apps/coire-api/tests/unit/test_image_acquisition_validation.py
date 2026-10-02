"""Two reserved Studio validation results gate image registry readiness."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import DownloadJobRow, ModelCopyRow, ModelRow, NodeRow
from coire_api.registry import service
from coire_api.registry.reconciler import RegistryReconciler
from coire_core.models.image_worker import ImageAssetValidationResult
from coire_core.models.images import ImageCapabilityProfile, ImageMode
from coire_core.models.jobs import JobKind, JobStage, JobStatus
from coire_core.models.registry import ModelKind, ModelState
from coire_core.settings import Settings


def _profile() -> ImageCapabilityProfile:
    return ImageCapabilityProfile(
        modes=(ImageMode.TXT2IMG,),
        min_width=512,
        max_width=512,
        min_height=512,
        max_height=512,
        max_pixels=512 * 512,
        min_steps=4,
        max_steps=4,
        min_guidance=Decimal(0),
        max_guidance=Decimal(0),
        max_outputs=1,
    )


async def test_image_validator_persists_both_results_before_releasing_holds() -> None:
    model = ModelRow(
        id=uuid.uuid4(),
        kind=ModelKind.IMAGE_MODEL,
        slug="org--image",
        repo_id="org/image",
        source_revision="a" * 40,
        manifest_sha256="b" * 64,
        memory_estimate_bytes=10**9,
        state=ModelState.REPLICATING,
    )
    job = DownloadJobRow(id=uuid.uuid4(), model_id=model.id, image_validation=None)
    assert model.manifest_sha256 is not None
    assert model.source_revision is not None
    manifest_sha256: str = model.manifest_sha256
    source_revision: str = model.source_revision
    origin = NodeRow(id=uuid.uuid4(), name="coire-edge-a")
    replica = NodeRow(id=uuid.uuid4(), name="coire-edge-b")
    calls: list[tuple[str, str]] = []

    class Client:
        async def hold_reservation(self, node: str, request: object) -> object:
            calls.append(("hold", node))
            return object()

        async def start_image_validate(self, node: str, request: Any) -> JobStatus:
            calls.append(("start", node))
            result = ImageAssetValidationResult(
                validated=True,
                kind=ModelKind.IMAGE_MODEL,
                manifest_sha256=manifest_sha256,
                source_revision=source_revision,
                peak_rss_bytes=100,
                peak_physical_bytes=200,
                peak_physical_delta_bytes=100,
                thumbnail_sha256="c" * 64,
                image_capability_profile=_profile(),
            )
            now = datetime.now(UTC)
            return JobStatus(
                job_id=request.job_id,
                kind=JobKind.IMAGE_VALIDATE,
                slug=model.slug,
                stage=JobStage.DONE,
                started_at=now,
                updated_at=now,
                result=result.model_dump(mode="json"),
            )

        async def release_reservation(self, node: str, reservation_id: uuid.UUID) -> None:
            calls.append(("release", node))

    reconciler = RegistryReconciler(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    for expected_count in (1, 2):
        assert not await reconciler._validate_image_copies(
            cast(AsyncSession, object()), cast(Any, Client()), job, model, origin, replica
        )
        assert len(job.image_validation or {}) == expected_count
        assert model.image_validated_at is None
        assert not any(action == "release" for action, _ in calls)
    assert await reconciler._validate_image_copies(
        cast(AsyncSession, object()), cast(Any, Client()), job, model, origin, replica
    )
    assert model.image_validated_at is not None
    assert model.image_capability_profile == _profile().model_dump(mode="json")
    assert calls[-2:] == [("release", origin.name), ("release", replica.name)]


async def test_two_verified_image_copies_remain_unready_until_validation() -> None:
    model = ModelRow(
        id=uuid.uuid4(),
        kind=ModelKind.IMAGE_MODEL,
        source="studio",
        state=ModelState.REPLICATING,
        image_capability_profile=None,
        image_validated_at=None,
    )

    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[ModelCopyRow]:
            return [ModelCopyRow(verified=True), ModelCopyRow(verified=True)]

    class Session:
        async def execute(self, query: object) -> Result:
            return Result()

        def add(self, row: object) -> None:
            pass

    session = cast(AsyncSession, Session())
    assert await service.recompute_state(session, model) is ModelState.REPLICATING
    model.image_capability_profile = _profile().model_dump(mode="json")
    model.image_validated_at = datetime.now(UTC)
    assert await service.recompute_state(session, model) is ModelState.READY


async def test_lora_validation_commands_bind_the_same_ready_base_on_both_copies() -> None:
    base = ModelRow(
        id=uuid.uuid4(),
        kind=ModelKind.IMAGE_MODEL,
        slug="org--base",
        state=ModelState.READY,
        source_revision="a" * 40,
        manifest_sha256="b" * 64,
    )
    adapter = ModelRow(
        id=uuid.uuid4(),
        kind=ModelKind.IMAGE_LORA,
        slug="org--adapter",
        state=ModelState.REPLICATING,
        source_revision="c" * 40,
        manifest_sha256="d" * 64,
        memory_estimate_bytes=10**9,
        capability_profile={"compatible_base_model_id": str(base.id)},
    )
    assert adapter.manifest_sha256 is not None and adapter.source_revision is not None
    adapter_manifest_sha256: str = adapter.manifest_sha256
    adapter_source_revision: str = adapter.source_revision
    job = DownloadJobRow(id=uuid.uuid4(), model_id=adapter.id, image_validation=None)
    origin = NodeRow(id=uuid.uuid4(), name="coire-edge-a")
    replica = NodeRow(id=uuid.uuid4(), name="coire-edge-b")
    commands: list[tuple[str, object]] = []

    class Session:
        async def get(self, model_type: type[object], identity: object) -> ModelRow:
            assert model_type is ModelRow and identity == base.id
            return base

    class Client:
        async def hold_reservation(self, node: str, request: object) -> object:
            commands.append(("hold", node))
            return object()

        async def start_image_validate(self, node: str, request: Any) -> JobStatus:
            assert request.compatible_base is not None
            assert request.compatible_base.model_id == base.id
            assert request.compatible_base.manifest_sha256 == base.manifest_sha256
            commands.append(("validate", node))
            now = datetime.now(UTC)
            return JobStatus(
                job_id=request.job_id,
                kind=JobKind.IMAGE_VALIDATE,
                slug=adapter.slug,
                stage=JobStage.DONE,
                started_at=now,
                updated_at=now,
                result=ImageAssetValidationResult(
                    validated=True,
                    kind=ModelKind.IMAGE_LORA,
                    manifest_sha256=adapter_manifest_sha256,
                    source_revision=adapter_source_revision,
                    peak_rss_bytes=100,
                    peak_physical_bytes=200,
                    peak_physical_delta_bytes=100,
                    thumbnail_sha256="e" * 64,
                ).model_dump(mode="json"),
            )

        async def release_reservation(self, node: str, reservation_id: uuid.UUID) -> None:
            commands.append(("release", node))

    reconciler = RegistryReconciler(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    for _ in (origin, replica):
        assert not await reconciler._validate_image_copies(
            cast(AsyncSession, Session()), cast(Any, Client()), job, adapter, origin, replica
        )
    assert await reconciler._validate_image_copies(
        cast(AsyncSession, Session()), cast(Any, Client()), job, adapter, origin, replica
    )
    assert adapter.image_validated_at is not None
    assert commands == [
        ("hold", origin.name),
        ("validate", origin.name),
        ("hold", replica.name),
        ("validate", replica.name),
        ("release", origin.name),
        ("release", replica.name),
    ]
