#!/usr/bin/env bash
# Install the Coire node agent on a Studio.
#
# Everything lands under one prefix (/opt/coire) plus the LaunchDaemon plist and System-keychain
# items. Nothing general-purpose is installed: the
# Studios' compute is reserved for inference (FR-012a/b), and `uninstall.sh --dry-run`
# enumerates the whole footprint.
#
#   install.sh --wheel-dir DIR   install from wheels built on core by scripts/build-node-wheel.sh
#   install.sh --dry-run         print every path that would be created, change nothing
#   install.sh --wheel-dir DIR --stage-only   verify an immutable candidate without activating it
#
# One-time prerequisite, run by the operator:
#   sudo mkdir -p /opt/coire && sudo chown "$USER" /opt/coire
#
# bash 3.2 compatible: macOS ships no bash 4.
set -euo pipefail

PREFIX="${COIRE_PREFIX:-/opt/coire}"
UV_VERSION="0.12.7"
PYTHON_VERSION="3.13"
AGENT_VERSION="0.2.0"
WHEEL_DIR=""
DRY_RUN=0
STAGE_ONLY=0
BOOTSTRAP_REQUEST=""
NODE_NAME="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
RUN_AGENT_IMAGE="${COIRE_RUN_AGENT_IMAGE:-}"
RUN_RELAY_IMAGE="${COIRE_RUN_RELAY_IMAGE:-}"
FAILOVER_IMAGE="${COIRE_FAILOVER_IMAGE:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wheel-dir) WHEEL_DIR="$2"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --stage-only) STAGE_ONLY=1; shift ;;
    --bootstrap-upgrades) BOOTSTRAP_REQUEST="$2"; shift 2 ;;
    --prefix)    PREFIX="$2"; shift 2 ;;
    -h|--help)   sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

ENV_DIR="$PREFIX/envs/$AGENT_VERSION-<locked-wheel-digest>"
PLIST="/Library/LaunchDaemons/com.coire.node.plist"

say() { printf '  %s\n' "$*"; }

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "install.sh would create exactly:"
  say "$PREFIX/bin/uv"
  say "$PREFIX/python/**            (CPython $PYTHON_VERSION, provisioned by uv)"
  say "$PREFIX/runtimes/**          (private retained interpreter trees and content receipts)"
  say "$ENV_DIR/**                  (agent virtualenv)"
  say "$PREFIX/envs/current         (symlink -> $ENV_DIR)"
  say "$PREFIX/log/                 (launchd stdout/stderr)"
  say "$PREFIX/models/               (model store: one directory per model slug)"
  say "$PREFIX/state/                (engine and job state; caches, never truth)"
  say "$PREFIX/hf-cache/             (Hugging Face metadata scratch; weights are not kept here)"
  say "$PREFIX/workspaces/           (platform-prepared run workspaces)"
  say "$PLIST"
  if [[ -n "$BOOTSTRAP_REQUEST" ]]; then
    say "$PREFIX/upgrade-bootstrap/ (verified retained source recovery metadata)"
    say "/Library/LaunchDaemons/com.coire.upgrade-guard.plist"
  fi
  say "keychain:coire-node-token    (System keychain, created by the operator)"
  say "keychain:coire-node-registration-token (one-time, System keychain)"
  say "keychain:coire-hf-token      (System keychain, created by the operator)"
  echo "and nothing under /usr/local, /opt/homebrew, or \$HOME."
  exit 0
fi
if [[ -n "$BOOTSTRAP_REQUEST" ]]; then
  if [[ "$STAGE_ONLY" -eq 1 || -n "$WHEEL_DIR" ]]; then
    echo "error: bootstrap is a separate operation on an already installed source." >&2
    exit 2
  fi
  case "$NODE_NAME" in
    coire-edge-a|coire-edge-b) ;;
    *) echo "error: managed recovery bootstrap requires a registered Studio." >&2; exit 2 ;;
  esac
  BOOTSTRAP_PYTHON="$PREFIX/envs/current/bin/python3"
  GUARD_TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/deploy/launchd/com.coire.upgrade-guard.plist.template"
  if [[ ! -x "$BOOTSTRAP_PYTHON" || ! -r "$BOOTSTRAP_REQUEST" || ! -r "$GUARD_TEMPLATE" ]]; then
    echo "error: bootstrap requires the installed source, registration request and guard template." >&2
    exit 2
  fi
  export NODE_NAME
  export UPGRADE_ENVIRONMENT_ROOT="$PREFIX/envs"
  export UPGRADE_BOOTSTRAP_ROOT="$PREFIX/upgrade-bootstrap"
  export UPGRADE_PREPARED_DIR="$PREFIX/upgrades/prepared"
  "$BOOTSTRAP_PYTHON" -I -B -m coire_node.upgrade_bootstrap < "$BOOTSTRAP_REQUEST"
  GUARD_RENDERED="$(mktemp)"
  "$BOOTSTRAP_PYTHON" -I -B -m coire_node.upgrade_bootstrap \
    --render-guard-plist "$GUARD_TEMPLATE" --user "$(whoami)" > "$GUARD_RENDERED"
  NODE_RENDERED="$(mktemp)"
  "$BOOTSTRAP_PYTHON" -I -B -m coire_node.upgrade_bootstrap \
    --render-node-plist "$PLIST" --user "$(whoami)" > "$NODE_RENDERED"
  echo "verified recovery metadata; install the retained guard before enabling managed node startup:"
  echo "  sudo install -o root -g wheel -m 644 $GUARD_RENDERED /Library/LaunchDaemons/com.coire.upgrade-guard.plist"
  echo "  sudo launchctl bootstrap system /Library/LaunchDaemons/com.coire.upgrade-guard.plist"
  echo "after the guard is ready and existing node work drains, install the managed node plist:"
  echo "  sudo install -o root -g wheel -m 644 $NODE_RENDERED $PLIST"
  echo "  # Use the documented drained node restart; do not terminate active user work."
  echo "rendered guard plist left at: $GUARD_RENDERED"
  echo "rendered managed node plist left at: $NODE_RENDERED"
  exit 0
fi
if [[ "$STAGE_ONLY" -eq 0 && ( -e "$PREFIX/upgrade-bootstrap" || -L "$PREFIX/upgrade-bootstrap" ) ]]; then
  echo "error: this Studio has managed recovery ownership; activate through the admin upgrade API." >&2
  echo "use --stage-only to prepare an immutable candidate without changing the selected runtime." >&2
  exit 2
fi
for IMAGE in "$RUN_AGENT_IMAGE" "$RUN_RELAY_IMAGE" "$FAILOVER_IMAGE"; do
  if [[ -n "$IMAGE" && ! "$IMAGE" =~ ^[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}$ ]]; then
    echo "error: run images must be digest-pinned references" >&2
    exit 2
  fi
done

# --- preconditions ---------------------------------------------------------
if [[ ! -d "$PREFIX" ]]; then
  echo "error: $PREFIX does not exist." >&2
  echo "run once, as the operator:  sudo mkdir -p $PREFIX && sudo chown \"\$USER\" $PREFIX" >&2
  exit 2
fi
if [[ ! -w "$PREFIX" ]]; then
  echo "error: $PREFIX is not writable by $(whoami); chown it to this account." >&2
  exit 2
fi

echo "installing coire-node $AGENT_VERSION into $PREFIX"
mkdir -p "$PREFIX/bin" "$PREFIX/python" "$PREFIX/log" "$PREFIX/envs"
# Feature 001: the model store, the agent's own state, and a metadata scratch cache. Weights
# are written straight into the store (snapshot_download --local-dir), so hf-cache stays small.
mkdir -p "$PREFIX/models" "$PREFIX/state/jobs" "$PREFIX/hf-cache" "$PREFIX/workspaces"

# --- uv, confined to the prefix --------------------------------------------
if [[ ! -x "$PREFIX/bin/uv" ]]; then
  say "installing uv $UV_VERSION"
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" \
    | UV_UNMANAGED_INSTALL="$PREFIX/bin" sh >/dev/null
else
  say "uv already present"
fi
export PATH="$PREFIX/bin:$PATH"
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"
export UV_PYTHON_BIN_DIR="$PREFIX/bin"
export UV_NO_CACHE=1

# --- pinned interpreter ----------------------------------------------------
# The Studios have Homebrew Python 3.14 and no 3.13; the constitution pins 3.13, so the agent
# brings its own rather than depending on what happens to be installed (research R5).
say "provisioning CPython $PYTHON_VERSION"
uv python install "$PYTHON_VERSION" >/dev/null
NODE_PYTHON="$(uv python find "$PYTHON_VERSION")"
# Retain the complete interpreter before adding Coire's distinct signed binary.
# Neither the virtualenv nor a rollback depends on uv's mutable managed install.
NODE_PYTHON="$("$NODE_PYTHON" "$(dirname "${BASH_SOURCE[0]}")/install_runtime.py" \
  --prepare-python "$NODE_PYTHON" "$PREFIX/runtimes")"
INTERPRETER_ROOT="$(dirname "$(dirname "$NODE_PYTHON")")"
say "using retained Studio runtime $NODE_PYTHON"

# --- exact locked wheel graph ---------------------------------------------
if [[ -z "$WHEEL_DIR" ]]; then
  echo "error: --wheel-dir is required." >&2
  echo "on core:  scripts/build-node-wheel.sh $NODE_NAME" >&2
  exit 2
fi
WHEELHOUSE="$WHEEL_DIR/node-wheels"
REQUIREMENTS="$WHEELHOUSE/requirements.txt"
CORE_WHEELS=("$WHEEL_DIR"/coire_core-*.whl)
NODE_WHEELS=("$WHEEL_DIR"/coire_node-*.whl)
if [[ ! -r "$REQUIREMENTS" || ! -d "$WHEELHOUSE" || \
      ${#CORE_WHEELS[@]} -ne 1 || ${#NODE_WHEELS[@]} -ne 1 || \
      ! -f "${CORE_WHEELS[0]}" || ! -f "${NODE_WHEELS[0]}" ]]; then
  echo "error: stage one core/node wheel and the locked node-wheels directory" >&2
  exit 2
fi
# The executable identity is part of the immutable environment, not just the wheel graph.
# An older environment with the same wheels may still point at uv's shared Python.
LOCK_ID="$("$NODE_PYTHON" "$(dirname "${BASH_SOURCE[0]}")/install_runtime.py" \
  --runtime-input-id "$INTERPRETER_ROOT" "$REQUIREMENTS" "${CORE_WHEELS[0]}" "${NODE_WHEELS[0]}" "$NODE_PYTHON")"
ENV_DIR="$PREFIX/envs/$AGENT_VERSION-$LOCK_ID"
STAGING="$ENV_DIR.staging.$$"
cleanup_stage() { [[ ! -e "$STAGING" ]] || rm -rf "$STAGING"; }
trap cleanup_stage EXIT

if [[ -e "$ENV_DIR" && ! -x "$ENV_DIR/bin/python3" ]]; then
  echo "error: immutable node environment is incomplete: $ENV_DIR" >&2
  exit 2
fi
if [[ ! -x "$ENV_DIR/bin/python3" ]]; then
  say "creating isolated $ENV_DIR from locked wheels"
  # uv 0.12.7 canonicalizes a renamed executable inside its configured managed
  # install root back to python3.13. Provisioning is complete; make this explicit
  # interpreter request without that managed-install shortcut, then verify it.
  env -u UV_PYTHON_INSTALL_DIR -u UV_PYTHON_BIN_DIR \
    uv venv --relocatable --python "$NODE_PYTHON" "$STAGING" >/dev/null
  uv pip sync --link-mode copy --python "$STAGING/bin/python3" --require-hashes --no-index \
    --find-links "$WHEELHOUSE" "$REQUIREMENTS" >/dev/null
  uv pip install --link-mode copy --python "$STAGING/bin/python3" --no-index --no-deps \
    "${CORE_WHEELS[0]}" "${NODE_WHEELS[0]}" >/dev/null
  uv pip check --python "$STAGING/bin/python3" >/dev/null
  SMOKE_SOURCE="$STAGING"
else
  say "checking existing immutable $ENV_DIR"
  SMOKE_SOURCE="$ENV_DIR"
fi

# A failed import or CLI flag check leaves the previous active environment untouched.
if [[ "$STAGE_ONLY" -eq 1 ]]; then
  "$SMOKE_SOURCE/bin/python3" "$(dirname "${BASH_SOURCE[0]}")/install_runtime.py" \
    --seal-stage "$SMOKE_SOURCE" "$ENV_DIR" "$NODE_PYTHON"
  say "verified immutable candidate $ENV_DIR; active link and launchd service unchanged"
  exit 0
fi
"$SMOKE_SOURCE/bin/python3" "$(dirname "${BASH_SOURCE[0]}")/install_runtime.py" \
  --seal-stage "$SMOKE_SOURCE" "$ENV_DIR" "$NODE_PYTHON" "$PREFIX/envs/current"

say "flipped $PREFIX/envs/current -> $ENV_DIR"

# --- launchd ---------------------------------------------------------------
TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/deploy/launchd/com.coire.node.plist.template"
if [[ ! -r "$TEMPLATE" ]]; then
  echo "warning: plist template not found at $TEMPLATE; skipping service install" >&2
  echo "agent installed but not started." >&2
  exit 0
fi

RENDERED="$(mktemp)"
sed -e "s|__PREFIX__|$PREFIX|g" -e "s|__USER__|$(whoami)|g" -e "s|__NODE_NAME__|$NODE_NAME|g" \
    -e "s|__FAILOVER_IMAGE__|$FAILOVER_IMAGE|g" \
    -e "s|__RUN_AGENT_IMAGE__|$RUN_AGENT_IMAGE|g" \
    -e "s|__RUN_RELAY_IMAGE__|$RUN_RELAY_IMAGE|g" \
    "$TEMPLATE" > "$RENDERED"

echo
echo "the remaining steps need sudo:"
echo "  sudo cp $RENDERED $PLIST"
echo "  sudo chown root:wheel $PLIST && sudo chmod 644 $PLIST"
echo "  sudo launchctl bootout system/com.coire.node 2>/dev/null || true"
echo "  # Wait for launchd to finish removing the old registration before bootstrap."
echo '  for i in $(seq 1 30); do launchctl print system/com.coire.node >/dev/null 2>&1 || break; sleep 1; done'
echo '  if launchctl print system/com.coire.node >/dev/null 2>&1; then echo "coire-node still registered" >&2; exit 1; fi'
echo "  sudo launchctl bootstrap system $PLIST"
echo
echo "and the node token, in the SYSTEM keychain (the login keychain is locked at boot):"
echo "  sudo security add-generic-password -a coire -s coire-node-token \\"
echo "       -w '<token for $NODE_NAME>' /Library/Keychains/System.keychain"
echo "the separately issued one-time registration token goes in the SYSTEM keychain:"
echo "  sudo security add-generic-password -a coire -s coire-node-registration-token \\"
echo "       -U -w '<issued token>' /Library/Keychains/System.keychain"
echo "  see docs/runbooks/instances.md for a clipboard-based command"
echo
echo "and the Hugging Face token, which exists ONLY here - never on core (spec FR-005):"
echo "  sudo security add-generic-password -a coire -s coire-hf-token \\"
echo "       -w '<hf_...>' /Library/Keychains/System.keychain"
echo
echo "rendered plist left at: $RENDERED"
