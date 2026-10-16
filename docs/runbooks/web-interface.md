# Web interface operations

The local interface uses the existing authenticated HTTPS address on Core. Feature 025 changes
only React/CSS/font presentation; it adds no API shape, database migration, external runtime
request or network listener. The reference theme is light ivory/navy/gold with an explicit dark
option. The theme preference `coire.theme` is non-sensitive and independent of private drafts.

## Inspect and validate

Run `pnpm -C apps/coire-web test`, `pnpm -C apps/coire-web lint` and
`pnpm -C apps/coire-web build`. The build includes strict TypeScript. Format touched frontend
files with the project's Prettier configuration. Shared API declarations remain generated.
Self-hosted Inter and Cormorant Garamond fonts include OFL1.1 notices and source hashes under
`apps/coire-web/public/fonts`; never replace them with runtime Google Fonts requests.

Check sign-in, main routes, active sidebar state, admin permissions, capability-gated
Upgrades, model selection, draft suggestions, history and mobile navigation. Theme changes
must survive reload while blocked storage must not prevent use. Inspect desktop light/dark,
1024px tablet and 390px phone screenshots. Verify long code and tables scroll internally,
keyboard focus remains visible, and the mobile sign-out icon retains its accessible label.

The overview counts active instances separately from historical stopped/failed records. Read
historical details in Instances. Unknown cluster status is Connecting, never green by default.
Existing API spans/metrics/audit continue to cover the same operations; no new backend path or
telemetry destination is introduced by the visual redesign.

## Deploy and roll back

Build the dedicated web image with `apps/coire-web/Dockerfile`, run all image-policy rules,
a CRITICAL scan including unfixed findings, and produce its SPDX SBOM. Preserve the current
web image ID and installed Compose file before changing anything. Use the existing private
local sign-in deployment at `~/.coire/projects/coire/local-signin/current/compose.json`; retain
its HTTPS configuration, Keychain-derived certificate mounts, ports, networks and hardening.
Replace only the web image with the validated digest and recreate only `coire-web` with
`--no-deps`. Do not rebuild/recreate API, scheduler, nodes or observability for a visual change.

Check actual owner sign-in, all visible routes, a live admin snapshot, light/dark preference,
mobile navigation and logout/replay refusal. HTTP browser entry must continue redirecting to
HTTPS while the original HTTP machine health route stays available. To roll back, restore the
recorded prior web image ID in that same installed Compose file and recreate only web.
API/database state, credentials and conversations require no rollback for this feature.

Keep browser screenshot fixtures separate from production data. Stop the owned loopback
preview server, remove temporary browser fixtures, and verify no test containers/volumes
remain before reporting completion. Never record credentials in screenshots, Git or logs.
