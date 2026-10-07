"""Simulated node serving boundaries: stores, target isolation, rewrite and failover."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Request

from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineState, ReconcileExpectation, ReconcileRequest
from coire_core.models.failover import FailoverRelayRequest
from coire_core.models.gateway import ChatCompletionRequest, ChatMessage, EngineChatRequest
from coire_core.models.training_node import TrainingArtifactFile, TrainingArtifactManifest
from coire_node.engines import (
    BackendMismatch,
    CopyMissing,
    EngineManager,
    _Engine,
    build_engine_argv,
)
from coire_node.routes import engines as routes
from coire_node.routes import failover
from coire_node.testing.harness import Agent


def prepare(agent: Agent) -> tuple[str, InferenceTarget]:
    slug = "fixture--exact-base"
    directory = agent.store.path_for(slug)
    directory.mkdir(parents=True)
    (directory / "config.json").write_text("{}")
    base_manifest = agent.store.hash_tree(slug, repo_id="fixture/base", revision="offline")
    agent.store.write_manifest(base_manifest)
    target = InferenceTarget(
        model_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        adapter_id=uuid.uuid4(),
        base_manifest_sha256=base_manifest.sha256(),
        adapter_manifest_sha256="a" * 64,
    )
    artifact = (
        Path(agent.settings.node_state_dir) / "training" / "artifacts" / str(target.adapter_id)
    )
    artifact.mkdir(parents=True, mode=0o700)
    files: list[TrainingArtifactFile] = []
    for index, (name, data) in enumerate(
        [("adapters.safetensors", b"synthetic"), ("adapter_config.json", b"{}")]
    ):
        (artifact / name).write_bytes(data)
        files.append(
            TrainingArtifactFile(
                id=f"file-{index}",
                name=name,
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    assert target.adapter_id is not None
    manifest = TrainingArtifactManifest(
        artifact_id=target.adapter_id,
        kind="adapter",
        files=files,
        total_bytes=sum(item.bytes for item in files),
    )
    (artifact / "manifest.json").write_text(manifest.model_dump_json())
    return slug, target.model_copy(update={"adapter_manifest_sha256": manifest.canonical_sha256()})


def test_registry_store_adapter_path_and_corruption_refusal(tmp_path: Path) -> None:
    agent = Agent(tmp_path)
    slug, target = prepare(agent)
    path = agent.engines.adapter_path(target, slug)
    assert path and path.name == str(target.adapter_id)
    argv = build_engine_argv(
        command=["python", "-m", "mlx_lm.server"],
        model_path=str(agent.store.path_for(slug)),
        host="127.0.0.1",
        port=9500,
        adapter_path=str(path),
    )
    assert argv[argv.index("--adapter-path") + 1] == str(path)
    (path / "adapters.safetensors").write_bytes(b"different")
    with pytest.raises(CopyMissing):
        agent.engines.adapter_path(target, slug)
    with pytest.raises(CopyMissing):
        agent.engines.adapter_path(
            target.model_copy(update={"base_manifest_sha256": "0" * 64}), slug
        )


def test_dedup_and_persistence_separate_same_base_slug(tmp_path: Path) -> None:
    agent = Agent(tmp_path)
    slug, pair = prepare(agent)
    base = pair.model_copy(update={"adapter_id": None, "adapter_manifest_sha256": None})
    other = pair.model_copy(update={"adapter_id": uuid.uuid4()})
    for target in [base, pair, other]:
        identity = uuid.uuid4()
        agent.engines._engines[str(identity)] = _Engine(
            engine_id=identity, slug=slug, port=9500, estimate_bytes=1024, target=target
        )
    assert agent.engines._serving(slug) is None  # Legacy base lookup never adopts a pair.
    pair_engine = agent.engines._serving(slug, pair)
    base_engine = agent.engines._serving(slug, base)
    assert pair_engine is not None and pair_engine.target == pair
    assert base_engine is not None and base_engine.target == base
    agent.engines._persist()
    records = json.loads((Path(agent.settings.node_state_dir) / "engines.json").read_text())
    assert {InferenceTarget.model_validate(record["target"]) for record in records} == {
        base,
        pair,
        other,
    }
    pair_engine = agent.engines._serving(slug, pair)
    assert pair_engine is not None and pair_engine.engine_id is not None
    with pytest.raises(BackendMismatch):
        agent.engines.start(
            engine_id=pair_engine.engine_id, slug=slug, estimate_bytes=1, target=base
        )


async def test_proxy_rewrites_exact_adapter_and_never_caller_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path)
    slug, target = prepare(agent)
    identity = uuid.uuid4()
    agent.engines._engines[str(identity)] = _Engine(
        engine_id=identity,
        slug=slug,
        target=target,
        port=9500,
        estimate_bytes=1,
        state=EngineState.READY,
    )
    seen: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(routes, "_engine_client", lambda: client)
        await routes.proxy_chat_completion(
            identity,
            EngineChatRequest(model=slug, messages=[ChatMessage(role="user", content="hi")]),
            agent.engines,
            agent.store,
            Request({"type": "http", "headers": []}),
        )
        assert seen[0]["model"] == str(agent.store.path_for(slug))
        assert seen[0]["adapters"] == str(agent.engines.adapter_path(target, slug))
        with pytest.raises(HTTPException) as refusal:
            await routes.proxy_chat_completion(
                identity,
                EngineChatRequest(
                    model="/caller/path", messages=[ChatMessage(role="user", content="hi")]
                ),
                agent.engines,
                agent.store,
                Request({"type": "http", "headers": []}),
            )
        assert refusal.value.status_code == 400 and len(seen) == 1


async def test_failover_rejects_adapter_engine_even_base_slug_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path)
    slug, target = prepare(agent)
    slug = "base-public"
    identity = uuid.uuid4()
    agent.engines._engines[str(identity)] = _Engine(
        engine_id=identity,
        slug=slug,
        target=target,
        port=9500,
        estimate_bytes=1,
        state=EngineState.READY,
    )
    monkeypatch.setattr(failover, "_authorize_relay", AsyncMock())
    body = FailoverRelayRequest(
        engine_id=identity,
        model_slug=slug,
        request=ChatCompletionRequest(
            model=target.model_id, messages=[ChatMessage(role="user", content="hi")]
        ),
    )
    with pytest.raises(HTTPException) as refusal:
        await failover.relay_completion(identity, body, agent.engines, agent.store, agent.settings)
    assert refusal.value.status_code == 503


def test_reconcile_never_adopts_same_pid_with_different_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path)
    slug, pair = prepare(agent)
    identity = uuid.uuid4()
    agent.engines._engines[str(identity)] = _Engine(
        engine_id=identity, slug=slug, target=pair, port=9500, estimate_bytes=1, pid=123
    )
    monkeypatch.setattr("coire_node.engines._alive", lambda *args, **kwargs: True)
    monkeypatch.setattr(agent.engines, "find_orphans", lambda: [])
    result = agent.engines.reconcile(
        ReconcileRequest(
            expected=[
                ReconcileExpectation(
                    engine_id=identity,
                    slug=slug,
                    port=9500,
                    target=pair.model_copy(update={"adapter_id": uuid.uuid4()}),
                )
            ]
        )
    )
    assert result.adopted == []
    assert result.dead == []
    assert [item.engine_id for item in result.orphans] == [identity]


@pytest.mark.parametrize("record_pair,argv_pair", [(True, True), (True, False), (False, True)])
def test_restart_adoption_binds_adapter_argv_and_rechecks_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_pair: bool,
    argv_pair: bool,
) -> None:
    agent = Agent(tmp_path)
    slug, pair = prepare(agent)
    identity = uuid.uuid4()
    agent.engines._engines[str(identity)] = _Engine(
        engine_id=identity,
        slug=slug,
        target=pair if record_pair else None,
        port=9500,
        estimate_bytes=1,
        pid=123,
        create_time=1.0,
        state=EngineState.READY,
    )
    agent.engines._persist()
    argv = ["python", "-m", "mlx_lm.server", "--model", str(agent.store.path_for(slug))]
    if argv_pair:
        argv += ["--adapter-path", str(agent.engines.adapter_path(pair, slug))]

    class Process:
        def cmdline(self) -> list[str]:
            return argv

    manager = EngineManager(agent.settings, agent.store, "127.0.0.1")
    monkeypatch.setattr("coire_node.engines._alive", lambda *args, **kwargs: True)
    monkeypatch.setattr("coire_node.engines.psutil.Process", lambda pid: Process())
    monkeypatch.setattr(manager, "_sample", lambda engine: None)
    monkeypatch.setattr(manager, "_probe_until_ready", lambda engine: None)
    adopted = manager.adopt_from_state()
    if record_pair and argv_pair:
        assert len(adopted) == 1 and adopted[0].target == pair
        assert adopted[0].state is EngineState.STARTING
    else:
        assert adopted == []
