"""Exact initialization preflight fails unavailable, incompatible or deep parents."""

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import AdapterLineageRow, TrainingAdapterRow, TrainingJobRow
from coire_api.training.preference_specs import resolve_initial_target
from coire_core.errors import TrainingConflict, TrainingNotFound, TrainingValidationError
from coire_core.models.training import TrainingSpecV3

FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "tests/fixtures/preference/legacy_training/resolved_v1.json"
)


@pytest.mark.parametrize(
    "change",
    [
        None,
        "missing",
        "retired",
        "base",
        "variant",
        "parameterization",
        "configuration",
        "resolved_digest",
        "mirror",
        "depth",
        "lineage",
    ],
)
async def test_exact_parent_preflight(change: str | None) -> None:
    raw = json.loads(await asyncio.to_thread(FIXTURE.read_text))
    parent_id = uuid.uuid4()
    spec = TrainingSpecV3.model_validate(
        {
            **raw["spec"],
            "schema_version": 3,
            "objective": "dpo",
            "objective_options": {"beta": 0.1},
            "init_adapter": str(parent_id),
        }
    )
    parent = TrainingAdapterRow(
        id=parent_id,
        model_id=spec.model.model_id,
        base_variant_id=spec.model.variant_id,
        base_manifest_sha256=raw["base_manifest_sha256"],
        manifest_sha256="c" * 64,
        parameterization=spec.parameterization.kind,
        purpose="serving",
        objective="sft",
        state="ready",
        source_job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        resolved_spec_sha256="d" * 64,
    )
    origin = TrainingJobRow(id=parent.source_job_id, resolved_spec=raw, resolved_sha256="d" * 64)
    lineage = None
    copies = [("coire-edge-a", "c" * 64), ("coire-edge-b", "c" * 64)]
    if change == "retired":
        parent.state = "retired"
    elif change == "base":
        parent.base_manifest_sha256 = "f" * 64
    elif change == "variant":
        parent.base_variant_id = uuid.uuid4()
    elif change == "parameterization":
        parent.parameterization = "dora"
    elif change == "configuration":
        raw["spec"]["parameterization"]["rank"] += 1
    elif change == "resolved_digest":
        parent.resolved_spec_sha256 = "f" * 64
    elif change == "mirror":
        copies.pop()
    elif change == "depth":
        lineage = AdapterLineageRow(adapter_id=parent_id, depth=32)
    elif change == "lineage":
        parent.objective = "dpo"

    async def get(model: object, identity: object, **kwargs: object) -> object:
        if model is TrainingAdapterRow:
            return None if change == "missing" else parent
        if model is TrainingJobRow:
            return origin
        if model is AdapterLineageRow:
            return lineage
        raise AssertionError("unexpected model")

    session = AsyncMock(spec=AsyncSession)
    session.get.side_effect = get
    session.execute.return_value = SimpleNamespace(all=lambda: copies)
    if change is not None:
        with pytest.raises((TrainingConflict, TrainingNotFound, TrainingValidationError)):
            await resolve_initial_target(session, spec, raw["base_manifest_sha256"])
    else:
        target = await resolve_initial_target(session, spec, raw["base_manifest_sha256"])
        assert target.adapter_id == parent_id and target.adapter_manifest_sha256 == "c" * 64
        session.get.reset_mock()
        bare = await resolve_initial_target(
            session, spec.model_copy(update={"init_adapter": None}), raw["base_manifest_sha256"]
        )
        assert bare.adapter_id is None and bare.adapter_manifest_sha256 is None
        session.get.assert_not_awaited()
