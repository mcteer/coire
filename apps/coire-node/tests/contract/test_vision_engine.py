"""Bare VLM node lifecycle without starting Metal or a real Studio engine."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from pydantic import ValidationError

from coire_core.models.engine import EngineStartRequest, EngineState
from coire_core.models.registry import EngineBackend
from coire_node import engines as engines_module
from coire_node.engines import (
    BackendMismatch,
    BudgetExceeded,
    CopyMissing,
    _Engine,
    build_engine_env,
    build_vision_argv,
)
from coire_node.testing.harness import TOKEN, Agent


@pytest.mark.asyncio
async def test_visual_checksum_preflight_keeps_authenticated_health_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path / "node")
    slug = "fake--vision"
    _seed(agent, slug)
    (agent.store.path_for(slug) / "model.safetensors").write_bytes(b"corrupt")
    entered, release = threading.Event(), threading.Event()
    verify = agent.store.verify_against

    def slow_verify(*args: Any, **kwargs: Any) -> list[str]:
        entered.set()
        assert release.wait(5)
        return verify(*args, **kwargs)

    monkeypatch.setattr(agent.store, "verify_against", slow_verify)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=agent.app()),
            base_url="http://node",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            pending = asyncio.create_task(
                client.post(
                    "/node/engines",
                    json={
                        "engine_id": str(uuid.uuid4()),
                        "slug": slug,
                        "estimate_bytes": 1024,
                        "backend": "mlx_vlm",
                    },
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                health = await asyncio.wait_for(client.get("/node/health"), 1)
                assert health.status_code == 200
            finally:
                release.set()
                response = await pending
            assert response.status_code == 404
            assert agent.engines.statuses() == []
    finally:
        release.set()
        agent.close()


def _seed(agent: Agent, slug: str) -> None:
    base = agent.store.path_for(slug)
    base.mkdir(parents=True, exist_ok=True)
    (base / "config.json").write_text(
        json.dumps({"architectures": ["Idefics3ForConditionalGeneration"]})
    )
    for name in ("processor_config.json", "preprocessor_config.json", "tokenizer_config.json"):
        (base / name).write_text("{}")
    (base / "tokenizer.json").write_text("{}")
    (base / "model.safetensors").write_bytes(b"\0" * 2048)
    agent.store.write_manifest(agent.store.hash_tree(slug, repo_id="fake/vision", revision="r"))


def test_vision_argv_is_fixed_local_offline_and_bounded() -> None:
    argv = build_vision_argv(
        model_path="/opt/coire/models/verified-vlm",
        host="127.0.0.1",
        port=9500,
        vision_cache_size=2,
        max_num_seqs=1,
        max_kv_size=2048,
    )
    assert argv == [
        argv[0],
        "-m",
        "mlx_vlm.server",
        "--model",
        "/opt/coire/models/verified-vlm",
        "--host",
        "127.0.0.1",
        "--port",
        "9500",
        "--vision-cache-size",
        "2",
        "--max-num-seqs",
        "1",
        "--log-level",
        "INFO",
        "--max-kv-size",
        "2048",
    ]
    assert "--trust-remote-code" not in argv
    env = build_engine_env(
        {
            "HF_TOKEN": "secret",
            "HF_API_TOKEN": "secret",
            "HUGGING_FACE_HUB_TOKEN": "secret",
            "MLX_TRUST_REMOTE_CODE": "true",
        }
    )
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    for key in ("HF_TOKEN", "HF_API_TOKEN", "HUGGING_FACE_HUB_TOKEN", "MLX_TRUST_REMOTE_CODE"):
        assert key not in env


def test_vision_process_records_backend_and_rejects_wrong_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(tmp_path / "node")
    slug = "fake--vision"
    _seed(agent, slug)
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 9999999

    def popen(argv: list[str], **kwargs: object) -> FakeProcess:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr("coire_node.engines.subprocess.Popen", popen)
    monkeypatch.setattr("coire_node.engines.version", lambda package: f"installed-{package}")
    monkeypatch.setattr(agent.engines, "_await_ready", lambda _key: None)
    try:
        engine_id = uuid.uuid4()
        existing, status = agent.engines.start(
            engine_id=engine_id,
            slug=slug,
            estimate_bytes=1024,
            backend=EngineBackend.MLX_VLM,
            vision_cache_size=2,
            max_num_seqs=1,
        )
        assert not existing
        assert status.backend is EngineBackend.MLX_VLM
        assert (
            agent.engines._engines[str(engine_id)].record()["engine_version"] == "installed-mlx-vlm"
        )
        assert captured["argv"][2] == "mlx_vlm.server"
        assert captured["argv"][4] == str(agent.store.path_for(slug))
        assert captured["env"]["HF_HUB_OFFLINE"] == "1"
        with pytest.raises(BackendMismatch):
            agent.engines.start(
                engine_id=uuid.uuid4(),
                slug=slug,
                estimate_bytes=1024,
                backend=EngineBackend.MLX_LM,
            )
        assert agent.engines._state_file.read_text().find('"backend": "mlx_vlm"') >= 0
    finally:
        agent.close()


def test_vision_backend_survives_agent_re_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(tmp_path / "node")
    slug = "fake--vision"
    _seed(agent, slug)
    engine_id = uuid.uuid4()
    agent.engines._state_file.write_text(
        json.dumps(
            [
                {
                    "engine_id": str(engine_id),
                    "slug": slug,
                    "port": 9500,
                    "pid": 9999999,
                    "create_time": 1.0,
                    "estimate_bytes": 2048,
                    "backend": "mlx_vlm",
                    "started_at": datetime.now(UTC).isoformat(),
                }
            ]
        )
    )
    monkeypatch.setattr("coire_node.engines._alive", lambda *_args, **_kwargs: True)

    class OwnedProcess:
        def __init__(self, pid: int) -> None:
            assert pid == 9999999

        def cmdline(self) -> list[str]:
            return [
                "python",
                "-m",
                "mlx_vlm.server",
                "--model",
                str(agent.store.path_for(slug)),
                "--port",
                "9500",
            ]

    monkeypatch.setattr("coire_node.engines.psutil.Process", OwnedProcess)
    monkeypatch.setattr(agent.engines, "_sample", lambda _engine: None)
    try:
        adopted = agent.engines.adopt_from_state()
        assert len(adopted) == 1
        assert adopted[0].backend is EngineBackend.MLX_VLM
        status = agent.engines.get(engine_id)
        assert status is not None and status.backend is EngineBackend.MLX_VLM
    finally:
        agent.close()


@pytest.mark.parametrize("alive", [True, False])
def test_unowned_vision_process_is_reported_as_vision_orphan(
    alive: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(tmp_path / "node")
    slug = "fake--vision"
    _seed(agent, slug)

    class FakePsutilProcess:
        pid = 9999999

        def create_time(self) -> float:
            return 1.0

        def cmdline(self) -> list[str]:
            return [
                "python",
                "-m",
                "mlx_vlm.server",
                "--model",
                str(agent.store.path_for(slug)),
                "--port",
                "9500",
            ]

    monkeypatch.setattr("coire_node.engines.psutil.process_iter", lambda: [FakePsutilProcess()])
    monkeypatch.setattr(agent.engines, "_sample", lambda _engine: None)
    monkeypatch.setattr("coire_node.engines._alive", lambda *args, **kwargs: alive)
    try:
        orphans = agent.engines.find_orphans()
        assert len(orphans) == int(alive)
        if alive:
            assert orphans[0].backend is EngineBackend.MLX_VLM
    finally:
        agent.close()


def test_vision_readiness_generates_with_verified_local_model_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(engines_module, "_tracer", provider.get_tracer("test.vision"))
    agent = Agent(tmp_path / "node")
    slug = "fake--vision"
    _seed(agent, slug)
    engine_id = uuid.uuid4()
    engine = _Engine(
        engine_id=engine_id,
        slug=slug,
        port=9500,
        estimate_bytes=2048,
        backend=EngineBackend.MLX_VLM,
    )
    agent.engines._engines[str(engine_id)] = engine
    posts: list[dict[str, object]] = []

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str) -> object:
            return object()

        def post(self, _url: str, *, json: dict[str, object], timeout: float) -> object:
            assert timeout == 30.0
            posts.append(json)
            return type("Response", (), {"status_code": 200})()

    monkeypatch.setattr("coire_node.engines.httpx.Client", Client)
    monkeypatch.setattr(agent.engines, "_sample", lambda _engine: None)
    try:
        agent.engines._await_ready(str(engine_id))
        assert engine.state is EngineState.READY
        span = exporter.get_finished_spans()[0]
        assert span.name == "coire.node.vision.load"
        assert span.attributes == {"engine_id": str(engine_id), "backend": "mlx_vlm"}
        assert span.events == ()
        assert posts == [
            {
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "temperature": 0.0,
                "model": str(agent.store.path_for(slug)),
            }
        ]
    finally:
        agent.close()


def test_visual_start_refuses_corrupt_or_linked_copy_before_process_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path / "node")
    slug = "fake--vision"
    _seed(agent, slug)
    spawned: list[object] = []
    monkeypatch.setattr("coire_node.engines.subprocess.Popen", lambda *_a, **_k: spawned.append(1))
    try:
        (agent.store.path_for(slug) / "model.safetensors").write_bytes(b"corrupt")
        with pytest.raises(CopyMissing):
            agent.engines.start(
                engine_id=uuid.uuid4(),
                slug=slug,
                estimate_bytes=1024,
                backend=EngineBackend.MLX_VLM,
            )
        assert not spawned
        _seed_replacement = agent.store.path_for(slug) / "model.safetensors"
        _seed_replacement.write_bytes(b"\0" * 2048)
        linked = agent.store.path_for(slug) / "processor_config.json"
        linked.unlink()
        linked.symlink_to(tmp_path / "outside")
        with pytest.raises(CopyMissing):
            agent.engines.start(
                engine_id=uuid.uuid4(),
                slug=slug,
                estimate_bytes=1024,
                backend=EngineBackend.MLX_VLM,
            )
        assert not spawned
    finally:
        agent.close()


def test_visual_start_refuses_excess_reservation_and_bounds_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path / "node")
    slug = "fake--vision"
    _seed(agent, slug)
    spawned: list[object] = []
    monkeypatch.setattr("coire_node.engines.subprocess.Popen", lambda *_a, **_k: spawned.append(1))
    try:
        with pytest.raises(BudgetExceeded):
            agent.engines.start(
                engine_id=uuid.uuid4(),
                slug=slug,
                estimate_bytes=10**15,
                backend=EngineBackend.MLX_VLM,
            )
        assert not spawned
        command = {
            "engine_id": str(uuid.uuid4()),
            "slug": slug,
            "estimate_bytes": 1024,
            "backend": "mlx_vlm",
        }
        for field, value in (("vision_cache_size", 0), ("max_num_seqs", 17), ("max_kv_size", 0)):
            with pytest.raises(ValidationError):
                EngineStartRequest.model_validate({**command, field: value})
    finally:
        agent.close()


def test_visual_cancel_keeps_backend_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path / "node")
    engine_id = uuid.uuid4()
    engine = _Engine(
        engine_id=engine_id,
        slug="fake--vision",
        port=9500,
        estimate_bytes=2048,
        backend=EngineBackend.MLX_VLM,
    )
    agent.engines._engines[str(engine_id)] = engine
    stopped = threading.Event()
    monkeypatch.setattr(agent.engines, "_terminate", lambda _engine: stopped.set())
    try:
        status = agent.engines.stop(engine_id)
        assert status is not None and status.backend is EngineBackend.MLX_VLM
        assert status.state is EngineState.STOPPING
        assert stopped.wait(2)
    finally:
        agent.close()
