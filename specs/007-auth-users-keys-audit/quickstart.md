# Quickstart: Auth, Users, API Keys, and Audit

## Automated acceptance

```bash
uv sync --all-packages
uv run ruff format --check
uv run ruff check
uv run mypy packages apps
uv run pytest -q apps/coire-api/tests packages/coire-core/tests
COIRE_INTEGRATION=1 uv run pytest -q tests/integration/test_identity_integration.py
uv run python -m coire_api.openapi --check
```

The integration scenario starts the isolated compose project, seeds a configured administrator,
validates a locally signed Access-style JWT against a test JWKS endpoint, creates and uses a scoped
key, distinguishes rate/quota errors, rotates and revokes the key, deactivates a user, checks the
last-admin invariant, sweeps all routes anonymously, and inspects audit rows for secret leakage.

## Operator configuration

Before deploying, add the Cloudflare team issuer, Access audience, and initial administrator email to
the core Keychain-backed secret material documented in `deploy/compose/README.md`, then regenerate the
mounted secrets. Do not paste an Access JWT or Coire key into a plist, compose file, shell history, or
the repository.

## Manual smoke test

1. Open the browser hostname through Cloudflare Access as the configured admin; `/api/v1/me` returns
   the matching local admin.
2. Create a chat-scoped key and copy its secret from the one-time response.
3. Confirm the key can call `/v1/models` but receives 403 from an admin route.
4. Rotate it and confirm the old secret receives 401 on the next request.
5. Revoke the new key and confirm it immediately receives 401.
6. Inspect the audit API and logs for the key value; no full or secret component may appear.

## Rollback

Stop ingress, roll API and MCP images back together, and run the reversible `0009` downgrade only if
identity rows may be discarded. Restoring the old universal admin bearer reopens the Principle IV
exception and is emergency-only; the full rollback procedure and exposure warning live in
`docs/runbooks/identity.md`.
