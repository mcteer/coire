"""Image node and worker commands are attempt-fenced registry-only messages."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from coire_core.models.image_worker import (
    ImageTransferGrant,
    ImageTransferReceipt,
    ImageWorkerLoadRequest,
    ImageWorkerRunRequest,
    NodeImageCleanupRequest,
    NodeImageInputManifest,
    NodeImageStartRequest,
)
from coire_core.models.images import (
    ImageInputDigest,
    ImageMode,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)

MODEL = uuid.UUID("10000000-0000-0000-0000-000000000001")
INSTANCE = uuid.UUID("20000000-0000-0000-0000-000000000001")
OUTPUT = uuid.UUID("30000000-0000-0000-0000-000000000001")
JOB = "01J00000000000000000000000"


def _resolved() -> ResolvedImageSpec:
    spec = ImageSpec(
        model_id=MODEL,
        prompt="fox",
        width=512,
        height=512,
        steps=20,
        guidance=Decimal("3.25"),
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


def _receipt(**overrides: object) -> ImageTransferReceipt:
    values: dict[str, object] = {
        "job_id": JOB,
        "attempt": 1,
        "fence": 1,
        "node": "coire-edge-b",
        "index": 0,
        "output_id": OUTPUT,
        "byte_count": 1024,
        "sha256": "c" * 64,
        "recipe_sha256": "d" * 64,
        "verified_at": datetime.now(UTC),
    }
    values.update(overrides)
    return ImageTransferReceipt.model_validate(values)


def test_worker_load_is_mflux_registry_only() -> None:
    load = ImageWorkerLoadRequest(
        slug="studio--z-image-turbo",
        model_id=MODEL,
        instance_id=INSTANCE,
        manifest_sha256="a" * 64,
        reservation_bytes=1024,
        runtime_version="mflux-0.20.0",
    )
    assert load.backend == "mflux"
    for override in (
        {"backend": "mlx_lm"},
        {"model_path": "/tmp/model"},
        {"model_id": "org/model"},
        {"slug": "../weights"},
        {"slug": "studio--model\n"},
    ):
        with pytest.raises(ValidationError):
            ImageWorkerLoadRequest.model_validate({**load.model_dump(), **override})


def test_node_start_binds_uuid_ulid_model_and_fence() -> None:
    request = NodeImageStartRequest(
        job_id=JOB,
        attempt=1,
        fence=4,
        node="coire-edge-b",
        model_id=MODEL,
        instance_id=INSTANCE,
        resolved=_resolved(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
        reservation_bytes=1024,
    )
    assert request.resolved.spec.model_id == MODEL
    for override in (
        {"job_id": str(MODEL)},
        {"attempt": 0},
        {"fence": 0},
        {"model_id": INSTANCE},
        {"path": "/tmp/evil"},
    ):
        with pytest.raises(ValidationError):
            NodeImageStartRequest.model_validate({**request.model_dump(), **override})


def test_cleanup_rejects_mismatched_receipt_and_duplicate_index() -> None:
    receipt = _receipt()
    NodeImageCleanupRequest(
        job_id=JOB, attempt=1, fence=1, node="coire-edge-b", receipts=(receipt,)
    )
    with pytest.raises(ValidationError, match="receipt"):
        NodeImageCleanupRequest(
            job_id=JOB, attempt=1, fence=2, node="coire-edge-b", receipts=(receipt,)
        )
    with pytest.raises(ValidationError, match="index"):
        NodeImageCleanupRequest(
            job_id=JOB,
            attempt=1,
            fence=1,
            node="coire-edge-b",
            receipts=(receipt, _receipt(output_id=uuid.uuid4())),
        )
    with pytest.raises(ValidationError, match="receipt"):
        NodeImageCleanupRequest(
            job_id=JOB, attempt=1, fence=1, node="coire-edge-a", receipts=(receipt,)
        )


def test_transfer_grant_is_bounded_to_node_attempt_and_expiry() -> None:
    now = datetime.now(UTC)
    grant = ImageTransferGrant(
        job_id=JOB,
        attempt=1,
        fence=1,
        node="coire-edge-b",
        index=0,
        expected_bytes=1024,
        expected_sha256="a" * 64,
        token="opaque-token",
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    assert grant.node == "coire-edge-b"
    with pytest.raises(ValidationError, match="expires_at"):
        ImageTransferGrant.model_validate(
            {**grant.model_dump(), "expires_at": now + timedelta(hours=1)}
        )


def test_worker_run_rejects_input_digest_substitution() -> None:
    source = uuid.uuid4()
    image = ImageSpec(
        model_id=MODEL,
        mode=ImageMode.IMG2IMG,
        prompt="fox",
        width=512,
        height=512,
        steps=20,
        guidance=Decimal("3.25"),
        seed=7,
        init_image_id=source,
        strength=Decimal("0.5"),
    )
    resolved = ResolvedImageSpec(
        spec=image,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(image),
        inputs=(ImageInputDigest(input_id=source, sha256="c" * 64, width=512, height=512),),
    )
    manifest = NodeImageInputManifest(
        input_id=source, purpose="init", sha256="c" * 64, byte_count=1024, width=512, height=512
    )
    ImageWorkerRunRequest(
        job_id=JOB,
        attempt=1,
        fence=1,
        instance_id=INSTANCE,
        resolved=resolved,
        inputs=(manifest,),
        deadline_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError, match="inputs"):
        ImageWorkerRunRequest.model_validate(
            {
                "job_id": JOB,
                "attempt": 1,
                "fence": 1,
                "instance_id": INSTANCE,
                "resolved": resolved,
                "inputs": [{**manifest.model_dump(), "sha256": "d" * 64}],
                "deadline_at": datetime.now(UTC),
            }
        )
