"""CPU analysis statistics and refusal logic with inert tokenizer primitives on core."""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from coire_core.errors import TrainingValidationError
from coire_core.models.datasets import DatasetAnalysisBinding, DatasetFormat
from coire_core.models.training_node import (
    DatasetAnalysisWorkerInput,
    DatasetInputGrant,
    NodeDatasetAnalysisRequest,
)
from coire_core.settings import Settings
from coire_node.reservations import ReservationLedger
from coire_node.training.analysis_supervisor import AnalysisSupervisor
from coire_node.training.datasets import analyze_source, distribution, load_analysis_tokenizer


class Tokenizer:
    has_chat_template = False

    def encode(self, text: str) -> list[int]:
        return [0, *(ord(letter) for letter in text)]

    def apply_chat_template(self, messages: list[dict[str, Any]], **_kwargs: Any) -> list[int]:
        raise AssertionError("raw text must not use chat framing")


def command(data: bytes) -> NodeDatasetAnalysisRequest:
    dataset_id, analysis_id, model_id, variant_id = (uuid.uuid4() for _ in range(4))
    source_sha = hashlib.sha256(data).hexdigest()
    return NodeDatasetAnalysisRequest(
        command_id=uuid.uuid4(),
        request_sha256="a" * 64,
        analysis_id=analysis_id,
        model_id=model_id,
        variant_id=variant_id,
        base_manifest_sha256="b" * 64,
        input_grant=DatasetInputGrant(
            grant_id=uuid.uuid4(),
            node="coire-edge-a",
            dataset_id=dataset_id,
            source_sha256=source_sha,
            max_bytes=len(data),
            analysis_id=analysis_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
            secret="private-test-grant" * 3,
        ),
        reservation_id=uuid.uuid4(),
        memory_bytes=1024**3,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        binding=DatasetAnalysisBinding(
            dataset_id=dataset_id,
            model_id=model_id,
            variant_id=variant_id,
            base_manifest_sha256="b" * 64,
            source_sha256=source_sha,
            split_sha256="c" * 64,
            format=DatasetFormat.TEXT,
            model_slug="synthetic--base",
        ),
    )


def test_cpu_analysis_records_complete_counts_and_content_only_duplicates(tmp_path: Path) -> None:
    data = b'{"text":"one","metadata":{"note":"private"}}\n{"text":"one"}\n{"text":"three"}\n'
    path = tmp_path / "source.jsonl"
    path.write_bytes(data)
    result = analyze_source(
        path,
        command(data),
        Tokenizer(),
        tokenizer_sha256="d" * 64,
        template_sha256="e" * 64,
        runtime_sha256="f" * 64,
    )
    assert result.state == "succeeded" and result.duplicate_rows == 1
    assert result.role_counts == {"assistant": 3}
    assert result.tokens is not None and result.tokens.minimum == 4 and result.tokens.maximum == 6
    assert sum(result.tokens.histogram) == 3
    assert "private" not in result.model_dump_json()


def test_overlength_is_reported_without_truncating_statistics(tmp_path: Path) -> None:
    data = json.dumps({"text": "x" * 128}).encode()
    path = tmp_path / "source.jsonl"
    path.write_bytes(data)
    result = analyze_source(
        path,
        command(data),
        Tokenizer(),
        tokenizer_sha256="d" * 64,
        template_sha256="e" * 64,
        runtime_sha256="f" * 64,
        max_sequence_length=32,
    )
    assert result.state == "failed" and result.invalid_count == 1
    assert result.diagnostics[0].code == "overlength"
    assert result.tokens is not None and result.tokens.maximum == 129


def test_changed_input_and_expired_deadline_are_refused(tmp_path: Path) -> None:
    data = b'{"text":"one"}\n'
    request = command(data)
    path = tmp_path / "source.jsonl"
    path.write_bytes(data.replace(b"one", b"two"))
    with pytest.raises(TrainingValidationError, match="immutable"):
        analyze_source(
            path,
            request,
            Tokenizer(),
            tokenizer_sha256="d" * 64,
            template_sha256="e" * 64,
            runtime_sha256="f" * 64,
        )
    path.write_bytes(data)
    expired = request.model_copy(update={"deadline": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(TrainingValidationError, match="deadline"):
        analyze_source(
            path,
            expired,
            Tokenizer(),
            tokenizer_sha256="d" * 64,
            template_sha256="e" * 64,
            runtime_sha256="f" * 64,
        )


def test_histogram_uses_exact_nearest_rank_percentiles() -> None:
    result = distribution(list(range(1, 101)))
    assert result is not None and result.p50 == 50 and result.p95 == 95
    assert sum(result.histogram) == 100 and result.upper_bounds[-1] >= 100


def test_actual_tokenizer_loading_refuses_core_before_import_or_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("coire_node.training.datasets.platform.node", lambda: "coire-core.lab")
    with pytest.raises(TrainingValidationError, match="Studios"):
        load_analysis_tokenizer(tmp_path, command(b"x").binding)


class AnalysisLedger:
    def __init__(self) -> None:
        self.holds: list[Any] = []
        self.releases: list[uuid.UUID] = []

    def bind_owner(self, *_args: object, **_kwargs: object) -> None:
        pass

    def hold(self, request: Any, **_kwargs: Any) -> tuple[None, bool]:
        self.holds.append(request)
        return None, True

    def release(self, identity: uuid.UUID) -> bool:
        self.releases.append(identity)
        return True


def supervisor_request(
    tmp_path: Path, data: bytes, monkeypatch: pytest.MonkeyPatch
) -> tuple[AnalysisSupervisor, NodeDatasetAnalysisRequest, AnalysisLedger]:
    monkeypatch.setattr(
        "coire_node.training.analysis_supervisor.platform.node", lambda: "coire-edge-a.lab"
    )
    settings = Settings(
        node_state_dir=str(tmp_path), node_name="coire-edge-a", training_enabled=True
    )
    ledger = AnalysisLedger()
    supervisor = AnalysisSupervisor(settings, cast(ReservationLedger, ledger))
    request = command(data)
    envelope = DatasetAnalysisWorkerInput(
        command_id=request.command_id,
        analysis_id=request.analysis_id,
        binding=request.binding,
        source_bytes=len(data),
        memory_bytes=request.memory_bytes,
        deadline=request.deadline,
        max_sequence_length=settings.training_max_sequence_length,
    )
    request = request.model_copy(
        update={"request_sha256": hashlib.sha256(envelope.model_dump_json().encode()).hexdigest()}
    )
    return supervisor, request, ledger


@pytest.mark.parametrize("response_data", [b"wrong", b"x" * 100])
async def test_download_mismatch_cleans_source_and_releases_own_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response_data: bytes
) -> None:
    supervisor, request, ledger = supervisor_request(tmp_path, b'{"text":"one"}\n', monkeypatch)
    client_type = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return client_type(
            **kwargs,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=response_data)
            ),
        )

    monkeypatch.setattr("coire_node.training.analysis_supervisor.httpx.AsyncClient", client)
    with pytest.raises(ValueError, match="source"):
        await supervisor.start(request)
    assert ledger.releases == [request.reservation_id]
    assert not (supervisor.path(request.analysis_id) / "source.jsonl").exists()
    assert (
        request.input_grant.secret
        not in (supervisor.path(request.analysis_id) / "journal.json").read_text()
    )
    assert (
        ledger.holds[0].disk_bytes >= len(response_data)
        or len(response_data) > request.input_grant.max_bytes
    )


async def test_preparation_cancel_interrupts_download_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, request, ledger = supervisor_request(tmp_path, b'{"text":"one"}\n', monkeypatch)
    entered = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            entered.set()
            await asyncio.Event().wait()
            yield b"never"

    client_type = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return client_type(
            **kwargs,
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=Stream())),
        )

    monkeypatch.setattr("coire_node.training.analysis_supervisor.httpx.AsyncClient", client)
    task = asyncio.create_task(supervisor.start(request))
    await asyncio.wait_for(entered.wait(), 1)
    result = await asyncio.wait_for(supervisor.cancel(request.analysis_id), 1)
    assert result.state == "cancelled" and task.cancelled()
    assert not (supervisor.path(request.analysis_id) / "source.jsonl").exists()
    assert ledger.releases


async def test_long_deadline_refused_before_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor, request, ledger = supervisor_request(tmp_path, b"x", monkeypatch)
    request = request.model_copy(update={"deadline": datetime.now(UTC) + timedelta(hours=1)})
    with pytest.raises(ValueError, match="30 minutes"):
        await supervisor.start(request)
    assert not ledger.holds


async def test_cache_filesystem_floor_is_checked_before_reserving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    supervisor, request, ledger = supervisor_request(tmp_path, b"x", monkeypatch)
    monkeypatch.setattr(
        "coire_node.training.analysis_supervisor.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1),
    )
    with pytest.raises(ValueError, match="free space"):
        await supervisor.start(request)
    assert not ledger.holds and not supervisor.path(request.analysis_id).exists()


async def test_fixed_spawn_has_presaved_intent_and_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from coire_core.models.training_node import NodeDatasetAnalysisStatus

    data = b'{"text":"one"}\n'
    supervisor, request, ledger = supervisor_request(tmp_path, data, monkeypatch)
    supervisor.settings = supervisor.settings.model_copy(
        update={"training_max_sequence_length": 32}
    )
    request = request.model_copy(update={"max_sequence_length": 64})
    frozen = DatasetAnalysisWorkerInput(
        command_id=request.command_id,
        analysis_id=request.analysis_id,
        binding=request.binding,
        source_bytes=len(data),
        memory_bytes=request.memory_bytes,
        deadline=request.deadline,
        max_sequence_length=request.max_sequence_length,
    )
    request = request.model_copy(
        update={"request_sha256": hashlib.sha256(frozen.model_dump_json().encode()).hexdigest()}
    )
    client_type = httpx.AsyncClient
    seen: dict[str, Any] = {}

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return client_type(
            **kwargs,
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=data)),
        )

    def spawn(argv: list[str], **kwargs: Any) -> Any:
        journal = supervisor.journal(request.analysis_id)
        assert journal["state"] == "launching" and journal["pid"] is None
        assert argv == supervisor.argv(request.analysis_id, journal["spawn_nonce"])
        seen.update(kwargs)
        return SimpleNamespace(pid=42)

    async def status(identity: uuid.UUID) -> NodeDatasetAnalysisStatus:
        return NodeDatasetAnalysisStatus(analysis_id=identity, state="running", completed_rows=0)

    monkeypatch.setattr("coire_node.training.analysis_supervisor.httpx.AsyncClient", client)
    monkeypatch.setattr("coire_node.training.analysis_supervisor.subprocess.Popen", spawn)
    monkeypatch.setattr(
        "coire_node.training.analysis_supervisor.psutil.Process",
        lambda _pid: SimpleNamespace(create_time=lambda: 1.0),
    )
    monkeypatch.setattr(supervisor, "_status", status)
    monkeypatch.setenv("COIRE_NODE_TOKEN", "secret-value")
    monkeypatch.setenv("CUSTOM_CREDENTIAL", "secret-value")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    assert (await supervisor.start(request)).state == "running"
    assert (
        DatasetAnalysisWorkerInput.model_validate_json(
            (supervisor.path(request.analysis_id) / "worker.json").read_bytes()
        ).max_sequence_length
        == 64
    )
    assert "secret-value" not in str(seen) and "PYTHONPATH" not in seen["env"]
    assert seen["env"]["HF_HUB_OFFLINE"] == "1" and seen["start_new_session"]
    assert len(ledger.holds) == 1
    assert (await supervisor.start(request)).state == "running" and len(ledger.holds) == 1
    refreshed = request.model_copy(
        update={
            "input_grant": request.input_grant.model_copy(
                update={
                    "grant_id": uuid.uuid4(),
                    "expires_at": datetime.now(UTC) + timedelta(minutes=2),
                    "secret": "refreshed-private-grant" * 3,
                }
            )
        }
    )
    assert (await supervisor.start(refreshed)).state == "running" and len(ledger.holds) == 1
    changed = request.model_copy(update={"template_sha256": "f" * 64})
    with pytest.raises(ValueError, match="identity conflicts"):
        await supervisor.start(changed)


def test_result_is_bound_to_worker_envelope_and_pinned_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b'{"text":"one"}\n'
    supervisor, request, _ledger = supervisor_request(tmp_path, data, monkeypatch)
    root = supervisor.path(request.analysis_id)
    root.mkdir(mode=0o700)
    source = root / "source.jsonl"
    source.write_bytes(data)
    envelope = DatasetAnalysisWorkerInput(
        command_id=request.command_id,
        analysis_id=request.analysis_id,
        binding=request.binding,
        source_bytes=len(data),
        memory_bytes=request.memory_bytes,
        deadline=request.deadline,
    )
    (root / "worker.json").write_text(envelope.model_dump_json())
    result = analyze_source(
        source,
        envelope,
        Tokenizer(),
        tokenizer_sha256="d" * 64,
        template_sha256="e" * 64,
        runtime_sha256="f" * 64,
    )
    result_bytes = result.model_dump_json().encode()
    result_sha = hashlib.sha256(result_bytes).hexdigest()
    (root / f"result-{request.request_sha256}.json").write_bytes(result_bytes)
    supervisor.save(
        request.analysis_id,
        {
            "analysis_id": str(request.analysis_id),
            "request_sha256": request.request_sha256,
            "result_sha256": result_sha,
        },
    )
    assert supervisor.result(request.analysis_id) == result
    (root / "worker.json").write_text(
        envelope.model_copy(update={"memory_bytes": 1}).model_dump_json()
    )
    with pytest.raises(ValueError, match="envelope changed"):
        supervisor.result(request.analysis_id)


def tokenizer_assets(
    tmp_path: Path, tokenizer_config: dict[str, Any], model_config: dict[str, Any] | None = None
) -> tuple[Path, DatasetAnalysisBinding]:
    from coire_core.models.jobs import ChecksumManifest, ManifestFile

    root = tmp_path / "synthetic--base"
    root.mkdir()
    files = {
        "config.json": json.dumps(model_config or {"model_type": "qwen2"}).encode(),
        "tokenizer_config.json": json.dumps(tokenizer_config).encode(),
        "tokenizer.json": b"{}",
        "model.safetensors": b"must-not-open-weight-file",
    }
    entries = []
    for name, data in files.items():
        (root / name).write_bytes(data)
        entries.append(
            ManifestFile(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        )
    manifest = ChecksumManifest(
        slug=root.name,
        repo_id="synthetic/base",
        revision="local",
        files=entries,
        total_bytes=sum(len(data) for data in files.values()),
        created_at=datetime.now(UTC),
    )
    root.with_name(root.name + ".manifest.json").write_text(manifest.model_dump_json())
    return root, command(b"x").binding.model_copy(
        update={"base_manifest_sha256": manifest.sha256()}
    )


@pytest.mark.parametrize(
    "bad_config",
    [
        {"tokenizer_class": "UnapprovedCustomTokenizer"},
        {
            "tokenizer_class": "PreTrainedTokenizerFast",
            "auto_map": {"AutoTokenizer": "private.code"},
        },
        {"tokenizer_class": ["wrong"]},
        {"tokenizer_class": "PreTrainedTokenizerFast", "tool_parser_type": "../../custom"},
    ],
)
def test_local_tokenizer_refuses_executable_or_malformed_configuration_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_config: dict[str, Any]
) -> None:
    root, binding = tokenizer_assets(tmp_path, bad_config)
    monkeypatch.setattr("coire_node.training.datasets.platform.node", lambda: "coire-edge-a.lab")
    monkeypatch.setattr("coire_node.training.datasets.platform.system", lambda: "Darwin")
    with pytest.raises(TrainingValidationError, match="unapproved"):
        load_analysis_tokenizer(root, binding)


@pytest.mark.parametrize("template_override", [None, "explicit-inert-template"])
def test_inert_tokenizer_loader_never_opens_weights_and_forces_local_safe_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template_override: str | None
) -> None:
    import sys
    from types import ModuleType, SimpleNamespace

    root, binding = tokenizer_assets(tmp_path, {"tokenizer_class": "PreTrainedTokenizerFast"})
    binding = binding.model_copy(update={"template_override": template_override})
    module = ModuleType("mlx_lm.tokenizer_utils")
    observed: dict[str, Any] = {}

    def load(path: Path, **kwargs: Any) -> Any:
        observed.update(kwargs)
        assert path == root
        effective = kwargs["tokenizer_config_extra"].get("chat_template", "inert-template")
        return SimpleNamespace(chat_template=effective, has_chat_template=True)

    module.__dict__["load"] = load
    monkeypatch.setitem(sys.modules, "mlx_lm", ModuleType("mlx_lm"))
    monkeypatch.setitem(sys.modules, "mlx_lm.tokenizer_utils", module)
    monkeypatch.setattr("coire_node.training.datasets.platform.node", lambda: "coire-edge-a.lab")
    monkeypatch.setattr("coire_node.training.datasets.platform.system", lambda: "Darwin")
    monkeypatch.setattr("coire_node.training.datasets.version", lambda _name: "test-runtime")
    original_open = Path.open

    def safe_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert path.name != "model.safetensors", "analysis opened base weights"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", safe_open)
    _tokenizer, tokenizer_sha, template_sha, runtime_sha = load_analysis_tokenizer(root, binding)
    expected: dict[str, Any] = {"trust_remote_code": False, "local_files_only": True}
    if template_override is not None:
        expected["chat_template"] = template_override
    assert observed == {"tokenizer_config_extra": expected}
    assert (
        template_sha == hashlib.sha256((template_override or "inert-template").encode()).hexdigest()
    )
    assert _tokenizer.has_chat_template
    assert len(tokenizer_sha) == len(template_sha) == len(runtime_sha) == 64


def test_evaluation_tokenizer_accepts_serving_only_architecture_without_widening_sft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import ModuleType, SimpleNamespace

    from coire_node.training.datasets import load_evaluation_tokenizer

    root, binding = tokenizer_assets(
        tmp_path,
        {"tokenizer_class": "Qwen2Tokenizer"},
        {"model_type": "qwen4_exp", "per_module_quantization": "x" * 80_000},
    )
    module = ModuleType("mlx_lm.tokenizer_utils")
    calls: list[dict[str, Any]] = []

    def load(path: Path, **kwargs: Any) -> Any:
        assert path == root
        calls.append(kwargs)
        return SimpleNamespace(chat_template="inert-serving-template")

    module.__dict__["load"] = load
    monkeypatch.setitem(sys.modules, "mlx_lm", ModuleType("mlx_lm"))
    monkeypatch.setitem(sys.modules, "mlx_lm.tokenizer_utils", module)
    monkeypatch.setattr("coire_node.training.datasets.platform.node", lambda: "coire-edge-a.lab")
    monkeypatch.setattr("coire_node.training.datasets.platform.system", lambda: "Darwin")
    monkeypatch.setattr("coire_node.training.datasets.version", lambda _name: "test-runtime")
    original_open = Path.open

    def safe_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert path.name != "model.safetensors", "evaluation opened model weights"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", safe_open)
    load_evaluation_tokenizer(
        root, base_manifest_sha256=binding.base_manifest_sha256, template_override=None
    )
    assert calls == [
        {"tokenizer_config_extra": {"trust_remote_code": False, "local_files_only": True}}
    ]
    with pytest.raises(TrainingValidationError):
        load_analysis_tokenizer(root, binding)
    assert len(calls) == 1, "SFT must reject before importing its tokenizer"


@pytest.mark.parametrize(
    "config",
    [
        {"model_type": "qwen4_exp", "auto_map": {"AutoModel": "custom.code"}},
        {"model_type": "qwen4_exp", "model_file": "custom.py"},
        {"model_type": "qwen4_exp", "padding": "x" * (1024**2)},
    ],
)
def test_evaluation_tokenizer_rejects_executable_or_oversized_serving_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]
) -> None:
    from coire_node.training.datasets import load_evaluation_tokenizer

    root, binding = tokenizer_assets(tmp_path, {"tokenizer_class": "Qwen2Tokenizer"}, config)
    monkeypatch.setattr("coire_node.training.datasets.platform.node", lambda: "coire-edge-a.lab")
    monkeypatch.setattr("coire_node.training.datasets.platform.system", lambda: "Darwin")
    with pytest.raises(TrainingValidationError):
        load_evaluation_tokenizer(
            root, base_manifest_sha256=binding.base_manifest_sha256, template_override=None
        )
