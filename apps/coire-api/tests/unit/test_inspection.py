from __future__ import annotations

import pytest

from coire_api.registry.inspection import classify_inspection
from coire_api.registry.placement import NodeView
from coire_core.models.jobs import RepoInspection
from coire_core.models.node import Reachability
from coire_core.settings import Settings


def _repo(**changes: object) -> RepoInspection:
    values: dict[str, object] = {
        "repo_id": "org/model",
        "revision": "a" * 40,
        "total_bytes": 2_100,
        "weight_bytes": 2_000,
        "is_mlx_format": False,
        "architecture": "Qwen3ForCausalLM",
    }
    values.update(changes)
    return RepoInspection.model_validate(values)


def _nodes(budget: int = 10_000) -> list[NodeView]:
    return [NodeView("coire-edge-a", Reachability.HEALTHY, memory_budget_bytes=budget)]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"has_gguf_only": True}, "gguf_only"),
        ({"architecture": "UnknownForCausalLM"}, "unsupported_architecture"),
        ({"gated": True}, "gated"),
    ],
)
def test_specific_early_rejections(changes: dict[str, object], code: str) -> None:
    result = classify_inspection(_repo(**changes), _nodes(), Settings())
    assert result.supported is False
    assert result.rejection_code == code


def test_no_fit_reports_required_and_available_per_placement() -> None:
    result = classify_inspection(_repo(weight_bytes=1_000_000), _nodes(100), Settings())
    assert result.rejection_code == "no_fit_memory"
    assert result.fit
    assert all(not decision.fits for decision in result.fit)
    assert result.fit[0].available_bytes == 100


def test_raw_and_mlx_sources_are_accepted_without_transferring_weights() -> None:
    raw = classify_inspection(_repo(), _nodes(), Settings())
    mlx = classify_inspection(_repo(is_mlx_format=True), _nodes(), Settings())
    assert raw.supported and raw.source_format == "safetensors"
    assert mlx.supported and mlx.source_format == "mlx"
    assert raw.metadata_bytes == 100
