# Feature Specification: Local browser sign-in

**Feature Branch**: `feat/024-local-browser-signin`
**Created**: 2026-10-15
**Status**: In progress
**Input**: Make the local interface usable without Cloudflare; finish implementation and verify browser access.

## User Scenarios & Testing

### User Story 1 — Sign in locally (Priority: P1)
An existing enabled user signs in with email and password and reaches their own console without an external identity provider.
**Independent Test**: Provision a local password, sign in, reload, and read the current identity.
**Acceptance Scenarios**:
1. Valid credentials establish a bounded browser session and display the correct user and role.
2. Invalid credentials, unknown users and disabled users receive the same refusal.
3. An unauthenticated visitor sees a useful sign-in form rather than an expired-session loop.

### User Story 2 — End access (Priority: P1)
A user signs out and an operator can reset local credentials.
**Independent Test**: Replay the old session after logout, password reset, expiry and user deactivation; all are refused.
**Acceptance Scenarios**:
1. Logout invalidates the server session immediately and clears the browser identity and drafts.
2. Resetting a password invalidates all previous sessions without changing roles or entitlements.

### Edge Cases
Cross-origin writes, absent origins, malformed cookies, concurrent attempts, disabled local sign-in, provider-only deployments, and unavailable databases fail closed. LAN membership never grants identity. Secrets never enter logs, URLs, browser storage or audit detail.

## Requirements
### Functional Requirements
- **FR-001**: Provide email/password local browser authentication for existing active users; no public registration or implicit admin creation.
- **FR-002**: Passwords are entered through a hidden operator prompt and stored only as memory-hard hashes; password provisioning/reset is audited.
- **FR-003**: Sessions expire within eight hours, are server-revocable, and contain at least 256 random bits with only a digest persisted.
- **FR-004**: Revalidate active user, current role and entitlements on every session request.
- **FR-005**: Protect every cookie-authenticated mutation, including login/logout, against cross-origin requests. Retain route-specific scope checks and existing API-key/Access verification.
- **FR-006**: Bound password verification concurrency and throttle failed attempts durably before expensive verification. Authentication failures and lifecycle events are audited without secrets.
- **FR-007**: Require HTTPS for LAN sign-in and secure, HttpOnly, same-site cookies. Loopback HTTP is supported for isolated development only. Do not expand network exposure.
- **FR-008**: Show accessible sign-in errors, password-manager compatible inputs and working logout. Provider-only installations retain their sign-in path.
- **FR-009**: Document bootstrap, local certificate trust, reset, revocation, diagnosis and rollback; include metrics, a dashboard panel and an alert.

### Key Entities
Local credential binds one password hash and generation to an existing user. Browser session binds a random credential digest, user, credential generation, creation, expiry and revocation. Login throttle records bounded verification attempts.

## Assumptions and Clarifications
- Owner explicitly authorizes local sign-in; this amends 007 FR-002 and its browser-only Access clarification for explicitly configured local sessions. Access remains supported.
- No new identity provider, public account registration or email delivery is needed.
- Reuse the installed Argon2 implementation. No additional dependency is needed.
- LAN HTTPS uses the existing web listener and a locally trusted certificate; plaintext LAN passwords are refused.
- Feature 020 remains unchanged; this branch is based on its completed local implementation.

## Success Criteria
- **SC-001**: A provisioned user signs in and reaches their identity within one minute without any external provider.
- **SC-002**: All tested unauthenticated, invalid, expired, revoked, disabled-user and cross-origin attempts are refused.
- **SC-003**: Logout and password reset invalidate replay on the next request.
- **SC-004**: A real browser completes sign-in, reload and logout against the local deployment; test resources are removed afterward.
