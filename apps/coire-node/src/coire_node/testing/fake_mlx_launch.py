"""Linux CI stand-in for MLX launch, collectives, and placement benchmarks."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    args = sys.argv[1:]
    joined = " ".join(args)
    if "coire_node.link_probe_worker" in joined:
        for rank in (0, 1):
            print(
                "COIRE_PROBE_RESULT "
                f'{{"rank":{rank},"bandwidth_bytes_per_second":1250000000,'
                '"latency_ms":0.85,"os_version":"ci-linux","engine_version":"fake-1"}}',
                flush=True,
            )
        return 0
    if "mlx_lm.benchmark" in joined:
        placement_tps = 24.0 if "--pipeline" in args else 20.0 if "--hostfile" in args else 18.0
        print(
            f"Averages: prompt_tps=100.000, generation_tps={placement_tps:.3f}, peak_memory=1.000"
        )
        return 0
    try:
        separator = args.index("--")
    except ValueError:
        separator = -1
    child = args[separator + 1 :] if separator >= 0 else args
    try:
        module = child.index("mlx_lm.server")
    except ValueError:
        print("fake launcher received an unknown command", file=sys.stderr)
        return 2
    child[module] = "coire_node.testing.fake_engine"
    return subprocess.call(child, env={**os.environ, "HF_HUB_OFFLINE": "1"})


if __name__ == "__main__":
    raise SystemExit(main())
