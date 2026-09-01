"""Small bare-MLX collective benchmark launched once per rank by ``mlx.launch``."""

from __future__ import annotations

import argparse
import json
import platform
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("jaccl", "ring"), required=True)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()

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
