#!/usr/bin/env bash
# Non-mutating gate for the control/data fabric cutover.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
STATE_DIR="${COIRE_PREFLIGHT_STATE_DIR:-$HOME/.coire/preflight}"
PROBES="${COIRE_PREFLIGHT_PROBES:-200}"

usage() { echo "usage: $0 [--software-only]"; }
MODE=full
case "${1:-}" in "") ;; --software-only) MODE=software;; -h|--help) usage; exit 0;; *) usage >&2; exit 2;; esac

[[ "$(scutil --get LocalHostName 2>/dev/null || hostname -s)" == coire-core ]] || {
  echo "preflight must run on coire-core" >&2; exit 2;
}

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
STAMP="$STATE_DIR/fabrics.ok"
rm -f "$STAMP"

for host in coire-core.lab coire-edge-a.lab coire-edge-b.lab; do
  dscacheutil -q host -a name "$host" | grep -q 'ip_address:' || {
    echo "control DNS failed: $host" >&2; exit 1;
  }
done

TOKENS="$(security find-generic-password -w -s coire-node-tokens)"
SAMPLES="$(mktemp)"
trap 'rm -f "$SAMPLES"' EXIT
for node in coire-edge-a coire-edge-b; do
  token="$(printf '%s' "$TOKENS" | python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$node")"
  : > "$SAMPLES"
  i=0
  while [[ "$i" -lt "$PROBES" ]]; do
    curl --fail --silent --show-error --output /dev/null \
      --header "Authorization: Bearer $token" \
      --write-out '%{time_total}\n' "http://$node.lab:9400/node/health" >> "$SAMPLES"
    i=$((i + 1))
  done
  p95="$(sort -n "$SAMPLES" | awk -v n="$PROBES" 'NR==int(n*.95+0.999){printf "%.3f",$1*1000}')"
  awk -v value="$p95" 'BEGIN{exit !(value <= 50)}' || {
    echo "$node control latency p95 ${p95}ms exceeds 50ms" >&2; exit 1;
  }
  echo "$node control latency p95=${p95}ms"
done

for node in coire-edge-a coire-edge-b; do
  ssh -o BatchMode=yes "mcteer@$node" 'test "$(scutil --get LocalHostName)" = "'"$node"'"'
done

"$ROOT/deploy/cluster/distributed_config.sh" "$STATE_DIR/jaccl-hostfile.json" >/dev/null
"$ROOT/deploy/cluster/scripts/apply-firewall.sh" --check >/dev/null

if [[ "$MODE" == full ]]; then
  : "${COIRE_TINY_MODEL_PROBE:?set COIRE_TINY_MODEL_PROBE to the executable tiny-model gate}"
  : "${COIRE_TOOL_LOOP_PROBE:?set COIRE_TOOL_LOOP_PROBE to the executable tool-loop gate}"
  : "${COIRE_IMAGE_RESULT_PROBE:?set COIRE_IMAGE_RESULT_PROBE to the executable image-result gate}"
  "$COIRE_TINY_MODEL_PROBE"
  "$COIRE_TOOL_LOOP_PROBE"
  "$COIRE_IMAGE_RESULT_PROBE"
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "$STAMP"
chmod 600 "$STAMP"
echo "preflight passed: $STAMP"
