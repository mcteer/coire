"""Shifted token/mask loss for unchanged mlx-lm trainer injection points."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import mlx.core as mx


def masked_sft_loss(
    model: Callable[[mx.array], mx.array], tokens: mx.array, target_mask: mx.array
) -> tuple[mx.array, mx.array]:
    import mlx.core as mx
    from mlx.nn.losses import cross_entropy

    logits = model(tokens[:, :-1])
    targets = tokens[:, 1:]
    mask = target_mask[:, 1:]
    losses = cross_entropy(logits, targets, reduction="none").astype(mx.float32)
    count = mask.sum()
    # Selection excludes padded targets rather than multiplying potentially nonfinite
    # padded loss by zero. SftBatch preflight guarantees at least one target per row.
    total = mx.where(mask, losses, mx.array(0.0, dtype=mx.float32)).sum()
    return total / count.astype(mx.float32), count
