"""Versioned pure mixture ordering shared by training and Studio input-history replay."""

import hashlib
import uuid
from collections.abc import Sequence
from typing import Literal

from coire_core.models.training_node import SingleSourceSamplerState


class _CounterRandom:
    """Versioned platform-independent unbiased integer draws and Fisher-Yates shuffle."""

    def __init__(self, key: bytes) -> None:
        self.key = key
        self.counter = 0

    def below(self, bound: int) -> int:
        limit = 2**256 - (2**256 % bound)
        while True:
            value = int.from_bytes(
                hashlib.sha256(self.key + self.counter.to_bytes(16, "big")).digest()
            )
            self.counter += 1
            if value < limit:
                return value % bound

    def shuffle(self, values: list[int]) -> None:
        for index in range(len(values) - 1, 0, -1):
            other = self.below(index + 1)
            values[index], values[other] = values[other], values[index]


def mixture_epoch_order(
    identity_sha256: str,
    epoch: int,
    pools: Sequence[tuple[int, int]],
    *,
    strategy: Literal["weighted", "sequential"],
    replacement: bool,
) -> list[tuple[int, int]]:
    if (
        not 1 <= len(pools) <= 16
        or not 0 <= epoch < 2**64
        or strategy not in ("weighted", "sequential")
    ):
        raise ValueError("Mixture replay configuration is invalid")
    if sum(quota for _, quota in pools) > 16_000_000 or any(
        not 1 <= size <= 1_000_000
        or not 0 <= quota <= 16_000_000
        or (not replacement and quota > size)
        for size, quota in pools
    ):
        raise ValueError("Mixture replay pool exceeds bounds")
    order: list[tuple[int, int]] = []
    for index, (size, quota) in enumerate(pools):
        rng = _CounterRandom(f"{identity_sha256}:{epoch}:source:{index}".encode())
        pool = list(range(size))
        if replacement:
            draws = [rng.below(size) for _ in range(quota)]
        else:
            rng.shuffle(pool)
            draws = pool[:quota]
        order.extend((index, row) for row in draws)
    if strategy == "weighted":
        positions = list(range(len(order)))
        _CounterRandom(f"{identity_sha256}:{epoch}:interleave".encode()).shuffle(positions)
        order = [order[position] for position in positions]
    return order


def consumed_mixture_rows(
    *,
    identity_sha256: str,
    epoch: int,
    cursor: int,
    sources: Sequence[tuple[uuid.UUID, Sequence[int], int]],
    strategy: Literal["weighted", "sequential"],
    replacement: bool,
) -> set[tuple[uuid.UUID, int]]:
    pools = [(len(rows), quota) for _, rows, quota in sources]
    epoch_samples = sum(quota for _, _, quota in sources)
    if (
        not 1 <= epoch_samples <= 16_000_000
        or not 0 <= cursor <= epoch_samples
        or not 0 <= epoch <= 6_400_000
        or epoch * epoch_samples + cursor > 409_600_000
    ):
        raise ValueError("Mixture replay boundary is invalid")
    consumed: set[tuple[uuid.UUID, int]] = set()
    maximum_rows = sum(len(rows) for _, rows, quota in sources if quota)
    for completed_epoch in range(epoch + 1):
        if len(consumed) == maximum_rows:
            break
        order = mixture_epoch_order(
            identity_sha256, completed_epoch, pools, strategy=strategy, replacement=replacement
        )
        count = cursor if completed_epoch == epoch else epoch_samples
        for source, position in order[:count]:
            identity, rows, _ = sources[source]
            consumed.add((identity, rows[position]))
    return consumed


def consumed_single_rows(
    state: SingleSourceSamplerState,
    *,
    seed: int,
    rows: Sequence[int],
) -> set[int]:
    """Reproduce legacy permutations and check the exact persisted RNG boundary."""
    import random

    if len(rows) != state.row_count or state.epoch * state.row_count + state.cursor > 409_600_000:
        raise ValueError("Single-source replay boundary exceeds bounded inputs")
    rng = random.Random(seed)
    permutation: list[int] = []
    for _ in range(state.epoch + 1):
        permutation = list(range(len(rows)))
        rng.shuffle(permutation)
    version, words, gaussian = rng.getstate()
    if (permutation, version, list(words), gaussian) != (
        state.permutation,
        state.rng_version,
        state.rng_state,
        state.gaussian_cache,
    ):
        raise ValueError("Single-source replay boundary differs from its seed")
    if state.epoch:
        return set(rows)
    return {rows[position] for position in permutation[: state.cursor]}
