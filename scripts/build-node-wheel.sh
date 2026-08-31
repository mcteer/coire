#!/usr/bin/env bash
# Build the node-agent wheels on core and stage them on a Studio over control DNS.
#
#   build-node-wheel.sh coire-edge-a
#
# Installation is staged in the operator's home directory. The installer later copies the runtime
# into /opt/coire after the operator has created that sudo-owned boundary.
set -euo pipefail
NODE="${1:?usage: build-node-wheel.sh <node-name>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
rm -rf dist && mkdir -p dist
uv build --package coire-core --out-dir dist
uv build --package coire-node --out-dir dist
echo "built: $(ls dist/*.whl | tr '\n' ' ')"
ssh "mcteer@${NODE}" 'mkdir -p "$HOME/coire-stage/dist" "$HOME/coire-stage/apps/coire-node" "$HOME/coire-stage/deploy/launchd"' \
  || { echo "control path to $NODE is unreachable" >&2; exit 1; }
scp dist/*.whl "mcteer@${NODE}:coire-stage/dist/"
scp apps/coire-node/install.sh "mcteer@${NODE}:coire-stage/apps/coire-node/"
scp deploy/launchd/com.coire.node.plist.template "mcteer@${NODE}:coire-stage/deploy/launchd/"
echo "staged on ${NODE}:~/coire-stage — after creating /opt/coire, run:"
echo "  ~/coire-stage/apps/coire-node/install.sh --wheel-dir ~/coire-stage/dist"
