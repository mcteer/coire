"""Bounded prompt and control stage caches. Source images stay owner-private."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass

from coire_node.metrics import record_image_cache


@dataclass(frozen=True, slots=True)
class StageCacheKey:
    """Identity for one encoded stage. Shared prompt entries have no owner."""

    stage: str
    identity: str
    owner_id: str | None = None

    def __post_init__(self) -> None:
        if self.stage not in {"prompt", "control"}:
            raise ValueError("unknown image cache stage")
        if len(self.identity) != 64:
            raise ValueError("image cache identity must be a sha256")
        if self.stage == "control" and not self.owner_id:
            raise ValueError("control cache entries are owner-private")
        if self.stage == "prompt" and self.owner_id is not None:
            raise ValueError("prompt cache entries are shared")


def stage_identity(*parts: str) -> str:
    """Hash runtime, model, transform and input identities. Callers omit prompt text labels."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


class StageCache:
    """Byte-capped LRU. A control entry is invisible to every other owner."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("image cache limit must be nonnegative")
        self.max_bytes = max_bytes
        self._items: OrderedDict[StageCacheKey, bytes] = OrderedDict()
        self.last_outcome: str | None = None
        self._evicted: OrderedDict[StageCacheKey, None] = OrderedDict()

    def occupancy(self, stage: str) -> int:
        return sum(len(payload) for key, payload in self._items.items() if key.stage == stage)

    def get(self, key: StageCacheKey) -> bytes | None:
        payload = self._items.get(key)
        if payload is None:
            self.last_outcome = "evicted" if key in self._evicted else "cold"
            record_image_cache(key.stage, "miss", self.occupancy(key.stage))
            return None
        self.last_outcome = "hit"
        self._items.move_to_end(key)
        record_image_cache(key.stage, "hit", self.occupancy(key.stage))
        return payload

    def put(self, key: StageCacheKey, payload: bytes) -> None:
        if not payload or len(payload) > self.max_bytes:
            raise ValueError("image cache payload exceeds its byte limit")
        self._items[key] = payload
        self._evicted.pop(key, None)
        self._items.move_to_end(key)
        while sum(len(item) for item in self._items.values()) > self.max_bytes:
            evicted, _ = self._items.popitem(last=False)
            self._evicted[evicted] = None
            if len(self._evicted) > 256:
                self._evicted.popitem(last=False)
            record_image_cache(evicted.stage, "eviction", self.occupancy(evicted.stage))
        record_image_cache(key.stage, "store", self.occupancy(key.stage))


class NativeStageCache:
    """Byte-bound resident values for evaluated native encoder results."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("image cache limit must be nonnegative")
        self.max_bytes = max_bytes
        self._items: OrderedDict[StageCacheKey, tuple[object, int]] = OrderedDict()
        self.used_bytes = 0
        self.last_outcome: str | None = None
        self._evicted: OrderedDict[StageCacheKey, None] = OrderedDict()

    def get(self, key: StageCacheKey) -> object | None:
        item = self._items.get(key)
        if item is None:
            self.last_outcome = "evicted" if key in self._evicted else "cold"
            record_image_cache(key.stage, "miss", self.used_bytes)
            return None
        self.last_outcome = "hit"
        self._items.move_to_end(key)
        record_image_cache(key.stage, "hit", self.used_bytes)
        return item[0]

    def put(self, key: StageCacheKey, value: object, size: int) -> None:
        if size < 1 or size > self.max_bytes:
            raise ValueError("native image cache value exceeds its byte limit")
        previous = self._items.pop(key, None)
        if previous is not None:
            self.used_bytes -= previous[1]
        while self._items and self.used_bytes + size > self.max_bytes:
            old_key, (_, old_size) = self._items.popitem(last=False)
            self.used_bytes -= old_size
            self._evicted[old_key] = None
            if len(self._evicted) > 256:
                self._evicted.popitem(last=False)
            record_image_cache(old_key.stage, "eviction", self.used_bytes)
        self._items[key] = (value, size)
        self._evicted.pop(key, None)
        self.used_bytes += size
        record_image_cache(key.stage, "store", self.used_bytes)
