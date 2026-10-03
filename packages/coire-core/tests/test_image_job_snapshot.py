"""Queued jobs retain exact settings before the selected Studio is known."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from coire_core.models.images import (
    ImageJob,
    ImageJobSettingsSnapshot,
    ImageJobState,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)

JOB_ID = "01K00000000000000000000000"


def _spec(seed: int = 7) -> ImageSpec:
    return ImageSpec(
        model_id=uuid.uuid4(),
        prompt="portrait",
        width=512,
        height=512,
        steps=9,
        guidance=Decimal("1.5"),
        seed=seed,
    )


def _resolved(spec: ImageSpec) -> ResolvedImageSpec:
    assert spec.seed is not None
    return ResolvedImageSpec(
        spec=spec,
        seeds=(spec.seed,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )


def test_queued_job_has_effective_spec_but_no_runtime_claim() -> None:
    spec = _spec()
    snapshot = ImageJobSettingsSnapshot(effective_spec=spec)
    assert ImageJobSettingsSnapshot.model_validate(snapshot.model_dump(mode="json")) == snapshot
    job = ImageJob(
        id=JOB_ID,
        state=ImageJobState.QUEUED,
        effective_spec=spec,
        resolved=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert snapshot.resolved is None and job.effective_spec.seed == 7
    with pytest.raises(ValidationError, match="resolved"):
        ImageJob(
            id=JOB_ID,
            state=ImageJobState.RUNNING,
            effective_spec=spec,
            resolved=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    running = ImageJob(
        id=JOB_ID,
        state=ImageJobState.RUNNING,
        effective_spec=spec,
        resolved=_resolved(spec),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert running.resolved is not None


def test_runtime_binding_is_exactly_once_and_matching() -> None:
    spec = _spec()
    snapshot = ImageJobSettingsSnapshot(effective_spec=spec)
    bound = snapshot.bind(_resolved(spec))
    assert ImageJobSettingsSnapshot.model_validate(bound.model_dump(mode="json")) == bound
    assert bound.resolved is not None and bound.bind(_resolved(spec)) == bound
    with pytest.raises(ValueError, match="effective_spec"):
        snapshot.bind(_resolved(_spec(seed=8)))
    with pytest.raises(ValueError, match="already"):
        bound.bind(_resolved(_spec(seed=8)))
    with pytest.raises(ValidationError, match="effective_spec"):
        ImageJobSettingsSnapshot(effective_spec=spec, resolved=_resolved(_spec(seed=8)))
