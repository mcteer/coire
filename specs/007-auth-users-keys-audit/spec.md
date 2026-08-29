# Feature Specification: Auth, Users, API Keys, and Audit

**Feature Branch**: `007-auth-users-keys-audit`

**Roadmap ID**: 005 (Phase 2 — Identity, users, admin)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "Cloudflare Access JWT validation, user/role model, scoped API keys with rate limits and budgets, audit log, admin API for users and keys."

## Overview

Everything so far has run behind a placeholder credential. This feature makes the platform's public exposure honest: edge identity validated on every browser session, a local user and role model, API keys that are scoped, rate-limited and budgeted, and an audit trail behind every administrative mutation. It closes the time-boxed Principle IV exception that feature 000 opened and is the last thing that must land before the platform can reasonably face the internet.

## Clarifications

### Session 2026-08-29

- Q: Are users auto-provisioned from edge identity? → A: No. Auto-provisioning is disabled; an admin creates the user row and the edge identity is matched to it by email. An authenticated identity with no matching user row is refused. On a public platform, "anyone my identity provider will authenticate" is not the same set as "anyone allowed to use my cluster".
- Q: How do programmatic clients authenticate, given the browser path is edge identity? → A: With Coire API keys alone, on the API and MCP hostnames, which are configured at the edge to accept them without an interactive Access challenge. Browser routes always require edge identity. This is what lets an SDK client work unattended while the console still sits behind the identity provider.
- Q: What does a key's budget do when exhausted? → A: Requests are refused with a specific quota error naming the budget and its reset time, distinct from a rate-limit refusal. Budgets are monthly token allowances; rate limits are short-window request ceilings. They are separate controls because they fail for different reasons and need different messages.
- Q: What must be audited? → A: Every administrative mutation, every credential lifecycle event, every entitlement change, and every authentication failure. Audit rows are append-only and are never deleted by any route the application exposes; retention is an operational concern handled outside the app.
- Q: How are keys stored and matched? → A: A key is shown once at creation and stored only as a strong password hash alongside an indexed non-secret prefix. Lookup finds the candidate by prefix and verifies the secret against the hash, so the full secret never needs to be stored or logged.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - No route is reachable without authentication (Priority: P1)

Every route requires a valid identity or credential, and an unauthenticated request is refused everywhere.

**Why this priority**: The platform is internet-reachable. This is the single most important property in the feature and the roadmap's first acceptance bar.

**Independent Test**: Sweep every route unauthenticated and confirm each is refused.

**Acceptance Scenarios**:

1. **Given** no credential, **When** any application route is called, **Then** it is refused with an authentication error and no side effect occurs.
2. **Given** an expired or malformed edge assertion, **When** a browser route is called, **Then** it is refused.
3. **Given** an authenticated edge identity with no matching user row, **When** any route is called, **Then** it is refused rather than auto-provisioning a user.
4. **Given** health and readiness routes, **When** called unauthenticated, **Then** they remain available, since they carry no user data and are needed for orchestration.

---

### User Story 2 - An admin issues, scopes, rotates, and revokes API keys (Priority: P1)

An admin creates a key with specific scopes, a rate limit, and a monthly budget; sees it once; and can rotate or revoke it later. Revocation takes effect immediately.

**Why this priority**: Programmatic access is the platform's main surface, and an un-revocable key on a public endpoint is an unbounded liability.

**Independent Test**: Create a scoped key, use it, revoke it, and confirm the next request fails immediately.

**Acceptance Scenarios**:

1. **Given** an admin, **When** they create a key with scopes, a rate limit, and a budget, **Then** the secret is shown exactly once and thereafter only its prefix and metadata are retrievable.
2. **Given** a key scoped to chat only, **When** it calls an admin route, **Then** it is refused for scope.
3. **Given** an active key, **When** an admin revokes it, **Then** the next request using it is refused without waiting for any cache to expire.
4. **Given** a key being rotated, **When** rotation completes, **Then** a new secret is issued and the previous secret stops working.

---

### User Story 3 - Limits and budgets are enforced distinguishably (Priority: P2)

A caller exceeding its short-window request rate is told so; a caller exhausting its monthly token budget is told something different. Neither degrades into a generic error.

**Why this priority**: Abuse controls are required from day one on a public platform, but the platform is usable before the distinction is polished.

**Independent Test**: Drive one key past its rate limit and another past its budget, and confirm two distinct, actionable errors.

**Acceptance Scenarios**:

1. **Given** a key at its rate limit, **When** it makes another request, **Then** it receives a rate-limit error with a retry hint.
2. **Given** a key that has exhausted its monthly budget, **When** it makes a request, **Then** it receives a quota error naming the budget and its reset time.
3. **Given** usage accumulating, **When** an admin inspects a key, **Then** consumption against the budget is visible before exhaustion.
4. **Given** a new budget period, **When** it begins, **Then** consumption resets and the key works again without manual intervention.

---

### User Story 4 - Every administrative action leaves a trail (Priority: P2)

An admin can reconstruct who changed what and when, including entitlement grants and failed authentication attempts.

**Why this priority**: Principle IV requires it, and the explicit-content entitlement in feature 015 is unusable without it. It is a strong second priority rather than first because nothing is blocked on reading the trail.

**Independent Test**: Perform several admin mutations and confirm each produced an audit row identifying actor, action, target, and time.

**Acceptance Scenarios**:

1. **Given** any administrative mutation, **When** it succeeds, **Then** an audit row records actor, action, target, before and after where meaningful, and timestamp.
2. **Given** a failed authentication, **When** it occurs, **Then** it is recorded with enough context to investigate without storing the presented secret.
3. **Given** an entitlement grant or revocation, **When** it occurs, **Then** it is recorded distinctly from other mutations.
4. **Given** any application route, **When** deletion of an audit row is attempted, **Then** no such route exists.

---

### Edge Cases

- The edge is bypassed and a request reaches the application directly on the LAN: the application MUST still authenticate independently, never trusting an unvalidated header.
- An edge assertion is valid but signed by an unexpected issuer or audience: it MUST be rejected.
- A key's prefix collides with another: lookup MUST verify the secret against the hash and MUST NOT authenticate on prefix alone.
- A revoked key is presented during an in-flight streaming request: the stream MUST be terminated rather than allowed to run to completion.
- A user is deleted while holding active keys: their keys MUST stop working immediately.
- An admin removes their own admin role: the system MUST prevent removing the last admin, so the platform cannot be locked out.
- Clock skew affects assertion validity: a bounded tolerance MUST be applied rather than rejecting outright.
- A budget is changed mid-period: the new budget MUST apply immediately against existing consumption rather than resetting it.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Every application route except health and readiness MUST require authentication.
- **FR-002**: Browser sessions MUST be authenticated by validating the edge identity assertion, including issuer, audience, signature, and expiry.
- **FR-003**: The application MUST authenticate independently of the edge and MUST NOT trust an unvalidated header, even on the LAN.
- **FR-004**: Edge identities MUST be matched to existing local user rows by email; auto-provisioning MUST be disabled and an unmatched identity MUST be refused.
- **FR-005**: The system MUST support the roles `admin` and `user`, and administrative routes MUST require `admin`.
- **FR-006**: The system MUST prevent removal of the last remaining admin.
- **FR-007**: API keys MUST be issued with an indexed non-secret prefix and a strong password hash of the secret; the secret MUST be shown exactly once.
- **FR-008**: Key lookup MUST verify the presented secret against the stored hash and MUST NOT authenticate on prefix alone.
- **FR-009**: Keys MUST carry scopes, and a request outside a key's scopes MUST be refused.
- **FR-010**: Keys MUST support rotation and revocation, and revocation MUST take effect immediately, including terminating in-flight streams using that key.
- **FR-011**: Keys MUST carry a short-window rate limit, enforced with a distinct rate-limit error and a retry hint.
- **FR-012**: Keys MUST carry a monthly token budget, enforced with a distinct quota error naming the budget and reset time.
- **FR-013**: Consumption against a budget MUST be visible before exhaustion, and MUST reset automatically at the period boundary.
- **FR-014**: A budget change MUST apply immediately against existing consumption without resetting it.
- **FR-015**: Deleting a user MUST immediately invalidate that user's keys.
- **FR-016**: Every administrative mutation, credential lifecycle event, entitlement change, and authentication failure MUST write an append-only audit record.
- **FR-017**: Audit records MUST identify actor, action, target, outcome, and timestamp, and MUST NOT contain presented secrets.
- **FR-018**: No application route may delete or modify an audit record.
- **FR-019**: The system MUST expose administrative routes to manage users, roles, keys, and entitlements.
- **FR-020**: Secrets used by the platform itself MUST be sourced from the operating system keychain and MUST NOT appear in the repository or any image.

### Key Entities

- **User**: A person. Identity, email, display name, role, entitlements, active flag, created and updated timestamps.
- **API Key**: A programmatic credential. Owner, prefix, secret hash, scopes, rate limit, monthly budget, consumption, state, created, last-used, revoked timestamps.
- **Entitlement**: A per-user grant. User, entitlement name, granted-by, granted-at, revoked-at.
- **Audit Record**: An append-only event. Actor, actor type, action, target type and identity, outcome, before and after summary, timestamp, request identity.
- **Usage Accumulator**: Consumption for a credential in a period. Credential, period start and end, tokens consumed, requests made.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Unauthenticated requests fail on 100% of application routes, verified by an automated sweep of the full route table.
- **SC-002**: A key can be created, scoped, rotated, and revoked, and revocation takes effect on the next request with no cache delay.
- **SC-003**: Rate-limit and budget refusals are distinguishable and actionable, never generic.
- **SC-004**: 100% of administrative mutations produce an audit row identifying actor, action, and target.
- **SC-005**: An authenticated identity with no local user row is refused in 100% of trials.
- **SC-006**: An out-of-scope request from a valid key is refused in 100% of trials.
- **SC-007**: No presented secret appears in any audit row, log, or trace, verified by inspection.
- **SC-008**: The last admin cannot be demoted or deleted.

## Assumptions

- Features 000–006 have shipped. This feature replaces the placeholder credential arrangement those features assumed and closes feature 000's time-boxed Principle IV exception.
- Edge identity is provided by Cloudflare Access over an OIDC provider on browser hostnames; API and MCP hostnames are configured to accept Coire keys without an interactive challenge.
- Edge rate limiting and WAF rules operate in front of the application; application-level limits are defence in depth, not a replacement.
- Node registration tokens from feature 005 are a separate credential class and are managed alongside keys but not interchangeable with them.
- Run tokens for agent containers arrive with feature 011 and reuse this feature's credential machinery.
- Explicit-content entitlement is defined here as an entitlement type; its enforcement in generation is feature 015.
- Audit retention and export are operational concerns; this feature guarantees only that records are written and never removable through the application.
- Platform secrets live in the macOS keychain on core and are delivered to containers as file-mounted secrets, established in feature 000.
