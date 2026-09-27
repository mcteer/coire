# Identity and API-key operations

## What is running

`coire-api` verifies Cloudflare Access assertions and Coire keys independently of the edge. `coire-mcp`
uses the same database-backed key verifier for MCP traffic. PostgreSQL owns users, active entitlements,
Argon2id key hashes, rate windows, monthly token accumulators, and append-only audit rows. Only
`/ready`, `/health`, and the separately authenticated node-registration route are anonymous.

## Initial configuration

1. Set `CLOUDFLARE_ACCESS_ISSUER` to the exact `https://<team>.cloudflareaccess.com` issuer and
   `CLOUDFLARE_ACCESS_AUDIENCE` to the application audience used by the browser hostname.
2. Run `COIRE_BOOTSTRAP_ADMIN_EMAIL=you@example.com scripts/coire-secrets-init.sh`. The resulting
   Keychain item seeds a local admin row; the email is not an authenticator.
3. Run `deploy/compose/coire-up`. Confirm `/ready` and `/health` work without a credential, while
   `/api/docs` returns 401 outside an authenticated Access session.
4. Sign in through Access and call `/api/v1/me`; confirm the exact normalized local email and `admin`
   role before creating other users or keys.

The old `coire-admin-token` is rollback-only. Production compose leaves
`IDENTITY_LEGACY_ADMIN_ENABLED` unset/false. Enabling it recreates the closed Principle IV exception
and must be treated as an incident workaround with a recorded start/end time.

## Observe and diagnose

- Dashboard panels: Authentication outcomes, API key limit refusals, and authentication audit
  failures in the Coire Cluster dashboard.
- Metrics: `coire_auth_attempts_total`, `coire_key_limit_refusals_total`, and
  `coire_auth_audit_failures_total`. Labels are closed enums and contain no email, path, token, or key.
- Spans: `coire.auth.verify_access`, `coire.auth.verify_api_key`, `coire.auth.enforce_limits`, and
  `coire.audit.write`.
- Logs may identify a user/key UUID or non-secret prefix. A full `coire_...` key or JWT is a security
  defect; revoke the credential and preserve the affected log interval if one appears.

For an Access refusal, verify issuer/audience exactly, core clock, JWKS reachability, JWT expiry, and
that the normalized email has an active local row. An unknown signing `kid` triggers one immediate
JWKS refresh. Failure to refresh remains fail-closed.

For a key refusal, distinguish:

- 401 `credential_invalid`: bad/rotated/revoked key or inactive owner.
- 403 scope refusal: valid key lacks the named scope.
- 429 `rate_limit_exceeded`: wait for `Retry-After`/`retry_at`.
- 429 `monthly_quota_exceeded`: inspect budget, consumed tokens, and UTC reset time.

## Revoke and contain

Revoke a key with `DELETE /api/v1/admin/keys/{key_id}` from an active administrator identity. Rotation
through `POST .../rotate` is also immediate: copy the one-time replacement before leaving the response;
the previous secret stops working. Active streams re-check the key/user version at most once per
`CREDENTIAL_STREAM_RECHECK_S` (default one second), emit a terminal authentication event, and close.

Deactivate a user with `DELETE /api/v1/admin/users/{user_id}`; all their keys fail on the next check.
The API serializes admin changes and refuses any operation that would leave zero active admins. If an
operator loses the final external identity, change the Keychain bootstrap email to a controlled Access
identity and restart `coire-api`; the bootstrap action is audited.

## Audit integrity

Audit routes are read-only. Administrative mutation audits share their database transaction; failed
authentication audits use an independent best-effort transaction and fire a critical alert if writing
fails. Before/after/context JSON passes recursive key-name and credential-value redaction. Never paste a
presented key/JWT into an audit search, issue, or chat transcript; search by UUID/prefix/request id.

## Rollback

Prefer rolling both API and MCP images back without changing schema. If identity behavior itself must be
disabled, stop public ingress first, explicitly enable the legacy bearer only for the rollback window,
and document the constitutional exception. Downgrade migration `0009_identity` only after exporting any
needed user/key metadata and accepting that users, entitlements, key hashes, counters, and enriched audit
columns will be destroyed. Never downgrade while public ingress remains open.
