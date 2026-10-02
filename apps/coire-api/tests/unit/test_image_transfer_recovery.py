"""Transfer recovery settles only receipts that core and Studio agree on."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageJobRow, ImageTransferRow, NodeRow
from coire_core.errors import ImageConflict
from coire_core.models.image_worker import (
    ImageTransferGrant,
    ImageTransferGrantRequest,
    ImageTransferReceipt,
    ImageWorkerOutputManifest,
    NodeImageJob,
    NodeImageTransferRequest,
)
from coire_core.models.images import (
    ImageClassificationResult,
    ImageContentTag,
    ImageJobSettingsSnapshot,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_scheduler import images

JOB = "01J00000000000000000000000"
NODE = "coire-edge-b"


def _resolved() -> ResolvedImageSpec:
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        prompt="private",
        width=64,
        height=64,
        steps=4,
        guidance=Decimal(0),
        seed=7,
    )
    return ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )


class FakeSession:
    def __init__(self, stored: ImageTransferReceipt) -> None:
        self.node = NodeRow(id=uuid.uuid4(), name=NODE)
        self.instance_id = uuid.uuid4()
        resolved = _resolved()
        self.job = ImageJobRow(
            id=JOB,
            state="transferring",
            attempt=1,
            fence=4,
            selected_node_id=self.node.id,
            instance_id=self.instance_id,
            cancel_requested_at=None,
            resolved_spec=ImageJobSettingsSnapshot(
                effective_spec=resolved.spec, resolved=resolved
            ).model_dump(mode="json"),
            cleanup_state="pending",
            receipt_state="pending",
        )
        self.transfer = ImageTransferRow(
            job_id=JOB,
            attempt=1,
            output_index=0,
            expected_bytes=100,
            expected_sha256=stored.sha256,
            staging_key=f"image-staging/{JOB}/1/0.png",
            state="received",
            receipt=stored.model_dump(mode="json"),
            lease_expires_at=datetime.now(UTC),
            grant_hash="c" * 64,
        )
        self.commits = 0

    async def get(self, model: type[object], identity: object, **_: object) -> Any:
        if model is ImageJobRow:
            return self.job if identity == JOB else None
        if model is NodeRow:
            return self.node if identity == self.node.id else None
        if model is ImageTransferRow:
            return self.transfer if identity == (JOB, 1, 0) else None
        raise AssertionError(model)


@pytest.mark.asyncio
async def test_restart_reconciles_existing_node_cleanup_without_rerunning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    receipt = ImageTransferReceipt(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        index=0,
        output_id=uuid.uuid4(),
        byte_count=100,
        sha256="a" * 64,
        recipe_sha256="b" * 64,
        verified_at=now,
    )
    session = FakeSession(receipt)
    status = NodeImageJob(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        instance_id=session.instance_id,
        state="succeeded",
        receipts=(receipt,),
        scratch_cleaned=True,
        updated_at=now,
    )
    observed: list[str] = []

    @asynccontextmanager
    async def scope() -> Any:
        yield cast(AsyncSession, session)
        session.commits += 1

    class FakeNodeClient:
        def __init__(self, _: object) -> None:
            pass

        async def __aenter__(self) -> FakeNodeClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def image_job_status(self, node: str, binding: object) -> NodeImageJob:
            observed.append(node)
            return status

        async def transfer_image_job(self, node: str, command: object) -> NodeImageJob:
            raise AssertionError("generation and transfer must not repeat")

    monkeypatch.setattr(images, "session_scope", scope)
    monkeypatch.setattr(images, "NodeClient", FakeNodeClient)
    monkeypatch.setattr(images, "get_settings", lambda: object())
    await images.drive_image_transfer(JOB)
    assert observed == [NODE]
    assert session.job.cleanup_state == "cleaned"
    assert session.job.receipt_state == "complete"
    assert session.transfer.node_cleanup_ack_at is not None
    assert session.commits == 2

    classification = ImageClassificationResult(
        tag=ImageContentTag.NORMAL,
        score=Decimal("0.1"),
        classifier_revision="a" * 40,
        processor_sha256="b" * 64,
        tagged_at=now,
    )
    status = status.model_copy(
        update={
            "outputs": (
                ImageWorkerOutputManifest(
                    index=0,
                    byte_count=receipt.byte_count,
                    sha256=receipt.sha256,
                    recipe_sha256=receipt.recipe_sha256,
                    classification=classification,
                ),
            ),
            "receipts": (receipt.model_copy(update={"classification": classification}),),
        }
    )
    session.job.cleanup_state = "pending"
    session.job.receipt_state = "pending"
    session.transfer.node_cleanup_ack_at = None
    session.transfer.receipt = receipt.model_dump(mode="json")
    await images.drive_image_transfer(JOB)
    assert (
        ImageTransferReceipt.model_validate(session.transfer.receipt).classification
        == classification
    )

    session.job.cleanup_state = "pending"
    session.job.receipt_state = "pending"
    session.transfer.node_cleanup_ack_at = None
    session.transfer.receipt = receipt.model_copy(update={"sha256": "f" * 64}).model_dump(
        mode="json"
    )
    with pytest.raises(ImageConflict, match="core and node image receipts differ"):
        await images.drive_image_transfer(JOB)
    assert session.job.cleanup_state == "pending"
    assert session.transfer.node_cleanup_ack_at is None


@pytest.mark.asyncio
async def test_recovery_renews_exact_grant_and_never_resends_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    receipt = ImageTransferReceipt(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        index=0,
        output_id=uuid.uuid4(),
        byte_count=100,
        sha256="a" * 64,
        recipe_sha256="b" * 64,
        verified_at=now,
    )
    session = FakeSession(receipt)
    manifest = ImageWorkerOutputManifest(
        index=0, byte_count=100, sha256=receipt.sha256, recipe_sha256=receipt.recipe_sha256
    )
    generated = NodeImageJob(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        instance_id=session.instance_id,
        state="transferring",
        outputs=(manifest,),
        updated_at=now,
    )
    cleaned = generated.model_copy(
        update={
            "state": "succeeded",
            "receipts": (receipt,),
            "scratch_cleaned": True,
            "updated_at": now + timedelta(seconds=1),
        }
    )
    commands: list[NodeImageTransferRequest] = []

    @asynccontextmanager
    async def scope() -> Any:
        yield cast(AsyncSession, session)
        session.commits += 1

    class FakeNodeClient:
        def __init__(self, _: object) -> None:
            pass

        async def __aenter__(self) -> FakeNodeClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def image_job_status(self, node: str, binding: object) -> NodeImageJob:
            return generated

        async def transfer_image_job(
            self, node: str, command: NodeImageTransferRequest
        ) -> NodeImageJob:
            commands.append(command)
            return cleaned

    async def grant(_: object, request: ImageTransferGrantRequest) -> ImageTransferGrant:
        assert request.job_id == JOB
        return ImageTransferGrant(
            **request.model_dump(),
            token="new-transfer-token",
            issued_at=now,
            expires_at=now + timedelta(minutes=1),
        )

    monkeypatch.setattr(images, "session_scope", scope)
    monkeypatch.setattr(images, "NodeClient", FakeNodeClient)
    monkeypatch.setattr(images, "get_settings", lambda: object())
    monkeypatch.setattr(images, "mint_transfer_grant", grant)
    await images.drive_image_transfer(JOB)
    assert len(commands) == 1
    assert commands[0].grants[0].expected_sha256 == receipt.sha256
    assert commands[0].fence == 4
    assert session.job.cleanup_state == "cleaned"
    assert session.commits == 3
