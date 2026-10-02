"""Prompt encodings can be shared. Control encodings cannot cross owners."""

from __future__ import annotations

import pytest

from coire_node.image_runtime.cache import StageCache, StageCacheKey, stage_identity


def _key(stage: str, owner: str | None, *parts: str) -> StageCacheKey:
    return StageCacheKey(stage=stage, owner_id=owner, identity=stage_identity(*parts))


def test_prompt_cache_is_shared_and_control_cache_is_owner_private() -> None:
    cache = StageCache(max_bytes=32)
    prompt = _key("prompt", None, "mflux-0.20.0", "model", "prompt-a")
    cache.put(prompt, b"encoded")
    assert cache.get(prompt) == b"encoded"
    control = _key("control", "owner-a", "mflux-0.20.0", "model", "image-a")
    cache.put(control, b"edges!!")
    assert cache.get(control) == b"edges!!"
    other = _key("control", "owner-b", "mflux-0.20.0", "model", "image-a")
    assert cache.get(other) is None


def test_changed_prompt_or_adapter_misses_and_byte_limit_evicts() -> None:
    cache = StageCache(max_bytes=4)
    first = _key("prompt", None, "runtime", "base", "hello")
    cache.put(first, b"1234")
    changed = _key("prompt", None, "runtime", "base", "hello", "adapter-2")
    assert cache.get(changed) is None
    cache.put(changed, b"abcd")
    assert cache.get(first) is None
    assert cache.last_outcome == "evicted"
    assert cache.get(changed) == b"abcd"
    assert cache.last_outcome == "hit"
    with pytest.raises(ValueError):
        cache.put(changed, b"12345")


def test_control_key_requires_an_owner() -> None:
    with pytest.raises(ValueError):
        StageCacheKey(stage="control", identity="a" * 64, owner_id=None)
