#!/usr/bin/env bash
# Restore legacy listener selection without deleting v2 database observations.
set -euo pipefail

case "${1:-}" in --check) echo "rollback targets: coire-core coire-edge-a coire-edge-b; database untouched"; exit;; -h|--help) echo "usage: $0 --apply"; exit;; --apply) ;; *) echo "explicit --apply required" >&2; exit 2;; esac

for node in coire-edge-a coire-edge-b; do
  ssh "mcteer@$node" \
    'sudo /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:LEGACY_NETWORK_MODE true" /Library/LaunchDaemons/com.coire.node.plist && sudo launchctl kickstart -k system/com.coire.node'
done
echo "legacy listener mode selected; restore the recoverable core/Studio cable and managed"
echo "legacy hosts mapping per docs/runbooks/network-fabrics.md. Database schema is unchanged."
