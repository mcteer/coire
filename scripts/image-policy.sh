#!/usr/bin/env bash
# Enforce contracts/image-policy.md rules 1-7 against a built image.
#
#   image-policy.sh <image> [dockerfile]
#
# Every failure prints "policy: <rule> in <image>: <detail>" to stderr. All rules are checked
# before exiting so one run reports everything wrong, not just the first thing.
#
# Applies to first-party and CI-derived images. Third-party images pulled unmodified are
# scanned and digest-pinned but exempt from rules 1-3 (research R3; spec FR-004 as amended).
set -uo pipefail

IMAGE="${1:?usage: image-policy.sh <image> [dockerfile]}"
DOCKERFILE="${2:-}"
FAILED=0

fail() { echo "policy: $1 in ${IMAGE}: $2" >&2; FAILED=1; }
pass() { printf '  ok  %s\n' "$1"; }

# --- rule 1: no shell ------------------------------------------------------
shell_found=""
for sh in /bin/sh /bin/bash /bin/ash /bin/dash /usr/bin/sh /usr/bin/bash; do
  if docker run --rm --entrypoint "$sh" "$IMAGE" -c true >/dev/null 2>&1; then
    shell_found="$sh"; break
  fi
done
CID="$(docker create "$IMAGE" 2>/dev/null)"
if [[ -n "$CID" ]]; then
  FS="$(docker export "$CID" 2>/dev/null | tar -t 2>/dev/null)"
  docker rm "$CID" >/dev/null 2>&1
else
  FS=""
fi
fs_shell="$(printf '%s\n' "$FS" | grep -E '^(usr/)?(bin|sbin)/(sh|bash|ash|dash)$' | head -1)"
if [[ -n "$shell_found" || -n "$fs_shell" ]]; then
  fail "shell present" "${shell_found:-$fs_shell}"
else
  pass "rule 1: no shell"
fi

# --- rule 2: no package manager -------------------------------------------
pkg="$(printf '%s\n' "$FS" | grep -E '^(usr/)?(bin|sbin)/(apt|apt-get|apk|dpkg|yum|dnf|pip|pip3)$|^usr/local/bin/(pip|pip3)$' | head -1)"
if [[ -n "$pkg" ]]; then fail "package manager present" "$pkg"; else pass "rule 2: no package manager"; fi

# --- rule 3: non-root ------------------------------------------------------
USER_CFG="$(docker image inspect "$IMAGE" --format '{{.Config.User}}' 2>/dev/null)"
if [[ -z "$USER_CFG" || "$USER_CFG" == "root" || "$USER_CFG" == 0* ]]; then
  fail "runs as root" "Config.User='${USER_CFG:-<empty>}'"
else
  pass "rule 3: non-root (${USER_CFG})"
fi

# --- rule 4: read-only rootfs compatible -----------------------------------
# Start with a read-only root and a tmpfs; the container must not die from an unwritable fs.
RO_CID="$(docker run -d --read-only --tmpfs /tmp --tmpfs /run "$IMAGE" 2>/dev/null)"
if [[ -n "$RO_CID" ]]; then
  for _ in $(seq 1 40); do
    st="$(docker inspect "$RO_CID" --format '{{.State.Status}}' 2>/dev/null)"
    [[ "$st" == "exited" ]] && break
    hs="$(docker inspect "$RO_CID" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null)"
    [[ "$hs" == "healthy" ]] && break
  done
  code="$(docker inspect "$RO_CID" --format '{{.State.ExitCode}}' 2>/dev/null)"
  logs="$(docker logs "$RO_CID" 2>&1 | grep -iE 'read-only file system|permission denied' | head -1)"
  docker rm -f "$RO_CID" >/dev/null 2>&1
  # Only filesystem evidence counts. A one-shot image legitimately exits non-zero when its
  # dependencies are absent (coire-migrate without a database), which says nothing about
  # whether it needs a writable root.
  if [[ -n "$logs" ]]; then
    fail "requires writable rootfs" "$logs"
  else
    pass "rule 4: read-only compatible (exit=${code})"
  fi
else
  fail "requires writable rootfs" "container would not start with --read-only"
fi

# --- rule 5: arm64 present -------------------------------------------------
ARCH="$(docker image inspect "$IMAGE" --format '{{.Architecture}}' 2>/dev/null)"
if [[ "$ARCH" != "arm64" ]]; then
  fail "no linux/arm64 variant" "architecture=${ARCH:-unknown}"
else
  pass "rule 5: linux/arm64"
fi

# --- rule 6: exec-form entrypoint -----------------------------------------
EP="$(docker image inspect "$IMAGE" --format '{{json .Config.Entrypoint}}' 2>/dev/null)"
if [[ "$EP" == "null" || -z "$EP" ]]; then
  fail "entrypoint is not exec-form" "no ENTRYPOINT set"
elif printf '%s' "$EP" | grep -qE '"(/bin/)?(sh|bash)"'; then
  fail "entrypoint is not exec-form" "shell-form entrypoint: $EP"
else
  pass "rule 6: exec-form entrypoint"
fi

# --- rule 7: pinned bases --------------------------------------------------
if [[ -n "$DOCKERFILE" ]]; then
  if [[ ! -r "$DOCKERFILE" ]]; then
    fail "unpinned FROM" "cannot read $DOCKERFILE"
  else
    unpinned="$(grep -iE '^\s*FROM\s' "$DOCKERFILE" | grep -v '@sha256:' | head -1)"
    if [[ -n "$unpinned" ]]; then
      fail "unpinned FROM" "$(printf '%s' "$unpinned" | tr -s ' ')"
    else
      pass "rule 7: all FROM digest-pinned"
    fi
  fi
fi

if [[ "$FAILED" -ne 0 ]]; then
  echo "IMAGE POLICY FAILED: $IMAGE" >&2
  exit 1
fi
echo "image policy passed: $IMAGE"
