"""Evaluation engine identity uses spawn provenance, never fills legacy gaps from current code."""

import uuid
from pathlib import Path

import pytest

from coire_core.models.adapters import InferenceTarget
from coire_core.models.engine import EngineState
from coire_core.models.registry import EngineBackend
from coire_core.settings import Settings
from coire_node.engines import EngineManager, _Engine
from coire_node.store import Store


def test_engine_attestation_requires_live_exact_owned_spawn_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = EngineManager(
        Settings(node_state_dir=str(tmp_path)), Store(tmp_path / "models"), "127.0.0.1"
    )
    target = InferenceTarget(
        model_id=uuid.uuid4(), variant_id=uuid.uuid4(), base_manifest_sha256="a" * 64
    )
    engine_id = uuid.uuid4()
    engine = _Engine(
        engine_id=engine_id,
        slug="fixture",
        port=9000,
        estimate_bytes=1,
        state=EngineState.READY,
        target=target,
    )
    manager._engines[str(engine_id)] = engine
    monkeypatch.setattr("coire_node.engines._alive", lambda *args, **kwargs: True)
    assert manager.attested_engine_version(engine_id, target) is None
    engine.engine_version = "synthetic-spawn-version"
    assert manager.attested_engine_version(engine_id, target) == "synthetic-spawn-version"
    assert manager.attested_engine_version(engine_id, target, backend=EngineBackend.MLX_VLM) is None
    assert engine.record()["engine_version"] == "synthetic-spawn-version"
    assert manager.attested_engine_version(engine_id, target, "foreign-template") is None
    assert (
        manager.attested_engine_version(
            engine_id, target.model_copy(update={"variant_id": uuid.uuid4()})
        )
        is None
    )
    engine.state = EngineState.STOPPED
    assert manager.attested_engine_version(engine_id, target) is None
    engine.state = EngineState.READY
    monkeypatch.setattr("coire_node.engines._alive", lambda *args, **kwargs: False)
    assert manager.attested_engine_version(engine_id, target) is None


@pytest.mark.parametrize(
    ("backend", "package"),
    [(EngineBackend.MLX_LM, "mlx-lm"), (EngineBackend.MLX_VLM, "mlx-vlm")],
)
def test_tokenizer_identity_reports_declared_bare_engine_package(
    monkeypatch: pytest.MonkeyPatch, backend: EngineBackend, package: str
) -> None:
    from coire_node import evaluation_tokenizer_worker

    monkeypatch.setattr(evaluation_tokenizer_worker, "version", lambda name: f"installed-{name}")
    assert evaluation_tokenizer_worker.engine_runtime_version(backend) == f"installed-{package}"
