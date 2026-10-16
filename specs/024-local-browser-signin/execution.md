# Local sign-in execution — 2026-10-15

Implementation and production browser acceptance pass. The owner explicitly approved
activation on 8443 on 2026-10-16. API/web are healthy on the existing control address;
HTTP browser pages redirect to HTTPS and the original HTTP machine routes remain available.
T009 is complete. No system/client trust store was changed automatically.

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

## Activated 2026-10-16

Owner approval: “Yes, please proceed with acitvation on 8443.” A fresh consistent backup
was created before mutation. Schema 0035 was applied, only API/web were recreated, and
the existing active admin received a local password stored in the operator's Keychain.
The password was passed privately to the audited provisioning utility and never recorded
in repository, CLI arguments, environment or execution logs.

Actual production browser checks pass: HTTP browser entry redirects to HTTPS; sign-in
returns the existing owner/admin identity; reload retains it; the admin API returns 200;
the admin page receives a live cluster snapshot; Secure/HttpOnly/Strict cookie attributes
and absence of secrets in browser storage are verified. Logout revokes the session and
replay returns 401. No 5xx response occurred during the walkthrough. Original HTTP health
returns 200 and native node probes/reconciliation remain healthy. All unrelated production
container IDs remain unchanged. No temporary test container or volume remains.

The durable targeted deployment/rollback manifests and public certificate are under
`~/.coire/projects/coire/local-signin/current/`. This is deliberately a separate two-service
pointer; it is not selected as the full control-plane release. The existing broader installed
release pointer remains unchanged. Future full deployments must include the local TLS overlay.
Private backup, custody and browser receipts remain under `~/.coire/qualification/024/`.

All 35 hosted validation jobs passed for implementation commit `4c8af410`; publish was skipped
as expected for a PR. The final browser-HTTP redirect correction additionally passes two
security regression tests, strict script typing, Ruff, actual nginx configuration validation,
HTTP redirect/API health checks and the repeated production browser walkthrough. The follow-up
commit records activation and this correction; its hosted checks run independently.

Client onboarding: trust the public host certificate on the browsing device, then use the
existing account email and the Keychain-stored password. The operator may instead set a
chosen password with the hidden-prompt provisioning command. Credentials stay private.

Cloudflare rollout and 020 physical fault acceptance remain outside this feature.
