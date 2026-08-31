#!/usr/bin/env bash
# Apply the Studio-only hosts mapping after a successful measured preflight.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
STATE_DIR="${COIRE_PREFLIGHT_STATE_DIR:-$HOME/.coire/preflight}"
STAMP="$STATE_DIR/fabrics.ok"

case "${1:-}" in --check) test -s "$STAMP" && echo "ready: $STAMP"; exit;; -h|--help) echo "usage: sudo $0 --apply"; exit;; --apply) ;; *) echo "explicit --apply required" >&2; exit 2;; esac
[[ "$(id -u)" -eq 0 ]] || { echo "--apply requires sudo" >&2; exit 2; }
[[ -s "$STAMP" ]] || { echo "full preflight stamp missing: $STAMP" >&2; exit 1; }

HOSTS="$ROOT/deploy/cluster/hosts"
grep -q 'coire-edge-a.fabric' "$HOSTS"
grep -q 'coire-edge-b.fabric' "$HOSTS"
! grep -q 'coire-core' "$HOSTS"

for node in coire-edge-a coire-edge-b; do
  ssh "mcteer@$node" 'sudo mkdir -p /etc/coire'
  scp "$HOSTS" "mcteer@$node:/tmp/coire-fabric-hosts"
  ssh "mcteer@$node" 'sudo install -o root -g wheel -m 0644 /tmp/coire-fabric-hosts /etc/coire/fabric-hosts && rm /tmp/coire-fabric-hosts'
done

echo "software fabric mapping applied. Disconnect core Thunderbolt only after the verification"
echo "steps in docs/runbooks/network-fabrics.md pass. This script does not alter cables."
