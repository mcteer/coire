#!/usr/bin/env bash
# Verify (and optionally refresh) image digest pinning.
#
#   pin-images.sh --check     exit non-zero if any FROM or third-party image: lacks @sha256
#   pin-images.sh --resolve   print the current digest for every reference in images.lock
#
# Enforces contracts/image-policy.md rule 7 and the compose-topology requirement that
# third-party images are digest-pinned. First-party images are tag-controlled via COIRE_TAG
# and pinned to released digests by CI's publish job.
#
# bash 3.2 compatible: macOS ships no bash 4.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$REPO_ROOT/deploy/compose/images.lock"
COMPOSE="$REPO_ROOT/deploy/compose/compose.yaml"
MODE="${1:---check}"

fail=0

check_dockerfiles() {
  local df line
  while IFS= read -r df; do
    while IFS= read -r line; do
      case "$line" in
        [Ff][Rr][Oo][Mm][[:space:]][Ss][Cc][Rr][Aa][Tt][Cc][Hh]|[Ff][Rr][Oo][Mm][[:space:]][Ss][Cc][Rr][Aa][Tt][Cc][Hh][[:space:]]*) ;;
        *@sha256:*) ;;
        *) echo "unpinned FROM in ${df#$REPO_ROOT/}: $(echo "$line" | tr -s ' ')" >&2; fail=1 ;;
      esac
    done < <(grep -iE '^[[:space:]]*FROM[[:space:]]' "$df" || true)
  done < <(find "$REPO_ROOT" -name '*Dockerfile' -not -path '*/node_modules/*' -not -path '*/.venv/*' -not -path '*/.git/*' -not -path '*/tests/fixtures/*')
}

check_compose_third_party() {
  # Third-party images are referenced literally; first-party come from ${COIRE_REGISTRY}/...
  local line
  while IFS= read -r line; do
    case "$line" in
      *'${'*) ;;                       # first-party, tag-controlled
      *@sha256:*) ;;                   # pinned
      *) echo "unpinned image in compose.yaml: $(echo "$line" | tr -s ' ')" >&2; fail=1 ;;
    esac
  done < <(grep -E '^[[:space:]]*image:' "$COMPOSE" || true)
}

check_lock_present() {
  # Deployed images only. Test fixtures under tests/fixtures are pinned (rule 7 above) but are
  # never deployed, so they do not belong in the lock.
  local ref digest
  while IFS= read -r digest; do
    grep -qF "$digest" "$LOCK" || {
      echo "digest not recorded in images.lock: $digest" >&2; fail=1;
    }
  done < <(grep -ohE 'sha256:[a-f0-9]{64}' "$COMPOSE" \
             $(find "$REPO_ROOT" -name '*Dockerfile' \
                 -not -path '*/node_modules/*' -not -path '*/.venv/*' \
                 -not -path '*/tests/fixtures/*') \
           2>/dev/null | sort -u)
}

case "$MODE" in
  --check)
    check_dockerfiles
    check_compose_third_party
    check_lock_present
    if [[ "$fail" -ne 0 ]]; then
      echo "pin-images: FAILED — every base image and third-party image must carry a digest" >&2
      exit 1
    fi
    echo "pin-images: all image references are digest-pinned and recorded"
    ;;
  --resolve)
    grep -vE '^\s*(#|$)' "$LOCK" | while read -r name ref; do
      current="$(docker buildx imagetools inspect "${ref%@*}" --format '{{.Manifest.Digest}}' 2>/dev/null || echo '?')"
      printf '%-24s %s\n' "$name" "${ref%@*}@$current"
    done
    ;;
  *)
    echo "usage: $0 [--check|--resolve]" >&2
    exit 2
    ;;
esac
