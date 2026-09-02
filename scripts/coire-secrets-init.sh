#!/usr/bin/env bash
# Create the three Keychain items coire-up needs, once, on core.
#
#   coire-secrets-init.sh            create any that are missing
#   coire-secrets-init.sh --force    replace existing items
#   coire-secrets-init.sh --show-node-tokens   print the per-node tokens to store on each Studio
#
# Creates: coire-postgres-password, coire-key-signing-secret, coire-node-tokens,
#          coire-admin-token (rollback-only), coire-bootstrap-admin-email, and the isolated
#          coire-ops service credential.
# Does NOT create the Hugging Face token: that lives only in each Studio's System keychain.
#
# The generated values are written straight into the login Keychain and never echoed, except
# the node tokens, which the operator must copy to each Studio's SYSTEM keychain.
set -euo pipefail

FORCE=0
SHOW=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --show-node-tokens) SHOW=1 ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

create() {  # $1 = service name, $2 = value
  if security find-generic-password -s "$1" >/dev/null 2>&1; then
    if [[ "$FORCE" -eq 1 ]]; then
      security delete-generic-password -s "$1" >/dev/null 2>&1 || true
    else
      echo "  $1: already present (use --force to replace)"
      return 0
    fi
  fi
  security add-generic-password -a coire -s "$1" -w "$2" -U
  echo "  $1: created"
}

if [[ "$SHOW" -eq 1 ]]; then
  tokens="$(security find-generic-password -w -s coire-node-tokens 2>/dev/null || echo '{}')"
  echo "Store each of these in the SYSTEM keychain on the matching Studio:"
  python3 -c "
import json,sys
for name, tok in json.loads(sys.argv[1]).items():
    print(f'  {name}:')
    print(f'    sudo security add-generic-password -a coire -s coire-node-token \\\\')
    print(f'         -w {tok!r} /Library/Keychains/System.keychain')
" "$tokens"
  exit 0
fi

echo "creating Coire secrets in the login Keychain:"
create coire-postgres-password "$(openssl rand -base64 32)"
create coire-key-signing-secret "$(openssl rand -base64 48)"
create coire-admin-token "$(openssl rand -base64 32)"
create coire-ops-service-token "coire_ops_$(openssl rand -hex 32)"
create coire-node-tokens "$(python3 -c '
import json, secrets
print(json.dumps({n: secrets.token_urlsafe(32) for n in ("coire-edge-a", "coire-edge-b")}))
')"
if [[ -n "${COIRE_BOOTSTRAP_ADMIN_EMAIL:-}" ]]; then
  normalized_email="$(python3 -c 'import sys; value=sys.argv[1].strip().casefold(); assert "@" in value and "." in value.partition("@")[2], "invalid bootstrap email"; print(value)' "$COIRE_BOOTSTRAP_ADMIN_EMAIL")"
  create coire-bootstrap-admin-email "$normalized_email"
elif ! security find-generic-password -s coire-bootstrap-admin-email >/dev/null 2>&1; then
  echo "missing bootstrap admin email; rerun with COIRE_BOOTSTRAP_ADMIN_EMAIL=you@example.com" >&2
  exit 2
fi

echo
echo "next: $(dirname "${BASH_SOURCE[0]}")/coire-secrets-init.sh --show-node-tokens"
echo "      to get the per-node tokens for each Studio's System keychain."
echo
echo "the Hugging Face token is NOT created here: it belongs only in each Studio's SYSTEM"
echo "keychain (spec FR-005), never on core. On each Studio, once:"
echo "  sudo security add-generic-password -a coire -s coire-hf-token \\"
echo "       -w '<hf_...>' /Library/Keychains/System.keychain"
