"""Memory estimation and fit.

Pure functions, deliberately: admission decisions have to be reproducible and testable without
a node, a database, or a model. Nothing here reads settings from the environment or the clock.

The estimate is a guess with a stated shape (research R6), not a measurement. The node reports
what an engine actually resides at, and the delta is recorded on every load (spec FR-014) so
feature 004 can correct the multipliers from data rather than from argument.
"""

from __future__ import annotations

from dataclasses import dataclass

from coire_core.models.jobs import RepoInspection

FP16_BYTES = 2
"""KV cache element size. mlx-lm's cache is fp16 unless quantised, which this feature does not
configure."""


def precision_label(inspection: RepoInspection) -> str:
    """A short, stable name for the weights' numeric format.

    `4bit-g64` for a grouped affine quantisation, `8bit` when there is no group size, the torch
    dtype (`bf16`) for an unquantised MLX repository, `unknown` when nothing says.
    """
    quant = inspection.quantization
    if quant is not None and quant.bits:
        if quant.group_size:
            return f"{quant.bits}bit-g{quant.group_size}"
        return f"{quant.bits}bit"
    dtype = (inspection.torch_dtype or "").lower()
    if dtype in ("bfloat16", "bf16"):
        return "bf16"
    if dtype in ("float16", "fp16", "half"):
        return "fp16"
    if dtype in ("float32", "fp32"):
        return "fp32"
    return "unknown"


def kv_bytes_per_token(inspection: RepoInspection) -> int:
    """KV cache cost of one token.

    `2 * layers * kv_heads * head_dim * element_bytes` — the keys and the values, per layer,
    per attention group. Returns 0 when the shape is unknown rather than inventing one; the
    caller then falls back to a flat headroom fraction.

    For Qwen3.8-27B (64 layers, 4 kv heads, head_dim 256) this is 256 KiB per token, so a
    128k-token context costs 32 GB on top of the weights — which is why the headroom is a
    setting rather than a constant.
    """
    layers = inspection.num_hidden_layers
    kv_heads = inspection.num_key_value_heads
    head_dim = inspection.head_dim
    if head_dim is None and inspection.hidden_size and inspection.num_attention_heads:
        head_dim = inspection.hidden_size // inspection.num_attention_heads
    if not layers or not kv_heads or not head_dim:
        return 0
    return 2 * layers * kv_heads * head_dim * FP16_BYTES


def estimate_bytes(
    inspection: RepoInspection,
    *,
    overhead: float,
    kv_headroom_tokens: int,
) -> int:
    """Memory a loaded engine is expected to occupy.

    `weights * overhead + kv_per_token * headroom`. When the model's shape is unknown the KV
    term degrades to a further 15 % of the weights — a crude but honest stand-in that is never
    silently zero.
    """
    weights = inspection.weight_bytes or inspection.total_bytes
    per_token = kv_bytes_per_token(inspection)
    kv = per_token * kv_headroom_tokens if per_token else int(weights * 0.15)
    return int(weights * overhead) + kv


@dataclass(frozen=True)
class NodeCapacity:
    """What one node can offer, as the fit checks need it."""

    name: str
    memory_budget_bytes: int
    store_free_bytes: int
    healthy: bool = True


@dataclass(frozen=True)
class FitResult:
    ok: bool
    required_bytes: int
    available_bytes: int
    """The figure the refusal quotes: the largest single-node budget for memory, the *smallest*
    free store for disk — because both Studios must hold a copy."""
    nodes: tuple[str, ...] = ()


def fits_memory(estimate: int, nodes: list[NodeCapacity]) -> FitResult:
    """Which healthy nodes could hold this model in memory.

    Single-node placements only; sharded placement is feature 006, and a model that fits
    neither Studio alone is refused here rather than queued (spec FR-010, clarification 5).
    """
    healthy = [n for n in nodes if n.healthy]
    fitting = tuple(n.name for n in healthy if n.memory_budget_bytes >= estimate)
    best = max((n.memory_budget_bytes for n in healthy), default=0)
    return FitResult(bool(fitting), estimate, best, fitting)


def fits_disk(total_bytes: int, nodes: list[NodeCapacity], *, reserve_bytes: int) -> FitResult:
    """Whether *every* healthy node can hold a copy, keeping the reserve free.

    Every node, not the best one: the two-copies rule means the roster is bounded by the
    smaller Studio's free disk (spec FR-008, edge case 1).
    """
    healthy = [n for n in nodes if n.healthy]
    required = total_bytes + reserve_bytes
    if not healthy:
        return FitResult(False, required, 0, ())
    smallest = min(n.store_free_bytes for n in healthy)
    fitting = tuple(n.name for n in healthy if n.store_free_bytes >= required)
    return FitResult(len(fitting) == len(healthy), required, smallest, fitting)
