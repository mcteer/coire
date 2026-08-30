#!/usr/bin/env bash
# Install the Coire node agent on a Studio.
#
# Everything lands under one prefix (/opt/coire) plus exactly two things outside it: the
# LaunchDaemon plist and one System-keychain item. Nothing general-purpose is installed: the
# Studios' compute is reserved for inference (FR-012a/b), and `uninstall.sh --dry-run`
# enumerates the whole footprint.
#
#   install.sh --wheel-dir DIR   install from wheels built on core by scripts/build-node-wheel.sh
#   install.sh --dry-run         print every path that would be created, change nothing
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
NODE_NAME="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wheel-dir) WHEEL_DIR="$2"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --prefix)    PREFIX="$2"; shift 2 ;;
    -h|--help)   sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

ENV_DIR="$PREFIX/envs/$AGENT_VERSION"
PLIST="/Library/LaunchDaemons/com.coire.node.plist"

say() { printf '  %s\n' "$*"; }

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "install.sh would create exactly:"
  say "$PREFIX/bin/uv"
  say "$PREFIX/python/**            (CPython $PYTHON_VERSION, provisioned by uv)"
  say "$ENV_DIR/**                  (agent virtualenv)"
  say "$PREFIX/envs/current         (symlink -> $ENV_DIR)"
  say "$PREFIX/log/                 (launchd stdout/stderr)"
  say "$PREFIX/models/               (model store: one directory per model slug)"
  say "$PREFIX/state/                (engine and job state; caches, never truth)"
  say "$PREFIX/hf-cache/             (Hugging Face metadata scratch; weights are not kept here)"
  say "$PLIST"
  say "keychain:coire-node-token    (System keychain, created by the operator)"
  say "keychain:coire-hf-token      (System keychain, created by the operator)"
  echo "and nothing under /usr/local, /opt/homebrew, or \$HOME."
  exit 0
fi

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
mkdir -p "$PREFIX/models" "$PREFIX/state/jobs" "$PREFIX/hf-cache"

# --- uv, confined to the prefix --------------------------------------------
if [[ ! -x "$PREFIX/bin/uv" ]]; then
  say "installing uv $UV_VERSION"
  UV_INSTALL_DIR="$PREFIX/bin" UV_NO_MODIFY_PATH=1 \
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | sh >/dev/null
else
  say "uv already present"
fi
export PATH="$PREFIX/bin:$PATH"
export UV_PYTHON_INSTALL_DIR="$PREFIX/python"

# --- pinned interpreter ----------------------------------------------------
# The Studios have Homebrew Python 3.14 and no 3.13; the constitution pins 3.13, so the agent
# brings its own rather than depending on what happens to be installed (research R5).
say "provisioning CPython $PYTHON_VERSION"
uv python install "$PYTHON_VERSION" >/dev/null

# --- agent virtualenv ------------------------------------------------------
say "creating $ENV_DIR"
uv venv --python "$PYTHON_VERSION" "$ENV_DIR" >/dev/null

if [[ -n "$WHEEL_DIR" ]]; then
  say "installing from wheels in $WHEEL_DIR"
  VIRTUAL_ENV="$ENV_DIR" uv pip install --python "$ENV_DIR/bin/python3" \
    "$WHEEL_DIR"/coire_core-*.whl "$WHEEL_DIR"/coire_node-*.whl >/dev/null
else
  echo "error: --wheel-dir is required." >&2
  echo "on core:  scripts/build-node-wheel.sh $NODE_NAME" >&2
  exit 2
fi

ln -sfn "$ENV_DIR" "$PREFIX/envs/current"
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
    "$TEMPLATE" > "$RENDERED"

echo
echo "the remaining steps need sudo:"
echo "  sudo cp $RENDERED $PLIST"
echo "  sudo chown root:wheel $PLIST && sudo chmod 644 $PLIST"
echo "  sudo launchctl bootout system/com.coire.node 2>/dev/null || true"
echo "  sudo launchctl bootstrap system $PLIST"
echo
echo "and the node token, in the SYSTEM keychain (the login keychain is locked at boot):"
echo "  sudo security add-generic-password -a coire -s coire-node-token \\"
echo "       -w '<token for $NODE_NAME>' /Library/Keychains/System.keychain"
echo
echo "and the Hugging Face token, which exists ONLY here - never on core (spec FR-005):"
echo "  sudo security add-generic-password -a coire -s coire-hf-token \\"
echo "       -w '<hf_...>' /Library/Keychains/System.keychain"
echo
echo "rendered plist left at: $RENDERED"
