"""A restarted node keeps its fenced journal instead of reissuing work."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from coire_core.models.image_worker import (
    ImageTransferReceipt,
    ImageWorkerLoadRequest,
    ImageWorkerOutputManifest,
    ImageWorkerRunRequest,
    NodeImageCleanupRequest,
    NodeImageStartRequest,
)
from coire_core.models.images import (
    ImageSpec,
    ResolvedImageSpec,
    canonical_recipe_bytes,
    canonical_spec_hash,
)
from coire_node.image_cleanup import cleanup_image_outputs
from coire_node.image_jobs import ImageJobJournal, ImageJournalConflict
from coire_node.image_worker import run_image_job
from coire_node.testing.fake_image_worker import FakeImagePipeline

pytestmark = pytest.mark.integration
JOB = "01J00000000000000000000000"
NODE = "coire-edge-b"


def _request() -> NodeImageStartRequest:
    model_id = uuid.uuid4()
    spec = ImageSpec(
        model_id=model_id,
        prompt="restart fixture",
        width=64,
        height=64,
        steps=1,
        guidance=Decimal(0),
        seed=1,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(1,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )
    return NodeImageStartRequest(
        job_id=JOB,
        attempt=1,
        fence=1,
        node=NODE,
        model_id=model_id,
        instance_id=uuid.uuid4(),
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
        reservation_bytes=1024,
    )


def test_restart_replays_same_fence_and_refuses_changed_attempt(tmp_path: Path) -> None:
    original = _request()
    first = ImageJobJournal(tmp_path, NODE)
    queued = first.begin(original)
    running = first.advance(
        queued.model_copy(
            update={
                "state": "reserving",
                "pid": 12345,
                "process_create_time": 1.0,
                "updated_at": queued.updated_at + timedelta(microseconds=1),
            }
        )
    )
    restarted = ImageJobJournal(tmp_path, NODE)
    assert restarted.get(JOB) == running
    assert restarted.begin(original) == running
    with pytest.raises(ImageJournalConflict):
        restarted.begin(original.model_copy(update={"fence": 2}))
    assert restarted.get(JOB) == running


def test_fenced_receipt_cannot_clean_or_publish_a_different_attempt(tmp_path: Path) -> None:
    start = _request()
    load = ImageWorkerLoadRequest(
        slug="studio--fake-image",
        model_id=start.model_id,
        instance_id=start.instance_id,
        manifest_sha256=start.resolved.model_sha256,
        reservation_bytes=start.reservation_bytes,
        runtime_version=start.resolved.pipeline_version,
    )
    output = run_image_job(
        FakeImagePipeline(),
        load,
        ImageWorkerRunRequest(
            job_id=JOB,
            attempt=1,
            fence=1,
            instance_id=start.instance_id,
            resolved=start.resolved,
            deadline_at=start.deadline_at,
        ),
        tmp_path / "image-scratch",
        lambda *_: None,
    )[0]
    journal = ImageJobJournal(tmp_path, NODE)
    queued = journal.begin(start)
    reserving = journal.advance(
        queued.model_copy(
            update={
                "state": "reserving",
                "pid": 12345,
                "process_create_time": 1.0,
                "updated_at": queued.updated_at + timedelta(microseconds=1),
            }
        )
    )
    running = journal.advance(
        reserving.model_copy(
            update={
                "state": "running",
                "updated_at": reserving.updated_at + timedelta(microseconds=1),
            }
        )
    )
    transferring = journal.advance(
        running.model_copy(
            update={
                "state": "transferring",
                "outputs": (
                    ImageWorkerOutputManifest(
                        index=0,
                        byte_count=output.encoded.byte_count,
                        sha256=output.encoded.sha256,
                        recipe_sha256=hashlib.sha256(
                            canonical_recipe_bytes(output.encoded.recipe)
                        ).hexdigest(),
                    ),
                ),
                "updated_at": running.updated_at + timedelta(microseconds=1),
            }
        )
    )
    receipt = ImageTransferReceipt(
        job_id=JOB,
        attempt=1,
        fence=1,
        node=NODE,
        index=0,
        output_id=uuid.uuid4(),
        byte_count=output.encoded.byte_count,
        sha256=output.encoded.sha256,
        recipe_sha256=transferring.outputs[0].recipe_sha256,
        verified_at=datetime.now(UTC),
    )
    wrong = NodeImageCleanupRequest(
        job_id=JOB,
        attempt=1,
        fence=2,
        node=NODE,
        receipts=(receipt.model_copy(update={"fence": 2}),),
    )
    with pytest.raises(ImageJournalConflict):
        cleanup_image_outputs(journal, tmp_path, wrong)
    assert output.path.exists()
    assert journal.get(JOB) == transferring
    valid = NodeImageCleanupRequest(job_id=JOB, attempt=1, fence=1, node=NODE, receipts=(receipt,))
    assert cleanup_image_outputs(journal, tmp_path, valid).state == "cleaned"
    assert not output.path.exists()
