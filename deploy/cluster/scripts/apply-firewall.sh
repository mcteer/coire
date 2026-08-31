#!/usr/bin/env bash
# Render or apply Coire's minimum-peer macOS PF anchor. Dry-run is the default.
set -euo pipefail

MODE="${1:---check}"
case "$MODE" in --check|--apply) ;; *) echo "usage: $0 [--check|--apply]" >&2; exit 2;; esac

HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
case "$HOST" in
  coire-core) PEERS="coire-edge-a coire-edge-b"; PORTS="8180 4317" ;;
  coire-edge-a) PEERS="coire-core coire-edge-b.fabric"; PORTS="9400 9401" ;;
  coire-edge-b) PEERS="coire-core coire-edge-a.fabric"; PORTS="9400 9401" ;;
  *) echo "refusing unknown host: $HOST" >&2; exit 2 ;;
esac

RULES="$(mktemp)"
trap 'rm -f "$RULES"' EXIT
{
  echo "set skip on lo0"
  echo "block in log proto tcp to self"
  for peer in $PEERS; do
    for port in $PORTS; do
      echo "pass in quick proto tcp from $peer to self port $port"
    done
  done
} > "$RULES"

pfctl -n -f "$RULES"
if [[ "$MODE" == "--check" ]]; then
  cat "$RULES"
  exit 0
fi
[[ "$(id -u)" -eq 0 ]] || { echo "--apply requires sudo" >&2; exit 2; }
pfctl -a coire -f "$RULES"
pfctl -E 2>/dev/null || true
