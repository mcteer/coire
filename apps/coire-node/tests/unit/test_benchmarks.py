from __future__ import annotations

import uuid
from pathlib import Path

from coire_core.models import BenchmarkCommand
from coire_node.benchmarks import build_benchmark_argv


def command(placement: str, digest: str | None = None) -> BenchmarkCommand:
    return BenchmarkCommand(
        command_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        slug="coire--tiny",
        placement=placement,
        prompt_tokens=16,
        generation_tokens=8,
        hostfile_sha256=digest,
    )


def test_single_benchmark_is_bare_mlx_lm() -> None:
    argv = build_benchmark_argv(
        command("single:coire-edge-a"), model_path=Path("/models/coire--tiny"), hostfile=None
    )
    assert argv[1:3] == ["-m", "mlx_lm.benchmark"]
    assert "mlx.launch" not in argv


def test_distributed_benchmarks_use_exactly_hostfile_ranks() -> None:
    tp = build_benchmark_argv(
        command("sharded:tp", "0" * 64),
        model_path=Path("/models/coire--tiny"),
        hostfile=Path("/state/jaccl.json"),
    )
    pp = build_benchmark_argv(
        command("sharded:pp", "0" * 64),
        model_path=Path("/models/coire--tiny"),
        hostfile=Path("/state/ring.json"),
    )
    assert "-n" not in tp and "jaccl" in tp
    assert "--pipeline" in pp and "ring" in pp
