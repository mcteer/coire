"""Small bare-MLX collective benchmark launched once per rank by ``mlx.launch``."""

from __future__ import annotations

import argparse
import json
import platform
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("jaccl", "ring"), required=True)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--managed-runtime", action="store_true")
    parser.add_argument("--environment-root", type=Path)
    args = parser.parse_args()

    from coire_core.settings import Settings
    from coire_node.upgrade_runtime import running_runtime_sha256

    settings = Settings()
    if args.managed_runtime:
        hostname = platform.node().lower().split(".", 1)[0]
        if hostname not in {"coire-edge-a", "coire-edge-b"} or (
            args.environment_root is None or not args.environment_root.is_absolute()
        ):
            raise ValueError("managed collective requires a configured Studio environment")
        settings.upgrades_enabled = True
        settings.node_name = hostname
        settings.upgrade_environment_root = str(args.environment_root)
    elif args.environment_root is not None:
        raise ValueError("collective environment root requires managed runtime identity")
    runtime_sha256 = running_runtime_sha256(settings)

    import mlx.core as mx

    group = mx.distributed.init(backend=args.backend)
    if group.size() != 2:
        raise RuntimeError(f"probe requires exactly two ranks, got {group.size()}")
    rates: list[float] = []
    latencies: list[float] = []
    for size in (1 << 20, 16 << 20, 64 << 20):
        values = mx.ones((size // 4,), dtype=mx.float32)
        mx.eval(mx.distributed.all_sum(values, group=group))
        for _ in range(args.iterations):
            started = time.perf_counter()
            mx.eval(mx.distributed.all_sum(values, group=group))
            elapsed = time.perf_counter() - started
            latencies.append(elapsed * 1000)
            rates.append(size / elapsed)
    print(
        "COIRE_PROBE_RESULT "
        + json.dumps(
            {
                "rank": group.rank(),
                "runtime_sha256": runtime_sha256,
                "bandwidth_bytes_per_second": int(max(rates)),
                "latency_ms": min(latencies),
                "os_version": platform.mac_ver()[0],
                "engine_version": getattr(mx, "__version__", "unknown"),
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
