# ADR 0013: Bounded development credential recovery

Date: 2026-10-07. Status: one-time incident workaround for feature 016.

The operator authorized development credential setup. Login-Keychain unlock
reports an invalid key attribute; the Keychain subsequently reports unlocked,
but credential reads fail or await UI access. The configured compatibility
administrator credential is rejected because its bridge is disabled.

Use the incident procedure documented in `docs/runbooks/identity.md` only long
enough to issue scoped, rate-limited, budgeted development keys through the
audited admin API. Enable `IDENTITY_LEGACY_ADMIN_ENABLED` on the existing API
container only, using its existing Keychain-sourced compose secret. A separate
restore watchdog bounds this exception to three minutes. Restore the original
configuration in a finally block and verify the compatibility bearer returns
401 before any acceptance workload starts.

This is a time-boxed exception to Constitution IV's scoped-key requirement;
every request still requires a credential. Record actual start/end times,
issued key UUIDs, audit evidence and restoration verification in the feature
execution record. Network, CORS, container capabilities and Access issuer/audience
stay as configured. No new models or entitlements are acquired or granted.

Keep issued test keys in a separate private encrypted Keychain with explicit
access for the local credential reader. Do not replace or reset the user's
login Keychain. Revoke temporary keys after acceptance. This exception is not
part of the feature's deployment defaults.
