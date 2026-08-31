#!/usr/bin/env bash
# Generate the JACCL inventory on edge-a; core is intentionally never a rank.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT="${1:-$ROOT/deploy/cluster/jaccl-hostfile.json}"
TEMPLATE="$ROOT/deploy/cluster/jaccl-hostfile.template.json"

python3 -m json.tool "$TEMPLATE" >/dev/null
cp "$TEMPLATE" "$OUTPUT"
python3 - "$OUTPUT" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
hosts = [item["host"] for item in data["hosts"]]
assert hosts == ["coire-edge-a.fabric", "coire-edge-b.fabric"]
assert all("core" not in host for host in hosts)
PY
echo "$OUTPUT"
