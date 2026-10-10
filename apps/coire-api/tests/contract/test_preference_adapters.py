"""Immutable ancestry snapshots survive parent retirement without granting trust."""

import uuid
from datetime import UTC, datetime

import pytest

from coire_api.training.lineage import compose_lineage
from coire_core.errors import TrainingConflict
from coire_core.models.adapters import InferenceTarget
from coire_core.models.feedback import AdapterLineage, AdapterLineageEntry


def entry(target: InferenceTarget) -> AdapterLineageEntry:
    assert target.adapter_id is not None
    return AdapterLineageEntry(
        adapter_id=target.adapter_id,
        target=target,
        objective="sft",
        source_job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        dataset_ids=[uuid.uuid4()],
        resolved_spec_sha256="c" * 64,
        created_at=datetime.now(UTC),
    )


def test_bare_and_chained_snapshots_keep_exact_parent_and_dataset_provenance() -> None:
    base = InferenceTarget(
        model_id=uuid.uuid4(), variant_id=uuid.uuid4(), base_manifest_sha256="a" * 64
    )
    parent = base.model_copy(
        update={"adapter_id": uuid.uuid4(), "adapter_manifest_sha256": "b" * 64}
    )
    assert parent.adapter_id is not None
    original = compose_lineage(parent.adapter_id, base, None, None, None)
    snapshot = entry(parent)
    child = compose_lineage(uuid.uuid4(), base, parent, snapshot, original)
    assert child.parent == parent and child.ancestors == [snapshot]
    assert AdapterLineage.model_validate_json(child.model_dump_json()) == child
    # Retirement changes registry state, not this self-contained immutable document.
    assert child.ancestors[0].target.adapter_manifest_sha256 == "b" * 64


def test_cycle_or_cross_base_and_depth_overflow_are_refused() -> None:
    base = InferenceTarget(
        model_id=uuid.uuid4(), variant_id=uuid.uuid4(), base_manifest_sha256="a" * 64
    )
    parent = base.model_copy(
        update={"adapter_id": uuid.uuid4(), "adapter_manifest_sha256": "b" * 64}
    )
    assert parent.adapter_id is not None
    with pytest.raises(TrainingConflict):
        compose_lineage(parent.adapter_id, base, parent, entry(parent), None)
    with pytest.raises(TrainingConflict):
        compose_lineage(
            uuid.uuid4(),
            base,
            parent.model_copy(update={"base_manifest_sha256": "d" * 64}),
            entry(parent),
            None,
        )
    prior = AdapterLineage(
        adapter_id=parent.adapter_id,
        base=base,
        parent=None,
        ancestors=[
            entry(
                base.model_copy(
                    update={"adapter_id": uuid.uuid4(), "adapter_manifest_sha256": "b" * 64}
                )
            )
            for _ in range(32)
        ],
    )
    with pytest.raises(TrainingConflict):
        compose_lineage(uuid.uuid4(), base, parent, entry(parent), prior)
