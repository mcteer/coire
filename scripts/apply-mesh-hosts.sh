#!/usr/bin/env bash
# Apply the Thunderbolt mesh name service to /etc/hosts as a managed block (ADR-0002).
#
# The mesh is unrouted and never reaches the UDM, so UniFi DNS cannot name it, and mDNS was
# measured non-deterministic here. deploy/cluster/hosts is the single source of mesh
# addresses; this script writes it into /etc/hosts between markers, replacing any prior block.
# That makes it config applied from deploy/, not hand-edited config on a node.
#
#   apply-mesh-hosts.sh            apply (needs sudo to write /etc/hosts)
#   apply-mesh-hosts.sh --check    exit 0 if current, 1 if out of date; writes nothing
#   apply-mesh-hosts.sh --remove   remove the managed block
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${COIRE_MESH_HOSTS:-$REPO_ROOT/deploy/cluster/hosts}"
TARGET="${COIRE_HOSTS_FILE:-/etc/hosts}"
BEGIN="# BEGIN coire-mesh"
END="# END coire-mesh"

[[ -r "$SOURCE" ]] || { echo "error: cannot read $SOURCE" >&2; exit 2; }

desired_block() {
  printf '%s\n' "$BEGIN"
  printf '# Managed by scripts/apply-mesh-hosts.sh from deploy/cluster/hosts — do not edit.\n'
  grep -vE '^\s*(#|$)' "$SOURCE"
  printf '%s\n' "$END"
}

without_block() {
  awk -v b="$BEGIN" -v e="$END" '
    $0 == b {skip=1; next}
    $0 == e {skip=0; next}
    !skip   {print}
  ' "$TARGET"
}

case "${1:-}" in
  --check)
    current="$(awk -v b="$BEGIN" -v e="$END" '$0==b{f=1} f{print} $0==e{f=0}' "$TARGET" 2>/dev/null || true)"
    if [[ "$current" == "$(desired_block)" ]]; then
      echo "mesh hosts: current"
      exit 0
    fi
    echo "mesh hosts: OUT OF DATE in $TARGET — run scripts/apply-mesh-hosts.sh" >&2
    exit 1
    ;;
  --remove)
    tmp="$(mktemp)"; without_block > "$tmp"
    cat "$tmp" > "$TARGET"; rm -f "$tmp"
    echo "mesh hosts: managed block removed from $TARGET"
    ;;
  ""|--apply)
    tmp="$(mktemp)"
    { without_block; desired_block; } > "$tmp"
    if diff -q "$tmp" "$TARGET" >/dev/null 2>&1; then
      echo "mesh hosts: already current"
    else
      echo "mesh hosts: updating $TARGET"
      diff <(cat "$TARGET") "$tmp" || true
      cat "$tmp" > "$TARGET"
      echo "mesh hosts: applied"
    fi
    rm -f "$tmp"
    ;;
  *)
    echo "usage: $0 [--apply|--check|--remove]" >&2
    exit 2
    ;;
esac
