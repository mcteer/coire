"""Bare MLX-LM placement benchmarks with fixed arguments and registry paths."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import re
import subprocess
import sys
from pathlib import Path

from coire_core.models import BenchmarkCommand, BenchmarkMeasurement
from coire_core.settings import Settings
from coire_node.store import Store

AVERAGE = re.compile(r"Averages:.*generation_tps=([0-9]+(?:\.[0-9]+)?)")


def build_benchmark_argv(
    command: BenchmarkCommand, *, model_path: Path, hostfile: Path | None
) -> list[str]:
    override = os.environ.get("COIRE_BENCHMARK_COMMAND")
    python = sys.executable
    benchmark = [
        python,
        "-m",
        "mlx_lm.benchmark",
        "--model",
        str(model_path),
        "--prompt-tokens",
        str(command.prompt_tokens),
        "--generation-tokens",
        str(command.generation_tokens),
        "--num-trials",
        "3",
    ]
    if command.placement == "single:coire-edge-a":
        return (override.split(os.pathsep) if override else []) + benchmark
    mode = command.placement.rsplit(":", 1)[1]
    launcher = override.split(os.pathsep) if override else [python, "-m", "mlx.launch"]
    assert hostfile is not None
    argv = [
        *launcher,
        "--backend",
        "jaccl" if mode == "tp" else "ring",
        "--hostfile",
        str(hostfile),
        "--env",
        "MLX_METAL_FAST_SYNCH=1",
        "--",
        *benchmark,
    ]
    if mode == "pp":
        argv.append("--pipeline")
    return argv


class BenchmarkRunner:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._results: dict[object, BenchmarkMeasurement] = {}

    def run(self, command: BenchmarkCommand) -> BenchmarkMeasurement:
        cached = self._results.get(command.command_id)
        if cached is not None:
            return cached
        model_path = self.store.path_for(command.slug)
        if not model_path.is_dir():
            raise FileNotFoundError(command.slug)
        hostfile: Path | None = None
        if command.placement.startswith("sharded:"):
            mode = command.placement.rsplit(":", 1)[1]
            hostfile = Path(
                self.settings.sharding_jaccl_hostfile
                if mode == "tp"
                else self.settings.sharding_ring_hostfile
            )
            if hashlib.sha256(hostfile.read_bytes()).hexdigest() != command.hostfile_sha256:
                raise ValueError("hostfile digest does not match benchmark command")
        completed = subprocess.run(
            build_benchmark_argv(command, model_path=model_path, hostfile=hostfile),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.settings.sharding_start_timeout_s,
            check=False,
            env={**os.environ, "HF_HUB_OFFLINE": "1"},
        )
        output = completed.stdout.decode(errors="replace")
        match = AVERAGE.search(output)
        succeeded = completed.returncode == 0 and match is not None
        try:
            version = importlib.metadata.version("mlx-lm")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        result = BenchmarkMeasurement(
            command_id=command.command_id,
            placement=command.placement,
            tokens_per_second=float(match.group(1)) if succeeded and match else None,
            engine_version=version,
            failure=None
            if succeeded
            else f"benchmark exited {completed.returncode}: {output[-400:]}",
        )
        self._results[command.command_id] = result
        return result
