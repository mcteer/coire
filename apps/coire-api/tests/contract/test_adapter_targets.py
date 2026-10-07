"""Exact selection and coalescing boundaries independent of MLX execution."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from functools import partial

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_api.gateway.loading import LoadCoordinator
from coire_api.gateway.targets import ModelNotFoundError, parse_selector, resolve_target
from coire_core.models.adapters import InferenceTarget


async def test_pair_stream_rewrites_split_frames_preserving_metadata_and_done() -> None:
    from coire_api.routes.v1 import _rewrite_selector

    selector = f"{uuid.uuid4()}@adapter"
    private = "/opt/coire/models/private"
    framed = (
        b"event: completion\r\ndata: "
        + json.dumps({"model": private, "choices": []}).encode()
        + b"\r\n\r\ndata: [DONE]\r\n\r\n"
    )
    closed = False

    async def source() -> AsyncIterator[bytes]:
        nonlocal closed
        try:
            for index in range(0, len(framed), 7):
                yield framed[index : index + 7]
        finally:
            closed = True

    frames = [
        frame async for frame in _rewrite_selector(source(), selector, private_model_path=private)
    ]
    assert closed and len(frames) == 2
    assert frames[0].startswith(b"event: completion\n")
    assert json.loads(frames[0].split(b"data: ", 1)[1])["model"] == selector
    assert private.encode() not in b"".join(frames)
    assert frames[1] == b"data: [DONE]\n\n"


@pytest.mark.parametrize("suffix", ["../x", "/tmp/x", "UPPER", "a@b", "", "a_", "a" * 64])
def test_pair_grammar_refuses_paths_and_nonregistry_names(suffix: str) -> None:
    with pytest.raises(ModelNotFoundError):
        parse_selector(f"{uuid.uuid4()}@{suffix}")


def test_selector_grammar_preserves_uuid_and_exact_slug() -> None:
    model = uuid.uuid4()
    assert parse_selector(model) == (model, None)
    assert parse_selector(str(model)) == (model, None)
    assert parse_selector(f"{model}@adapter-1") == (model, "adapter-1")


async def test_legacy_run_grant_refuses_pair_before_registry_io() -> None:
    model = uuid.uuid4()
    principal = Principal(kind=PrincipalKind.RUN, permitted_model_ids=frozenset({model}))
    with pytest.raises(ModelNotFoundError):
        await resolve_target(None, f"{model}@adapter", principal)  # type: ignore[arg-type]
    with pytest.raises(ModelNotFoundError):
        await resolve_target(None, model, principal, uuid.uuid4())  # type: ignore[arg-type]


async def test_single_flight_separates_base_two_adapters_and_exact_variant() -> None:
    model, variant = uuid.uuid4(), uuid.uuid4()
    base = InferenceTarget(model_id=model, variant_id=variant, base_manifest_sha256="a" * 64)
    targets = [
        base,
        *[
            base.model_copy(
                update={"adapter_id": uuid.uuid4(), "adapter_manifest_sha256": "b" * 64}
            )
            for _ in range(2)
        ],
        base.model_copy(update={"variant_id": uuid.uuid4()}),
    ]
    coordinator = LoadCoordinator()
    started: list[InferenceTarget] = []
    release = asyncio.Event()

    async def load(target: InferenceTarget) -> None:
        started.append(target)
        await release.wait()

    calls = [
        asyncio.create_task(coordinator.run(target, partial(load, target)))
        for target in targets
        for _ in range(3)
    ]
    for _ in range(5):
        await asyncio.sleep(0)
    assert set(started) == set(targets)
    assert len(started) == 4
    release.set()
    await asyncio.gather(*calls)
    assert not coordinator._loads
