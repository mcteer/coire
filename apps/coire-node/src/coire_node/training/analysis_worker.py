"""Fixed native CPU analysis entry point; node owns spawn, memory and termination."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psutil

from coire_core.models.training_node import DatasetAnalysisWorkerInput
from coire_node.store import write_atomic
from coire_node.training.datasets import analyze_source, load_analysis_tokenizer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", type=uuid.UUID, required=True)
    parser.add_argument("--owner", type=uuid.UUID, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--store-root", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.node().lower().split(".", 1)[0] == "coire-core":
        parser.error("native analysis worker requires a Studio")
    root = args.state_root / str(args.analysis)
    if (
        root.is_symlink()
        or not root.is_dir()
        or (root / "worker.json").is_symlink()
        or (root / "worker.json").stat().st_size > 512 * 1024
    ):
        parser.error("analysis envelope path is unsafe")
    envelope = DatasetAnalysisWorkerInput.model_validate_json((root / "worker.json").read_bytes())
    journal = json.loads((root / "journal.json").read_bytes())
    envelope_sha = hashlib.sha256(envelope.model_dump_json().encode()).hexdigest()
    if journal["spawn_nonce"] != str(args.owner) or journal["request_sha256"] != envelope_sha:
        parser.error("analysis ownership differs from journal")
    if envelope.analysis_id != args.analysis:
        parser.error("analysis identity differs from node-owned envelope")
    if datetime.now(UTC) >= envelope.deadline:
        return 124
    from coire_node.training.telemetry import initialize_training_telemetry

    initialize_training_telemetry()
    stopped = threading.Event()
    process = psutil.Process()

    def guard() -> None:
        while not stopped.wait(0.5):
            try:
                unsafe = datetime.now(UTC) >= envelope.deadline or process.memory_info().rss > min(
                    envelope.memory_bytes, 1024**3
                )
            except (OSError, ValueError, psutil.Error):
                unsafe = True
            if unsafe:
                os._exit(124)

    watcher = threading.Thread(target=guard, daemon=True)
    watcher.start()
    try:
        tokenizer, tokenizer_sha, template_sha, runtime_sha = load_analysis_tokenizer(
            args.store_root / envelope.binding.model_slug,
            envelope.binding,
        )
        result = analyze_source(
            root / "source.jsonl",
            envelope,
            tokenizer,
            tokenizer_sha256=tokenizer_sha,
            template_sha256=template_sha,
            runtime_sha256=runtime_sha,
            max_sequence_length=envelope.max_sequence_length,
        )
        write_atomic(root / f"result-{envelope_sha}.json", result.model_dump_json().encode())
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return 0
    finally:
        stopped.set()
        watcher.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
