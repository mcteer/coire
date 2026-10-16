# Local browser sign-in

Local sign-in authenticates existing Coire users without an external identity provider.
It does not create users, assign roles, change entitlements or enable Chat. Cloudflare Access
and scoped API keys continue to work. Local credentials are opt-in and fail closed when unset.

## Prepare and enable

Use the existing admin account. Back up Postgres before migrating to `0035_local_browser_auth`;
`0034_upgrades` is its predecessor. Migration 0035 only adds the local credential, browser
session and login throttle tables and is reversible after disabling local sign-in. Keep a
backup before downgrade because dropping those tables removes local credentials and sessions.

Build and scan the API and web images through the normal image-policy workflow. Do not retag
or replace unrelated production services. `LOCAL_AUTH_ENABLED` defaults false;
`LOCAL_AUTH_ORIGIN` defaults empty. Compose accepts these as `COIRE_LOCAL_AUTH_ENABLED` and
`COIRE_LOCAL_AUTH_ORIGIN`. The origin must be exact HTTPS, including the port and no trailing
slash. Loopback HTTP (`localhost` or `127.0.0.1`) is only for isolated development.

Run `python3 scripts/coire-local-tls.py` on Core. It creates a host certificate/private key
in the login Keychain under service `coire-local-browser-tls` and account `coire-core.lab`,
and materializes a private generation under `~/.coire/local-tls/`. The returned JSON contains
paths, never private key material. Preserve that generation while mounted. The certificate
expires after one year; renew it explicitly and replace the mounted generation through Compose.
The script neither changes system trust nor starts a listener. Do not overwrite an unknown or
malformed existing Keychain item.

The prepared `deploy/compose/compose.local-tls.yaml` overlay adds HTTPS on control-address port
8443 while retaining port 8180 for existing node traffic. **Obtain the operator's approval before
activating this extra listener** (AGENTS.md network rule). It does not alter networks, CORS,
capabilities, firewall rules or node URLs. Set the four path/origin variables emitted by the
preparation tool, retain the current control bind address, and include the overlay in the
normal installed Compose release. Replace only API/web with their validated image IDs. Check
`nginx -t` against the generated configuration before activation. Plaintext port 8180 cannot
accept a local password or session because its scheme differs from the configured HTTPS origin.

Trust the generated **public certificate** on the client before entering a password. Verify
its SHA-256 fingerprint against Core (`openssl x509 -in <certificate.pem> -noout -fingerprint
-sha256`) through a trusted channel. On macOS import the public certificate into Keychain Access
and explicitly trust it for SSL after checking the fingerprint. Never distribute the private
key and never ask users to bypass an unexplained certificate warning. Certificate trust is an
operator/client action; the preparation utility does not install a trust anchor automatically.

Set a password through a private terminal on Core:

```sh
docker exec -it coire-coire-api-1 /app/.venv/bin/python3 -m coire_api.identity.local_password USER_EMAIL
```

The hidden prompt requires at least 15 characters. A private automation pipe may use explicit
`--password-stdin`; never place the password in argv, an environment variable, the repository or
logs. The operation requires an existing active user and writes `identity.local.reset` audit
history. It cannot create or elevate an account. If an initial generated password is used,
store it in the operator's Keychain and deliver it privately, never in an execution record.

Open `https://coire-core.lab:8443`, enter that account's email/password, and verify the user
identity and role. Reload and sign out. The HTTPS cookie is `__Host-coire-session`, Secure,
HttpOnly, SameSite=Strict, host-only, path `/`, with a maximum eight-hour lifetime. Session
material never enters browser storage. The API stores only its SHA256 digest and rechecks the
active user, role, entitlements and password generation on every authenticated request.

## Reset, revoke and observe

Resetting the password with the same operator command revokes all prior sessions. Disabling
the user through the audited admin API refuses the next request. Sign out revokes that browser's
server session before clearing private drafts. No unverified cookie grants access; wrong Host,
scheme and mutation Origin are refused. Exact Origin checks cover **all** cookie mutations,
including admin actions. Invalid explicit bearer/Access credentials do not fall back to cookies.

Five password verification attempts per known credential per minute are allowed. Unknown users
share a single bounded bucket. Attempts commit before expensive work and survive failed login,
process restart and concurrent requests. Argon2id runs off the event loop, with one verifier at
a time. Refusals are uniform and do not reveal whether an account exists. Old expired sessions
are pruned on successful login; audit rows remain append-only.

Inspect `identity.local.login`, `identity.local.logout`, `identity.local.reset` and
`authentication.refused` audit actions. Correlate `coire.api.local_auth.verify` spans without
recording secrets. `coire_local_auth_attempts_total{outcome="accepted|refused|limited"}` feeds
the **Coire Local Sign-in** dashboard; `CoireLocalSignInFailures` alerts on repeated refusals.
Normal metrics/audit remain available in lean mode; trace history needs diagnostics.

## Rollback

Set `LOCAL_AUTH_ENABLED=false` and recreate only API to refuse all local sessions immediately.
Keep Access/API-key authentication and audit history. Remove the optional TLS listener through
the installed Compose release if no longer needed; retain the original 8180 listener and node
configuration. Restore the prior API/web image IDs if required. Do not drop local auth tables
until local sign-in is disabled and a verified backup exists. Do not delete mounted credential
generations or add a network/firewall exception to diagnose a sign-in failure.
