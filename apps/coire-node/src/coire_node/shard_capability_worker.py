"""Bounded, short-lived inspection of the installed bare MLX model implementation."""

from __future__ import annotations

import os
import platform
import resource
import sys
import threading
import time
from pathlib import Path

import psutil

from coire_core.models import ShardingMode


def main() -> int:
    if (
        platform.system() != "Darwin"
        or platform.node().lower().split(".", 1)[0] == "coire-core"
        or len(sys.argv) != 3
    ):
        return 2
    model_path = Path(sys.argv[1])
    mode = ShardingMode(sys.argv[2])
    if not model_path.is_absolute() or not model_path.is_dir():
        return 2
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    process = psutil.Process()
    deadline = time.monotonic() + 85
    stopped = threading.Event()

    def guard() -> None:
        while not stopped.wait(0.25):
            try:
                if time.monotonic() >= deadline or process.memory_info().rss > 1024**3:
                    os._exit(124)
            except psutil.Error:
                os._exit(124)

    threading.Thread(target=guard, daemon=True).start()
    try:
        from mlx_lm import load

        model = load(str(model_path), lazy=True)[0]
        supported = (
            hasattr(model, "shard")
            if mode is ShardingMode.TENSOR_PARALLEL
            else hasattr(getattr(model, "model", None), "pipeline")
        )
        print("true" if supported else "false")
        return 0
    finally:
        stopped.set()


if __name__ == "__main__":
    raise SystemExit(main())
