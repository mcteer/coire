"""Native CPU worker fails closed before tokenizer work; all process/runtime hooks are fake."""

import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from coire_core.models.datasets import DatasetAnalysisBinding, DatasetFormat
from coire_core.models.training_node import DatasetAnalysisWorkerInput
from coire_node.training import analysis_worker


def prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, expired: bool = False
) -> DatasetAnalysisWorkerInput:
    identity, owner = uuid.uuid4(), uuid.uuid4()
    root = tmp_path / str(identity)
    root.mkdir(mode=0o700)
    envelope = DatasetAnalysisWorkerInput(
        command_id=uuid.uuid4(),
        analysis_id=identity,
        binding=DatasetAnalysisBinding(
            dataset_id=uuid.uuid4(),
            model_id=uuid.uuid4(),
            variant_id=uuid.uuid4(),
            base_manifest_sha256="b" * 64,
            source_sha256="c" * 64,
            split_sha256="d" * 64,
            format=DatasetFormat.TEXT,
            model_slug="synthetic--base",
        ),
        source_bytes=1,
        memory_bytes=1024**3,
        deadline=datetime.now(UTC) + timedelta(seconds=-1 if expired else 60),
    )
    (root / "worker.json").write_text(envelope.model_dump_json())
    (root / "journal.json").write_text(
        json.dumps(
            {
                "spawn_nonce": str(owner),
                "request_sha256": hashlib.sha256(envelope.model_dump_json().encode()).hexdigest(),
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "analysis-worker",
            "--analysis",
            str(identity),
            "--owner",
            str(owner),
            "--state-root",
            str(tmp_path),
            "--store-root",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(
        "coire_node.training.analysis_worker.platform.node", lambda: "coire-edge-a.lab"
    )
    monkeypatch.setattr("coire_node.training.analysis_worker.platform.system", lambda: "Darwin")

    def no_loader(*_args: Any) -> Any:
        pytest.fail("watchguard allowed tokenizer work")

    monkeypatch.setattr(analysis_worker, "load_analysis_tokenizer", no_loader)
    return envelope


def test_expired_envelope_exits_before_cpu_tokenizer_or_process_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(tmp_path, monkeypatch, expired=True)
    monkeypatch.setattr(
        "coire_node.training.analysis_worker.psutil.Process",
        lambda: pytest.fail("expired worker probed process"),
    )
    assert analysis_worker.main() == 124


def test_valid_owned_analysis_initializes_telemetry_before_tokenizer_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(tmp_path, monkeypatch)
    initialized: list[bool] = []
    monkeypatch.setattr(
        "coire_node.training.telemetry.initialize_training_telemetry",
        lambda: initialized.append(True),
    )

    class LoadingReached(Exception):
        pass

    def load(*_args: Any) -> Any:
        assert initialized == [True]
        raise LoadingReached

    monkeypatch.setattr(analysis_worker, "load_analysis_tokenizer", load)
    with pytest.raises(LoadingReached):
        analysis_worker.main()


@pytest.mark.parametrize("probe_denied", [False, True])
def test_worker_memory_breach_or_unreadable_rss_cannot_disable_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_denied: bool
) -> None:
    prepare(tmp_path, monkeypatch)

    def memory() -> Any:
        if probe_denied:
            raise psutil.AccessDenied(42)
        return SimpleNamespace(rss=1024**3 + 1)

    monkeypatch.setattr(
        "coire_node.training.analysis_worker.psutil.Process",
        lambda: SimpleNamespace(memory_info=memory),
    )
    monkeypatch.setattr(
        "coire_node.training.analysis_worker.threading.Event",
        lambda: SimpleNamespace(wait=lambda _timeout: False, set=lambda: None),
    )

    def thread(target: Any, *, daemon: bool) -> Any:
        assert daemon
        return SimpleNamespace(start=target)

    monkeypatch.setattr("coire_node.training.analysis_worker.threading.Thread", thread)

    class GuardExit(Exception):
        pass

    def exit_worker(code: int) -> None:
        assert code == 124
        raise GuardExit

    monkeypatch.setattr("coire_node.training.analysis_worker.os._exit", exit_worker)
    with pytest.raises(GuardExit):
        analysis_worker.main()
