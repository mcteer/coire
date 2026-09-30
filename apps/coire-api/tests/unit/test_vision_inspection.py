"""Admin inspection admits only complete preconverted visual repositories."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import ADMIN
from coire_api.nodes_client import NodeClient
from coire_api.registry.inspection import (
    classify_inspection,
    estimate_variant_memory_bytes,
    visual_recipe_rejection,
)
from coire_api.registry.placement import NodeView
from coire_core.memory import ENGINE_RUNTIME_BASELINE_BYTES
from coire_core.models.acquisition import (
    AcquisitionRequest,
    InspectionResult,
    Precision,
    VariantRecipe,
)
from coire_core.models.jobs import Quantization, RepoFile, RepoInspection
from coire_core.models.node import Reachability
from coire_core.models.registry import EngineBackend
from coire_core.settings import Settings

_FILES = (
    "config.json",
    "model.safetensors",
    "processor_config.json",
    "preprocessor_config.json",
    "tokenizer_config.json",
    "tokenizer.json",
)


def _inspect(*, mlx: bool = True, files: tuple[str, ...] = _FILES) -> RepoInspection:
    return RepoInspection(
        repo_id="org/visual",
        revision="a" * 40,
        files=[RepoFile(path=name, bytes=100) for name in files],
        total_bytes=600,
        weight_bytes=100,
        is_mlx_format=mlx,
        architecture="Idefics3ForConditionalGeneration",
    )


def _classify(repo: RepoInspection) -> InspectionResult:
    nodes = [NodeView("coire-edge-a", Reachability.HEALTHY, memory_budget_bytes=1024**3)]
    return classify_inspection(repo, nodes, Settings())


def test_complete_preconverted_vision_source_gets_vision_backend() -> None:
    result = _classify(_inspect())
    assert result.supported
    assert result.backend is EngineBackend.MLX_VLM
    assert result.source_format == "mlx"


def test_preconverted_text_estimate_keeps_actual_quantized_bytes() -> None:
    repo = _inspect().model_copy(update={"weight_bytes": 880_000_000, "total_bytes": 900_000_000})
    estimated = estimate_variant_memory_bytes(
        repo, Precision.BIT4, EngineBackend.MLX_LM, Settings()
    )
    assert estimated >= repo.total_bytes + ENGINE_RUNTIME_BASELINE_BYTES
    assert estimated > int(repo.weight_bytes * 0.32)


def test_raw_source_estimate_uses_conversion_ratio_once() -> None:
    repo = _inspect(mlx=False).model_copy(
        update={"weight_bytes": 880_000_000, "total_bytes": 900_000_000}
    )
    estimated = estimate_variant_memory_bytes(
        repo, Precision.BIT4, EngineBackend.MLX_LM, Settings()
    )
    assert estimated < repo.total_bytes
    assert estimated >= int(repo.weight_bytes * 0.32) + ENGINE_RUNTIME_BASELINE_BYTES


def test_missing_processor_file_is_refused_before_weight_transfer() -> None:
    result = _classify(
        _inspect(files=tuple(name for name in _FILES if name != "preprocessor_config.json"))
    )
    assert result.supported is False
    assert result.rejection_code == "incomplete_visual_processor"


def test_raw_vision_source_is_refused_without_conversion() -> None:
    result = _classify(_inspect(mlx=False))
    assert result.supported is False
    assert result.rejection_code == "vision_requires_preconverted_mlx"


def test_visual_source_without_weights_is_refused() -> None:
    result = _classify(
        _inspect(files=tuple(name for name in _FILES if not name.endswith(".safetensors")))
    )
    assert result.supported is False
    assert result.rejection_code == "missing_visual_weights"


def test_other_visual_architecture_is_not_misclassified_as_text() -> None:
    repo = _inspect().model_copy(update={"architecture": "Qwen2VLForConditionalGeneration"})
    result = _classify(repo)
    assert result.supported is False
    assert result.rejection_code == "unsupported_visual_architecture"


def test_preconverted_visual_recipe_must_match_inspected_quantization() -> None:
    source = _inspect().model_copy(update={"quantization": Quantization(bits=4, group_size=64)})
    assert (
        visual_recipe_rejection(source, VariantRecipe(name="upstream", precision=Precision.BIT4))
        is None
    )
    assert visual_recipe_rejection(source, VariantRecipe(name="mismatch", precision=Precision.BIT6))
    assert visual_recipe_rejection(
        source,
        VariantRecipe(name="mismatch", precision=Precision.BIT4, bits=4, group_size=128),
    )


async def test_admin_submission_audits_unverifiable_visual_recipe_before_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.routes import admin_acquisitions

    class _EmptyResult:
        def scalar_one_or_none(self) -> None:
            return None

    session = SimpleNamespace(execute=AsyncMock(return_value=_EmptyResult()), commit=AsyncMock())
    nodes = [
        NodeView("coire-edge-a", Reachability.HEALTHY, memory_budget_bytes=10_000),
        NodeView("coire-edge-b", Reachability.HEALTHY, memory_budget_bytes=10_000),
    ]
    reject = AsyncMock()
    submit = AsyncMock()
    monkeypatch.setattr("coire_api.registry.service.node_views", AsyncMock(return_value=nodes))
    monkeypatch.setattr("coire_api.registry.acquisition.reject", reject)
    monkeypatch.setattr("coire_api.registry.acquisition.submit", submit)
    client = SimpleNamespace(inspect=AsyncMock(return_value=_inspect()))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(reconciler=None)))
    body = AcquisitionRequest(
        repo_id="org/visual", variant=VariantRecipe(name="upstream", precision=Precision.BIT4)
    )
    with pytest.raises(HTTPException) as raised:
        await admin_acquisitions.submit_acquisition(
            body,
            cast(Request, request),
            Response(),
            ADMIN,
            cast(AsyncSession, session),
            Settings(),
            cast(NodeClient, client),
        )
    assert raised.value.status_code == 422
    detail = cast(dict[str, object], raised.value.detail)
    assert detail["code"] == "unsupported_visual_recipe"
    assert detail["bytes_transferred"] == 0
    reject.assert_awaited_once()
    submit.assert_not_awaited()


async def test_supported_preconverted_visual_source_enters_admin_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.routes import admin_acquisitions

    nodes = [
        NodeView("coire-edge-a", Reachability.HEALTHY, memory_budget_bytes=10_000),
        NodeView("coire-edge-b", Reachability.HEALTHY, memory_budget_bytes=10_000),
    ]
    rows = [SimpleNamespace(name=node.name, id=uuid.uuid4()) for node in nodes]

    class _EmptyResult:
        def scalar_one_or_none(self) -> None:
            return None

    class _NodeResult:
        def scalars(self) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: rows)

    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_EmptyResult(), _NodeResult()]),
        commit=AsyncMock(),
    )
    expected = object()
    submit = AsyncMock(return_value=(None, None, object(), True))
    reject = AsyncMock()
    monkeypatch.setattr("coire_api.registry.service.node_views", AsyncMock(return_value=nodes))
    monkeypatch.setattr("coire_api.registry.acquisition.submit", submit)
    monkeypatch.setattr("coire_api.registry.acquisition.reject", reject)
    monkeypatch.setattr(
        "coire_api.registry.acquisition.projection", AsyncMock(return_value=expected)
    )
    source = _inspect().model_copy(update={"quantization": Quantization(bits=4, group_size=64)})
    client = SimpleNamespace(inspect=AsyncMock(return_value=source))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(reconciler=None)))
    body = AcquisitionRequest(
        repo_id="org/visual", variant=VariantRecipe(name="upstream", precision=Precision.BIT4)
    )
    result = await admin_acquisitions.submit_acquisition(
        body,
        cast(Request, request),
        Response(),
        ADMIN,
        cast(AsyncSession, session),
        Settings(),
        cast(NodeClient, client),
    )
    assert result is expected
    awaited = submit.await_args
    assert awaited is not None
    assert awaited.kwargs["inspection"]["backend"] == "mlx_vlm"
    assert awaited.kwargs["weight_bytes"] == 100
    reject.assert_not_awaited()
