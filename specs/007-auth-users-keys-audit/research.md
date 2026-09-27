# Research: Auth, Users, API Keys, and Audit

## R1 — Verify Cloudflare assertions locally

**Decision**: Accept `Cf-Access-Jwt-Assertion` only for browser requests. Fetch configured team JWKS
with `httpx`, cache it for a bounded interval, select by `kid`, and use PyJWT to verify an RS256
signature, exact issuer, configured audience, `exp`, `nbf`, `iat`, and 60-second leeway. Require a
normalized email claim and match it to an active local user. An unknown key triggers one refresh;
network failure never becomes acceptance.

**Rationale**: PyJWT exposes explicit issuer/audience checks, algorithm allowlisting, leeway, and JWK
construction. A bounded JWKS cache supports edge key rotation without a network call per request.

**Rejected**: trusted forwarding headers can be forged when the edge is bypassed; introspection adds
an online dependency; accepting the token-selected algorithm permits confusion attacks.

## R2 — Hash API keys with Argon2id

**Decision**: Issue `coire_<12-char-prefix>_<43-char-secret>`. Store an indexed prefix and Argon2id
encoded hash only. Query every active candidate for a prefix and verify each candidate, so a collision
never authenticates by prefix alone. Return the full secret only from create/rotate. Pin
`argon2-cffi==25.1.0` (MIT), configured as Argon2id with random salts, 19 MiB memory, two iterations,
and one lane (the OWASP minimum profile). This retains a memory-hard verifier while fitting the
gateway's 20 ms authentication budget for a 256-bit generated secret. Pin
`PyJWT[crypto]==2.13.0` (MIT) for JWT/JWK verification.

**Rejected**: reversible encryption makes all keys recoverable after a database compromise; fast
hashes make offline guessing cheap; bcrypt's input truncation adds no benefit for generated secrets.

## R3 — Central authentication, explicit authorization

**Decision**: Middleware authenticates every API request except documented health/readiness paths and
attaches a typed principal. Existing principal/admin dependencies enforce roles, and scope dependencies
protect API-key routes. A route-table test fails when a new application route lacks protection. MCP
readiness stays anonymous; future `/mcp` traffic requires `mcp` scope.

**Rationale**: a central seam prevents routers from accidentally omitting authentication while
route-level authorization remains reviewable and represented in OpenAPI.

## R4 — PostgreSQL owns limits and budgets

**Decision**: Persist per-key UTC fixed-minute request windows and increment with an atomic conditional
upsert. Persist monthly usage in `[month_start, next_month_start)` UTC rows. The gateway refuses when
consumption reaches the key budget and atomically records actual prompt/completion tokens on finish.
Changing a budget does not change consumption. Return RFC 9457 429 responses: `rate_limit_exceeded`
with `Retry-After`, or `monthly_quota_exceeded` with budget, consumed tokens, and reset time.

**Rationale**: Postgres already owns request usage and remains correct across process restarts. Redis
would add a service for household-scale counters. Actual engine usage is authoritative; bounded
concurrent overshoot remains visible rather than being hidden by a prompt-token estimate.

## R5 — Re-check revocation during streams

**Decision**: Do not cache successful API-key verification. Re-read key/user active state before each
request and at a bounded interval between streamed events. Rotation changes the hash and credential
version atomically. Revocation/deactivation invalidates the next check; the stream emits a terminal
authentication error and closes without requesting more engine work.

**Rationale**: database checks work across processes without an in-memory invalidation bus and satisfy
next-request revocation. Stream checks are bounded by event cadence and a one-second maximum timer.

## R6 — Configure the first admin

**Decision**: Add a Keychain-backed `bootstrap_admin_email` value. Startup creates or activates exactly
that local email as admin and records a bootstrap audit. Cloudflare identity verification remains a
separate login. The transitional static admin bearer no longer authenticates application routes.

**Rationale**: refusing unmatched identities requires an out-of-band first row. A configured identity
is deterministic and leaves no public bootstrap endpoint or persistent universal bearer.

## R7 — Make audit insert-only and secret-safe

**Decision**: Extend audit rows with actor type, optional user/request ids, before/after summaries, and
safe request context. Expose list/get only. Central redaction drops secret-like keys recursively and
sanitizes known credential patterns in strings. Mutation audits share the mutation transaction;
authentication failures use an independent best-effort transaction plus an audit-failure metric.

**Rationale**: success audits cannot outlive rolled-back changes, while refusal audits must survive an
abandoned request. No application code or route updates/deletes audit rows.

## R8 — Observability labels are bounded and secret-free

**Decision**: Emit `coire_auth_attempts_total{method,outcome,reason}` where method is one of
`access|api_key|legacy|none`, outcome is `accepted|refused`, and reason comes from a closed enum;
`coire_key_limit_refusals_total{kind}` with `rate|quota`; and
`coire_auth_audit_failures_total{reason}` with a closed persistence reason. Spans are
`coire.auth.verify_access`, `coire.auth.verify_api_key`, `coire.auth.enforce_limits`, and
`coire.audit.write`. Logs may contain user/key UUID and non-secret prefix, never presented material,
email, JWT claims, or arbitrary exception text as metric labels.

**Rationale**: authentication needs attributable failures without turning credentials, identity PII,
paths, or attacker-controlled values into telemetry cardinality or long-lived secret copies.
