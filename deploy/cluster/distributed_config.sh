#!/usr/bin/env bash
# Generate or validate MLX's complete two-Studio inventory. Core is never a rank.
set -euo pipefail

usage() { echo "usage: $0 --generate <jaccl|ring> <output> | --check <hostfile>"; }

case "${1:-}" in
  --generate)
    BACKEND="${2:-}"
    OUTPUT="${3:-}"
    [[ "$BACKEND" == jaccl || "$BACKEND" == ring ]] && [[ -n "$OUTPUT" ]] || {
      usage >&2
      exit 2
    }
    MLX_CONFIG="${COIRE_MLX_DISTRIBUTED_CONFIG:-/opt/coire/envs/current/bin/mlx.distributed_config}"
    ARGS=(--verbose --backend "$BACKEND" --hosts coire-edge-a.fabric,coire-edge-b.fabric --output "$OUTPUT")
    if [[ "$BACKEND" == jaccl ]]; then
      ARGS+=(--over thunderbolt --auto-setup)
    fi
    "$MLX_CONFIG" "${ARGS[@]}"
    ;;
  --check)
    OUTPUT="${2:-}"
    [[ -n "$OUTPUT" ]] || { usage >&2; exit 2; }
    ;;
  *) usage >&2; exit 2 ;;
esac

python3 - "$OUTPUT" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8") as stream:
    data = json.load(stream)
entries = data["hosts"]
hosts = [item["ssh"] for item in entries]
assert hosts == ["coire-edge-a.fabric", "coire-edge-b.fabric"]
assert all("core" not in host for host in hosts)
assert data["backend"] in {"jaccl", "ring"}
if data["backend"] == "jaccl":
    assert all("rdma" in item for item in entries)
PY
echo "$OUTPUT"
