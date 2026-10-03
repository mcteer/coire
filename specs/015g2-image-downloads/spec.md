# Feature Specification: Authenticated image download grants

**Feature Branch**: `feat/015g2-image-downloads`
**Parent**: `specs/015-image-generation/` (part of T022, T023, T026, T038–T039)
**Dependency**: draft PR #63

## Goal

Let an owner issue a five-minute opaque grant for a published output and redeem it only with the same live human identity or personal image key. Explicit bytes require the current explicit entitlement and key scope at both issuance and redemption. A URL alone never grants access.

## Acceptance

1. POST `/api/v1/image-outputs/{id}/download-grants` requires the live owner and returns a no-store URL whose fragment carries an unguessable token expiring within 300 seconds. The database stores only a subject-bound hash. The client strips the fragment and sends the token in `X-Coire-Image-Grant`.
2. GET `/api/v1/image-outputs/{id}/content` requires current credentials, the grant header, matching owner/output/subject, unexpired grant, published nondeleted output and live explicit permission. Ordinary admin status does not bypass ownership.
3. The API opens only a regular file under the configured blob root without following path components, verifies its size and SHA-256, and streams PNG bytes with private/no-store headers. Missing/corrupt bytes fail safely.
4. Deleted outputs and revoked entitlements/keys deny new redemption immediately. An already authorized stream may finish; deletion and revocation apply to subsequent requests.
5. Tests cover token replay across subjects, expiry, tombstones, explicit revocation, unsafe blob paths and content response headers. Existing image admission remains disabled.
