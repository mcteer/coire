"""Real synthetic CPU safetensors: extraction integrity, serving metadata and crash edges."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi import FastAPI, Header, HTTPException
from safetensors.numpy import save_file

from coire_core.errors import TrainingConflict
from coire_core.models.acquisition import ReservationState
from coire_core.models.adapters import InferenceTarget
from coire_core.models.datasets import TokenizedTrainingExample
from coire_core.models.training import ResolvedTrainingSpec
from coire_core.models.training_node import CheckpointWorkerState, TrainingAdapterExtractRequest
from coire_core.settings import Settings
from coire_node.engines import EngineManager
from coire_node.reservations import ReservationLedger
from coire_node.store import Store
from coire_node.training.artifacts import TrainingArtifacts
from coire_node.training.checkpoints import CheckpointStore
from coire_node.training.extraction import AdapterExtractor, canonical
from coire_node.training.sampler import SingleSourceSampler

JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


class CPUWriter:
    def is_tensor(self, value: object) -> bool:
        return isinstance(value, np.ndarray)

    def shape(self, value: Any) -> list[int]:
        return list(value.shape)

    def dtype(self, value: Any) -> str:
        return str(value.dtype)

    def save(self, path: Path, values: dict[str, Any]) -> None:
        save_file(values, str(path))

    def load(self, path: Path) -> dict[str, Any]:
        raise AssertionError("Extraction must not allocate tensors")


def fixture(
    tmp_path: Path, kind: str = "lora", *, invalid: str = ""
) -> tuple[AdapterExtractor, TrainingAdapterExtractRequest]:
    tmp_path = tmp_path.resolve()
    settings = Settings(node_state_dir=str(tmp_path / "state")).model_copy(
        update={"training_artifact_disk_floor_bytes": 0}
    )
    store = Store(tmp_path / "models")
    store.ensure_root()
    base = store.path_for("synthetic--extract")
    base.mkdir()
    config: dict[str, Any] = {
        "model_type": "llama",
        "num_hidden_layers": 2,
        "hidden_size": 64,
        "intermediate_size": 128,
        "num_attention_heads": 4,
        "num_key_value_heads": 2,
    }
    weight = np.zeros((64, 64), dtype=np.float16)
    if kind == "qlora":
        config["quantization"] = {"bits": 4, "group_size": 64}
        weight = np.zeros((64, 8), dtype=np.uint32)
    if invalid == "executable":
        config["model_file"] = "must_never_import.py"
    if invalid == "architecture":
        config["model_type"] = "unapproved"
    if invalid == "dimensions":
        config["num_attention_heads"] = 0
    if invalid == "quantization":
        config["quantization"] = {"bits": 8, "group_size": 64}
    weights = {"model.layers.1.self_attn.q_proj.weight": weight}
    if kind == "qlora":
        weights["model.layers.1.self_attn.q_proj.scales"] = np.ones((64, 1), dtype=np.float16)
        if invalid != "missing_biases":
            weights["model.layers.1.self_attn.q_proj.biases"] = np.zeros((64, 1), dtype=np.float16)
    (base / "config.json").write_text(json.dumps(config))
    save_file(weights, str(base / "model.safetensors"))
    manifest = store.hash_tree(base.name, repo_id="synthetic/base", revision="offline")
    store.write_manifest(manifest)
    model, variant, dataset = [uuid.uuid4() for _ in range(3)]
    resolved = ResolvedTrainingSpec.model_validate(
        {
            "spec": {
                "model": {"model_id": model, "variant_id": variant},
                "data": {
                    "train": {
                        "datasets": [
                            {"dataset_id": dataset, "sample_count": 4, "mixture_proportion": 1.0}
                        ],
                        "epoch_samples": 4,
                    },
                    "validation": {"dataset_ids": [dataset]},
                },
                "parameterization": {
                    "kind": kind,
                    "rank": 2,
                    "target_modules": ["self_attn.q_proj"],
                    "scale": 3.0,
                    "dropout": 0.1,
                },
                "optim": {"updates": 4},
                "output": {"adapter_slug": "synthetic-extract"},
            },
            "base_manifest_sha256": manifest.sha256(),
            "datasets": [
                {
                    "dataset_id": dataset,
                    "analysis_id": uuid.uuid4(),
                    "source_sha256": "a" * 64,
                    "split_sha256": "b" * 64,
                    "analysis_sha256": "c" * 64,
                }
            ],
            "tokenizer_sha256": "d" * 64,
            "template_sha256": "e" * 64,
            "enable_thinking": False,
            "runtime_sha256": "f" * 64,
            "worker_version": "1",
            "resource_envelope": {
                "weight_bytes": 10,
                "adapter_bytes": 2,
                "optimizer_bytes": 4,
                "activation_bytes": 8,
                "buffer_bytes": 2,
                "safety_bytes": 10,
                "checkpoint_bytes": 100,
                "evidence_sha256": "c" * 64,
            },
        }
    )
    artifacts = TrainingArtifacts(
        Path(settings.node_state_dir) / "training" / "artifacts", node_name="coire-edge-a"
    )
    sampler = SingleSourceSampler(
        [
            TokenizedTrainingExample(
                source_row=i + 1,
                content_sha256="a" * 64,
                tokens=[1, 2, 3],
                target_mask=[False, True, True],
                target_start=1,
            )
            for i in range(4)
        ],
        dataset_sha256="a" * 64,
        batch_size=1,
        seed=0,
        max_sequence_length=8,
    )
    state = CheckpointWorkerState(
        job_id=JOB,
        attempt_id=JOB,
        fence=1,
        completed_update=3,
        rank=0,
        world_size=1,
        runtime_sha256=resolved.runtime_sha256,
        resolved_spec_sha256=hashlib.sha256(
            canonical(resolved.model_dump(mode="json"))
        ).hexdigest(),
        optimizer=resolved.spec.optim,
        mlx_rng_key=(1, 2),
        sampler=sampler.snapshot(),
    )
    adapter = {
        "model.layers.1.self_attn.q_proj.lora_a": np.ones((64, 2), dtype=np.float32),
        "model.layers.1.self_attn.q_proj.lora_b": np.ones((2, 64), dtype=np.float32),
    }
    if kind == "dora":
        adapter["model.layers.1.self_attn.q_proj.m"] = np.ones((64,), dtype=np.float32)
    if invalid == "adapter_shape":
        adapter["model.layers.1.self_attn.q_proj.lora_a"] = np.ones((32, 2), dtype=np.float32)
    if invalid == "adapter_dtype":
        adapter["model.layers.1.self_attn.q_proj.lora_a"] = np.ones((64, 2), dtype=np.int32)
    if invalid == "adapter_extra":
        adapter["model.layers.1.self_attn.q_proj.linear.weight"] = np.ones(
            (64, 64), dtype=np.float32
        )
    if invalid == "missing_magnitude":
        adapter.pop("model.layers.1.self_attn.q_proj.m")
    checkpoint = CheckpointStore(artifacts.root, tensor_io=CPUWriter(), disk_floor_bytes=0).save(
        state, adapter, {"step": np.array(3, dtype=np.uint32)}
    )
    ledger = ReservationLedger(settings, store, lambda: 0)
    extractor = AdapterExtractor(artifacts, ledger, settings, store)
    command = TrainingAdapterExtractRequest(
        command_id=uuid.uuid4(),
        adapter_id=uuid.uuid4(),
        checkpoint_id=checkpoint.artifact_id,
        checkpoint_manifest_sha256=checkpoint.canonical_sha256(),
        job_id=JOB,
        attempt_id=JOB,
        fence=1,
        node="coire-edge-a",
        resolved=resolved,
        disk_reservation_id=uuid.uuid4(),
        max_bytes=1024**2,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    return extractor, command


@pytest.mark.parametrize("kind", ["lora", "qlora", "dora"])
def test_native_served_format_and_exact_engine_store(tmp_path: Path, kind: str) -> None:
    extractor, command = fixture(tmp_path, kind)
    result = extractor.extract(command)
    assert result.state == "succeeded", result
    assert result.manifest is not None
    directory = extractor.artifacts.directory(command.adapter_id)
    config = json.loads((directory / "adapter_config.json").read_bytes())
    assert config["fine_tune_type"] == ("dora" if kind == "dora" else "lora")
    assert config["num_layers"] == 1
    assert config["lora_parameters"] == {
        "rank": 2,
        "scale": 3.0,
        "dropout": 0.1,
        "keys": ["self_attn.q_proj"],
    }
    assert config["coire_base_manifest_sha256"] == command.resolved.base_manifest_sha256
    checkpoint = extractor.artifacts.manifest(command.checkpoint_id)
    source = extractor.artifacts.file(checkpoint, checkpoint.ranks[0].adapter_file_id)
    assert source.read_bytes() == (directory / "adapters.safetensors").read_bytes()
    assert {path.name for path in directory.iterdir()} == {
        "adapters.safetensors",
        "adapter_config.json",
        "manifest.json",
    }
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())
    engine = EngineManager(extractor.settings, extractor.store, "127.0.0.1")
    target = InferenceTarget(
        model_id=command.resolved.spec.model.model_id,
        variant_id=command.resolved.spec.model.variant_id,
        adapter_id=command.adapter_id,
        base_manifest_sha256=command.resolved.base_manifest_sha256,
        adapter_manifest_sha256=result.manifest.canonical_sha256(),
    )
    assert engine.adapter_path(target, "synthetic--extract") == directory
    assert extractor.extract(command) == result
    assert extractor.reservations.held_disk_bytes(extractor.artifacts.root) == 0
    restarted = AdapterExtractor(
        extractor.artifacts, extractor.reservations, extractor.settings, extractor.store
    )
    assert restarted.extract(command) == result
    with pytest.raises(TrainingConflict):
        restarted.extract(command.model_copy(update={"max_bytes": 1024**2 + 1}))
    with pytest.raises(TrainingConflict):
        restarted.extract(
            command.model_copy(
                update={"command_id": uuid.uuid4(), "disk_reservation_id": uuid.uuid4()}
            )
        )


@pytest.mark.parametrize(
    "failure",
    [
        "corrupt_adapter",
        "corrupt_optimizer",
        "wrong_base",
        "wrong_shape",
        "wrong_dtype",
        "missing_key",
        "unsafe_config",
        "deadline",
        "quota",
    ],
)
def test_corruption_incompatibility_deadline_quota_fail_before_publication(
    tmp_path: Path, failure: str
) -> None:
    extractor, command = fixture(tmp_path)
    checkpoint = extractor.artifacts.manifest(command.checkpoint_id)
    if failure.startswith("corrupt"):
        entry = (
            checkpoint.ranks[0].adapter_file_id
            if failure == "corrupt_adapter"
            else checkpoint.ranks[0].optimizer_file_id
        )
        extractor.artifacts.file(checkpoint, entry).write_bytes(b"private corrupted tensor")
    elif failure == "wrong_base":
        command.resolved.base_manifest_sha256 = "0" * 64
    elif failure in {"wrong_shape", "wrong_dtype", "missing_key"}:
        rank = checkpoint.ranks[0]
        if failure == "wrong_shape":
            rank.adapter_tensors[0].shape = [1, 2]
        elif failure == "wrong_dtype":
            rank.adapter_tensors[0].dtype = "float16"
        else:
            rank.adapter_tensors.pop()
        path = extractor.artifacts.directory(checkpoint.artifact_id) / "manifest.json"
        path.write_text(checkpoint.model_dump_json())
        command.checkpoint_manifest_sha256 = checkpoint.canonical_sha256()
    elif failure == "unsafe_config":
        base = extractor.store.path_for("synthetic--extract")
        config = json.loads((base / "config.json").read_bytes())
        config["model_file"] = "private_secret.py"
        (base / "config.json").write_text(json.dumps(config))
    elif failure == "deadline":
        command.deadline = datetime.now(UTC) - timedelta(seconds=1)
    else:
        command.max_bytes = 1
    result = extractor.extract(command)
    assert result.state == "failed"
    assert result.reason == (
        "execution_timeout"
        if failure == "deadline"
        else "disk_full"
        if failure == "quota"
        else "checkpoint_invalid"
    )
    assert not (extractor.artifacts.root / str(command.adapter_id)).exists()
    assert not extractor._staging(command).exists()
    assert extractor.reservations.held_disk_bytes(extractor.artifacts.root) == 0
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize("published", [False, True])
def test_restart_reconciles_copy_or_post_publish_crash_and_releases_hold(
    tmp_path: Path, published: bool
) -> None:
    extractor, command = fixture(tmp_path)
    result = extractor.extract(command)
    assert result.manifest is not None
    record = extractor._read(command.command_id)
    record["status"].update(state="running", manifest=None)
    extractor._save(record)
    # Recreate a genuine held ledger scope, as if the process died before release.
    ledger_file = Path(extractor.settings.node_state_dir) / "reservations.json"
    ledger = json.loads(ledger_file.read_bytes())
    ledger["items"][str(command.disk_reservation_id)]["reservation"]["state"] = "held"
    ledger_file.write_text(json.dumps(ledger))
    reservations = ReservationLedger(extractor.settings, extractor.store, lambda: 0)
    if not published:
        (extractor.artifacts.root / str(command.adapter_id)).rename(extractor._staging(command))
    restarted = AdapterExtractor(
        extractor.artifacts, reservations, extractor.settings, extractor.store
    )
    assert restarted.status(command.command_id).state == ("succeeded" if published else "failed")
    assert not restarted._staging(command).exists()
    hold = reservations.get(command.disk_reservation_id)
    assert hold is not None and hold.state is ReservationState.RELEASED
    assert reservations.held_disk_bytes(extractor.artifacts.root) == 0


def test_copy_failure_releases_hold_and_removes_private_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor, command = fixture(tmp_path)
    original = extractor._deadline

    def deadline(value: TrainingAdapterExtractRequest) -> None:
        if extractor._staging(value).exists():
            raise TimeoutError("private error detail")
        original(value)

    monkeypatch.setattr(extractor, "_deadline", deadline)
    result = extractor.extract(command)
    assert result.reason == "execution_timeout"
    assert not extractor._staging(command).exists()
    hold = extractor.reservations.get(command.disk_reservation_id)
    assert hold is not None and hold.state is ReservationState.RELEASED


async def test_authenticated_control_routes_and_safe_conflicts(tmp_path: Path) -> None:
    extractor, command = fixture(tmp_path)
    control, data = FastAPI(), FastAPI()

    async def auth(x_coire_node_token: str | None = Header(default=None)) -> None:
        if x_coire_node_token != "synthetic-control-token":
            raise HTTPException(401, "Node authentication required")

    control.state.require_node_token = auth
    extractor.attach(control)
    prefix = "/node/training/adapters/extractions"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://control"
    ) as client:
        assert (await client.post(prefix, json=command.model_dump(mode="json"))).status_code == 401
        assert (await client.get(f"{prefix}/{command.command_id}")).status_code == 401
        client.headers["X-Coire-Node-Token"] = "synthetic-control-token"
        assert (await client.get(f"{prefix}/{command.command_id}")).status_code == 404
        response = await client.post(prefix, json=command.model_dump(mode="json"))
        assert response.status_code == 200 and response.json()["state"] == "succeeded", (
            response.text
        )
        assert (await client.get(f"{prefix}/{command.command_id}")).json() == response.json()
        command.max_bytes += 1
        assert (await client.post(prefix, json=command.model_dump(mode="json"))).status_code == 409
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=data), base_url="http://data"
    ) as client:
        assert (await client.post(prefix, json=command.model_dump(mode="json"))).status_code == 404


@pytest.mark.parametrize(
    "kind,invalid",
    [
        ("lora", "executable"),
        ("lora", "architecture"),
        ("lora", "dimensions"),
        ("lora", "adapter_shape"),
        ("lora", "adapter_dtype"),
        ("lora", "adapter_extra"),
        ("qlora", "missing_biases"),
        ("qlora", "quantization"),
        ("dora", "missing_magnitude"),
    ],
)
def test_verified_manifest_does_not_authorize_incompatible_native_artifacts(
    tmp_path: Path, kind: str, invalid: str
) -> None:
    extractor, command = fixture(tmp_path, kind, invalid=invalid)
    # All checksums/descriptors agree; compatibility still needs independent validation.
    extractor.artifacts.verify(command.checkpoint_id, command.checkpoint_manifest_sha256)
    assert extractor.extract(command).reason == "checkpoint_invalid"
    assert extractor.reservations.get(command.disk_reservation_id) is None


def test_persisted_artifacts_remain_counted_against_store_quota(tmp_path: Path) -> None:
    extractor, command = fixture(tmp_path)
    extractor.settings = extractor.settings.model_copy(update={"training_artifact_quota_bytes": 1})
    result = extractor.extract(command)
    assert result.state == "failed" and result.reason == "disk_full"
    assert extractor.reservations.get(command.disk_reservation_id) is None


@pytest.mark.parametrize("edge", ["copy", "published"])
def test_actual_crash_edges_reconcile_on_fresh_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edge: str
) -> None:
    import coire_node.training.extraction as module
    from coire_node.training.checkpoints import _fsync_directory

    extractor, command = fixture(tmp_path)
    original = _fsync_directory

    def crash(path: Path) -> None:
        if edge == "copy" and path == extractor._staging(command):
            raise KeyboardInterrupt("simulate process death during staging")
        if (
            edge == "published"
            and path == extractor.artifacts.root
            and (path / str(command.adapter_id)).exists()
        ):
            raise OSError("simulate lost directory fsync acknowledgement")
        original(path)

    monkeypatch.setattr(module, "_fsync_directory", crash)
    with pytest.raises(KeyboardInterrupt if edge == "copy" else OSError):
        extractor.extract(command)
    assert extractor.status(command.command_id).state == "running"
    assert extractor.reservations.held_disk_bytes(extractor.artifacts.root) == command.max_bytes
    monkeypatch.setattr(module, "_fsync_directory", original)
    ledger = ReservationLedger(extractor.settings, extractor.store, lambda: 0)
    recovered = AdapterExtractor(extractor.artifacts, ledger, extractor.settings, extractor.store)
    assert recovered.status(command.command_id).state == (
        "failed" if edge == "copy" else "succeeded"
    )
    assert ledger.held_disk_bytes(extractor.artifacts.root) == 0
    assert not recovered._staging(command).exists()
