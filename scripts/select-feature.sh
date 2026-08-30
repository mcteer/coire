#!/usr/bin/env bash
# Point the Spec Kit workflow at a feature directory.
#
#   select-feature.sh                 show the current feature
#   select-feature.sh 001             select specs/001-*
#   select-feature.sh 001-model-...   select by full directory name
#
# The spec directories already exist (all 22 were generated up front), so
# create-new-feature.sh does not apply; this just rewrites .specify/feature.json.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POINTER="$REPO_ROOT/.specify/feature.json"

if [[ $# -eq 0 ]]; then
  current="$(python3 -c 'import json;print(json.load(open("'"$POINTER"'"))["feature_directory"])' 2>/dev/null || echo unset)"
  echo "current feature: $current"
  exit 0
fi

match=$(cd "$REPO_ROOT" && ls -d "specs/$1"* 2>/dev/null | head -1 || true)
[[ -n "$match" ]] || { echo "no spec directory matches '$1'" >&2; exit 1; }
match="${match%/}"
printf '{"feature_directory":"%s"}' "$match" > "$POINTER"
echo "feature -> $match"
ls "$REPO_ROOT/$match" | sed 's/^/  /'
