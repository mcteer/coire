# ADR-0004: Interim static admin token until feature 007 provides roles

- **Status**: Accepted
- **Date**: 2026-08-30
- **Deciders**: Dan McTeer
- **Constitution**: extends the **Principle IV** exception recorded in ADR-0001, and the
  Technology Constraints line forbidding long-lived static tokens
- **Time-box**: closes with feature 007 (auth, users, keys, audit), together with ADR-0001

## Context

Feature 001 introduces the first routes that must refuse a non-admin caller: adding, curating,
loading, unloading and retiring models (spec 001 US2, FR-003, FR-004; Principle V). Feature 000
shipped no user-facing credential at all (ADR-0001), so the platform cannot yet tell an admin
from anyone else. A placeholder guard that always succeeds is explicitly rejected by ADR-0001 —
it looks like protection and is worse than none — but shipping feature 007's roles, edge
identity and API keys now would block the model registry on an identity provider that is not
configured.

## Decision

1. A **static admin bearer token** is created once in core's login Keychain
   (`coire-admin-token`, via `scripts/coire-secrets-init.sh`) and mounted by `coire-up` as
   `/run/secrets/admin_token`, exactly like the other three secrets.
2. `require_principal()` in `apps/coire-api/src/coire_api/auth.py` — the seam ADR-0001
   declared — returns a principal of kind `ADMIN` when the presented bearer matches the mounted
   secret (constant-time compare) and the anonymous principal otherwise. `Principal.is_admin`
   becomes true only for that kind.
3. A `require_admin` dependency guards every `/api/v1/admin/*` route: a non-admin caller gets
   **403** and an `audit_log` row with `outcome: refused` (actor `anonymous`). The contract test
   enumerates every admin path from the generated OpenAPI document so a new route cannot omit
   the guard.
4. The `audit_log` table is created now, append-only from its first row; `actor` is the literal
   `admin-token` for admin actions until 007 supplies subjects.
5. The token is never sent to a Studio and never leaves the mesh/loopback: the platform remains
   reachable only on core's loopback and the unrouted mesh (ADR-0001 §2).

## Consequences

- Until 007, anyone with `coire-admin-token` from core's Keychain — i.e. the operator — is the
  only admin. There are still no users; the anonymous principal sees only
  `published` + `ready` models with an empty entitlement list.
- 007's Constitution Check must record this ADR as **closed**: the token is deleted from the
  Keychain and from `coire-up`, `require_principal` is replaced, and the audit `actor` column
  starts carrying real subjects. No route signature changes.
- The audit log's first rows will show `admin-token` as actor; that is the truthful record of
  this period and is not rewritten.

## Alternatives rejected

- Treating every caller as admin until 007 — makes SC-002 untestable and turns a public-facing
  platform's registry into an open write surface the moment the tunnel is opened.
- Pulling feature 007 forward — blocks 001 on Cloudflare Access and a user model it does not
  need; the spec's assumption ("enforced through whatever credential mechanism feature 000
  established, and hardens later without changing these rules") is exactly this ADR.
