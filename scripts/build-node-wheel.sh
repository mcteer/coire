#!/usr/bin/env bash
# Build the node-agent wheels on core and copy them to a Studio over the mesh.
#
#   build-node-wheel.sh coire-edge-a
#
# The mesh is the only transfer path: it is ~30x faster than the egress interface and is the
# platform fabric (ARCHITECTURE.md 2.1).
set -euo pipefail
NODE="${1:?usage: build-node-wheel.sh <node-name>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
rm -rf dist && mkdir -p dist
uv build --package coire-core --out-dir dist
uv build --package coire-node --out-dir dist
echo "built: $(ls dist/*.whl | tr '\n' ' ')"
ssh "mcteer@${NODE}.mesh" "mkdir -p /opt/coire/dist" \
  || { echo "mesh path to $NODE unreachable; is scripts/apply-mesh-hosts.sh applied?" >&2; exit 1; }
scp dist/*.whl "mcteer@${NODE}.mesh:/opt/coire/dist/"
echo "copied to ${NODE}:/opt/coire/dist — now run on that node:"
echo "  apps/coire-node/install.sh --wheel-dir /opt/coire/dist"
