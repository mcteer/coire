"""Memory estimation and fit (T012)."""

from __future__ import annotations

from typing import ClassVar

from coire_core.memory import (
    NodeCapacity,
    estimate_bytes,
    fits_disk,
    fits_memory,
    kv_bytes_per_token,
    precision_label,
)
from coire_core.models.jobs import Quantization, RepoInspection

GiB = 1024**3
GB = 1000**3


def _inspection(**kw: object) -> RepoInspection:
    base = {
        "repo_id": "mlx-community/x",
        "revision": "sha",
        "total_bytes": 16 * GiB,
        "weight_bytes": 16 * GiB,
        "is_mlx_format": True,
    }
    return RepoInspection(**{**base, **kw})  # type: ignore[arg-type]


# The real thing (research R12): Qwen3.8-27B-4bit as the Hub reports it.
QWEN = _inspection(
    weight_bytes=16_054_541_349,
    total_bytes=16_081_800_000,
    quantization=Quantization(bits=4, group_size=64, mode="affine"),
    num_hidden_layers=64,
    num_key_value_heads=4,
    head_dim=256,
    max_position_embeddings=262_144,
    sizing_from_text_config=True,
)


class TestPrecisionLabel:
    def test_grouped_quantisation(self) -> None:
        assert precision_label(QWEN) == "4bit-g64"

    def test_ungrouped_quantisation(self) -> None:
        assert precision_label(_inspection(quantization=Quantization(bits=8))) == "8bit"

    def test_unquantised_uses_the_dtype(self) -> None:
        assert precision_label(_inspection(torch_dtype="bfloat16")) == "bf16"

    def test_unknown_is_named_not_guessed(self) -> None:
        assert precision_label(_inspection()) == "unknown"


class TestKVCache:
    def test_qwen_costs_256_kib_per_token(self) -> None:
        """2 * 64 layers * 4 kv heads * 256 head_dim * 2 bytes."""
        assert kv_bytes_per_token(QWEN) == 262_144

    def test_head_dim_is_derived_when_absent(self) -> None:
        derived = _inspection(
            num_hidden_layers=2, num_key_value_heads=2, hidden_size=512, num_attention_heads=8
        )
        assert kv_bytes_per_token(derived) == 2 * 2 * 2 * 64 * 2

    def test_unknown_shape_yields_zero_rather_than_a_guess(self) -> None:
        assert kv_bytes_per_token(_inspection()) == 0


class TestEstimate:
    def test_qwen_worked_example(self) -> None:
        """Research R6: ~26.2 GB — weights at 1.10 (17.7 GB) plus 32k tokens of KV (8.6 GB).

        Asserted in decimal GB because that is the unit the Hub reports sizes in and the unit
        R6 quotes; 26.2 GB is 24.4 GiB, and conflating the two is exactly how a fit check ends
        up wrong by 7%.
        """
        estimate = estimate_bytes(QWEN, overhead=1.10, kv_headroom_tokens=32_768)
        assert 26.0 * GB < estimate < 26.5 * GiB
        assert estimate == int(16_054_541_349 * 1.10) + 262_144 * 32_768

    def test_headroom_scales_with_context(self) -> None:
        small = estimate_bytes(QWEN, overhead=1.10, kv_headroom_tokens=4_096)
        large = estimate_bytes(QWEN, overhead=1.10, kv_headroom_tokens=131_072)
        assert large - small == 262_144 * (131_072 - 4_096)

    def test_unknown_shape_still_reserves_something(self) -> None:
        """A missing KV term must never make the estimate look like bare weights."""
        estimate = estimate_bytes(_inspection(), overhead=1.10, kv_headroom_tokens=32_768)
        assert estimate > 16 * GiB * 1.10


class TestFit:
    NODES: ClassVar[list[NodeCapacity]] = [
        NodeCapacity("coire-edge-a", memory_budget_bytes=230 * GiB, store_free_bytes=1700 * GiB),
        NodeCapacity("coire-edge-b", memory_budget_bytes=230 * GiB, store_free_bytes=1700 * GiB),
    ]

    def test_a_model_that_fits_names_every_candidate(self) -> None:
        result = fits_memory(26 * GiB, self.NODES)
        assert result.ok and set(result.nodes) == {"coire-edge-a", "coire-edge-b"}

    def test_a_model_that_fits_neither_is_refused_with_the_figures(self) -> None:
        result = fits_memory(400 * GiB, self.NODES)
        assert not result.ok
        assert result.required_bytes == 400 * GiB
        assert result.available_bytes == 230 * GiB

    def test_a_model_fitting_only_the_larger_node_is_accepted_there(self) -> None:
        nodes = [
            NodeCapacity("coire-edge-a", 230 * GiB, 1700 * GiB),
            NodeCapacity("coire-edge-b", 100 * GiB, 1700 * GiB),
        ]
        result = fits_memory(150 * GiB, nodes)
        assert result.ok and result.nodes == ("coire-edge-a",)

    def test_unreachable_nodes_are_not_candidates(self) -> None:
        nodes = [NodeCapacity("coire-edge-a", 230 * GiB, 1700 * GiB, healthy=False)]
        assert not fits_memory(1 * GiB, nodes).ok

    def test_disk_requires_every_node_not_the_roomiest(self) -> None:
        """Two copies is the rule, so the smaller Studio bounds the roster."""
        nodes = [
            NodeCapacity("coire-edge-a", 230 * GiB, 1700 * GiB),
            NodeCapacity("coire-edge-b", 230 * GiB, 20 * GiB),
        ]
        result = fits_disk(100 * GiB, nodes, reserve_bytes=50 * GiB)
        assert not result.ok
        assert result.available_bytes == 20 * GiB
        assert result.required_bytes == 150 * GiB

    def test_disk_reserve_is_included_in_the_requirement(self) -> None:
        nodes = [NodeCapacity("coire-edge-a", 230 * GiB, 120 * GiB)]
        assert not fits_disk(100 * GiB, nodes, reserve_bytes=50 * GiB).ok
        assert fits_disk(60 * GiB, nodes, reserve_bytes=50 * GiB).ok

    def test_no_healthy_nodes_never_fits(self) -> None:
        assert not fits_disk(1, [], reserve_bytes=0).ok
