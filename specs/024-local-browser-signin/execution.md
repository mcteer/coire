# Local sign-in execution — 2026-10-15

Implementation and isolated acceptance pass. Production activation is pending operator approval
for one additional HTTPS listener under the explicit AGENTS.md network rule. No new production
listener, credential or schema has been activated. T009 remains open.

## Validation

- Full non-integration selection: **6,112 passed**, 75 existing platform skips, 655 integration deselections, 590.14s. This run collected the first four new database tests. After the final explicit-header refusal correction, the expanded security/auth regression passed **37 tests**, including all **nine** new local sign-in security cases, with no skips.
- Browser suite: **249 passed**, 62 files, including local login/logout and mixed local/Access logout compatibility. TypeScript production build and ESLint pass.
- Strict mypy: **1,308** app/package sources and the TLS preparation script pass. Ruff check/format and generated OpenAPI freshness pass; TypeScript API types regenerated.
- Actual API/web containers with disposable Postgres: migration from baseline to 0035, downgrade to 0034 and re-upgrade pass. Browser signs in over certificate-pinned HTTPS, verifies identity and Secure/HttpOnly/Strict cookie, reloads, verifies no password/session token in browser storage, signs out and rejects replay with 401. Certificate pinning was restricted to the prepared certificate; no system/client trust setting was changed.
- Both final images pass all seven container-policy rules and zero CRITICAL vulnerabilities (including unfixed); SPDX SBOMs generated.
- A production schema-0033 custom-format backup was created, parsed and successfully restored into isolated Postgres with no network. Restore container/volume removed.
- All feature test containers, networks, volumes and generated fixture credentials retired. Only compact private execution evidence remains. Required production rollback backup is retained privately.

## Prepared activation

Private evidence and deployment receipt live under `~/.coire/qualification/024/`.
The prepared manifest changes only API/web; original volumes, resource limits, networks,
credentials and plaintext Studio listener are retained. Existing temporary Compose source
files had disappeared, so exact API/web runtime custody was reconstructed into durable
private prepared/rollback manifests and validated with Compose and nginx preflight.
The existing installed-release pointer was not changed.

Prepared HTTPS uses the existing control address on port 8443. Approval to widen network
exposure is required by AGENTS.md: “Do not widen a CORS, network, capability, or firewall
setting to make something work; ask.” The TLS overlay and runbook make that action reviewable.
After approval: verify custody/backup, migrate, provision the existing admin password through
a private pipe and Keychain, recreate only API/web, verify actual owner sign-in/reload/logout,
install a durable release pointer compatible with the existing deployment custody, and record
the actual public-certificate fingerprint and client trust instructions. Recheck original
Studio HTTP traffic; preserve rollback and do not claim activation before these pass.

Cloudflare rollout and 020 physical fault acceptance remain outside this feature.
