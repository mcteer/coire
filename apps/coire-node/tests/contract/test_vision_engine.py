"""Bare VLM node lifecycle without starting Metal or a real Studio engine."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from coire_core.models.registry import EngineBackend
from coire_node.engines import BackendMismatch, build_engine_env, build_vision_argv
from coire_node.testing.harness import Agent


def _seed(agent: Agent, slug: str) -> None:
    base = agent.store.path_for(slug)
    base.mkdir(parents=True, exist_ok=True)
    (base / "config.json").write_bytes(b"{}")
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
    env = build_engine_env({"HF_TOKEN": "secret", "MLX_TRUST_REMOTE_CODE": "true"})
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    assert "HF_TOKEN" not in env and "MLX_TRUST_REMOTE_CODE" not in env


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
    monkeypatch.setattr(agent.engines, "_sample", lambda _engine: None)
    try:
        adopted = agent.engines.adopt_from_state()
        assert len(adopted) == 1
        assert adopted[0].backend is EngineBackend.MLX_VLM
        status = agent.engines.get(engine_id)
        assert status is not None and status.backend is EngineBackend.MLX_VLM
    finally:
        agent.close()


def test_unowned_vision_process_is_reported_as_vision_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(tmp_path / "node")
    slug = "fake--vision"
    _seed(agent, slug)

    class FakePsutilProcess:
        def __init__(self) -> None:
            self.info = {
                "pid": 9999999,
                "create_time": 1.0,
                "cmdline": [
                    "python",
                    "-m",
                    "mlx_vlm.server",
                    "--model",
                    str(agent.store.path_for(slug)),
                    "--port",
                    "9500",
                ],
            }

    monkeypatch.setattr(
        "coire_node.engines.psutil.process_iter", lambda _fields: [FakePsutilProcess()]
    )
    monkeypatch.setattr(agent.engines, "_sample", lambda _engine: None)
    try:
        orphans = agent.engines.find_orphans()
        assert len(orphans) == 1
        assert orphans[0].backend is EngineBackend.MLX_VLM
    finally:
        agent.close()
