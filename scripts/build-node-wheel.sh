#!/usr/bin/env bash
# Build the node-agent wheels on core and stage them on a Studio over control DNS.
#
#   build-node-wheel.sh coire-edge-a
#   build-node-wheel.sh --local-only   # verify/stage without touching a Studio
#
# Installation is staged in the operator's home directory. The installer later copies the runtime
# into /opt/coire after the operator has created that sudo-owned boundary.
set -euo pipefail
LOCAL_ONLY=0
if [[ "${1:-}" == "--local-only" ]]; then
  LOCAL_ONLY=1
  shift
fi
NODE="${1:-}"
if [[ "$LOCAL_ONLY" -eq 0 && -z "$NODE" ]]; then
  echo "usage: build-node-wheel.sh [--local-only] <node-name>" >&2
  exit 2
fi
CONTROL_TARGET="$NODE"
case "$CONTROL_TARGET" in
  *.lab) ;;
  *) CONTROL_TARGET="$CONTROL_TARGET.lab" ;;
esac
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
rm -rf dist && mkdir -p dist
uv build --package coire-core --out-dir dist
uv build --package coire-node --out-dir dist
mkdir -p dist/node-wheels
uv export --locked --package coire-node --no-dev --no-emit-workspace \
  --no-header --no-annotate --format requirements.txt \
  --output-file dist/node-wheels/requirements.txt >/dev/null
uv export --locked --package coire-node --no-dev --no-emit-workspace \
  --format pylock.toml --output-file dist/node-wheels/pylock.coire-node.toml >/dev/null
uv run --locked --package coire-node python scripts/stage-node-wheels.py \
  dist/node-wheels/pylock.coire-node.toml dist/node-wheels
echo "built: $(ls dist/*.whl | tr '\n' ' ')"
if [[ "$LOCAL_ONLY" -eq 1 ]]; then
  echo "locked wheels staged in dist/node-wheels; no Studio contacted"
  exit 0
fi
ssh "mcteer@${CONTROL_TARGET}" 'mkdir -p "$HOME/coire-stage/dist/node-wheels" "$HOME/coire-stage/apps/coire-node" "$HOME/coire-stage/deploy/launchd"' \
  || { echo "control path to $NODE is unreachable" >&2; exit 1; }
scp dist/*.whl "mcteer@${CONTROL_TARGET}:coire-stage/dist/"
scp dist/node-wheels/*.whl dist/node-wheels/requirements.txt \
  "mcteer@${CONTROL_TARGET}:coire-stage/dist/node-wheels/"
scp apps/coire-node/install.sh "mcteer@${CONTROL_TARGET}:coire-stage/apps/coire-node/"
scp apps/coire-node/install_runtime.py "mcteer@${CONTROL_TARGET}:coire-stage/apps/coire-node/"
scp deploy/launchd/com.coire.node.plist.template "mcteer@${CONTROL_TARGET}:coire-stage/deploy/launchd/"
echo "staged on ${NODE}:~/coire-stage — after creating /opt/coire, run:"
echo "  ~/coire-stage/apps/coire-node/install.sh --wheel-dir ~/coire-stage/dist"
