#!/usr/bin/env bash
# Remove the Coire node agent, leaving the Studio as it was.
#
#   uninstall.sh --dry-run    list exactly what would be removed
#   uninstall.sh              remove it
#   uninstall.sh --keychain   also remove the System-keychain token
#
# The footprint is deliberately small enough to enumerate (FR-012b).
set -euo pipefail

PREFIX="${COIRE_PREFIX:-/opt/coire}"
PLIST="/Library/LaunchDaemons/com.coire.node.plist"
DRY_RUN=0
KEYCHAIN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)  DRY_RUN=1; shift ;;
    --keychain) KEYCHAIN=1; shift ;;
    --prefix)   PREFIX="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

targets=()
for p in "$PREFIX/bin" "$PREFIX/python" "$PREFIX/envs" "$PREFIX/log"; do
  [[ -e "$p" ]] && targets+=("$p")
done
[[ -e "$PLIST" ]] && targets+=("$PLIST")

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "uninstall.sh would remove:"
  for t in "${targets[@]:-}"; do [[ -n "$t" ]] && printf '  %s\n' "$t"; done
  [[ "$KEYCHAIN" -eq 1 ]] && printf '  keychain:coire-node-token (System keychain)\n'
  echo "and nothing else. $PREFIX itself is left for the operator."
  exit 0
fi

if [[ -e "$PLIST" ]]; then
  echo "stopping the service (needs sudo)"
  sudo launchctl bootout system/com.coire.node 2>/dev/null || true
  sudo rm -f "$PLIST"
fi

for p in "$PREFIX/bin" "$PREFIX/python" "$PREFIX/envs" "$PREFIX/log"; do
  [[ -e "$p" ]] && rm -rf "$p" && echo "removed $p"
done

if [[ "$KEYCHAIN" -eq 1 ]]; then
  sudo security delete-generic-password -s coire-node-token /Library/Keychains/System.keychain \
    >/dev/null 2>&1 && echo "removed the node token" || true
fi

echo "coire-node removed. $PREFIX kept (remove it yourself if you want it gone)."
