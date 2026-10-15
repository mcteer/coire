# Release Review: Control-Plane Failover

## Dependency evidence

`coire-failover` uses the existing MIT-licensed FastAPI, Uvicorn, HTTPX, and `coire-core`
workspace dependencies. HTTPX is pinned at `0.28.1` in the service distribution to make the
release image reproducible. `coire-core` now declares `cryptography==50.0.1` (Apache-2.0/BSD) for
Ed25519 verification of snapshots and peer votes; it was already present transitively through the
Cloudflare Access verifier and is now a direct, pinned security dependency.

## Mandatory real-cluster verification

Not yet performed. T036 remains open until the power-loss, partition, ingress, break-glass,
and hand-back observations from `quickstart.md` are recorded here. Do not include credentials,
keys, or model data. The local integration suite does not substitute for those observations.

## Local verification on 2026-09-25

- `uv run pytest -q`: 725 passed, 108 skipped after the service-boundary and controller
  shutdown/rollback tests were added. `COIRE_TEST_MODEL` was not configured, so the required
  tiny-model hardware integration was not run.
- Web tests: 17 passed; TypeScript build and ESLint passed.
- Ruff, mypy, Compose validation, shell syntax, pinned-image lock, and both OpenAPI freshness
  checks passed. A focused 13-test run passed after adding publication/subscription telemetry.
- The arm64 failover image built and served the degraded page from a local container; its
  election readiness returned 503 without a lease. Image policy passed, Trivy reported zero
  critical findings, and Syft wrote `/tmp/coire-failover-020-final.spdx.json`.
- The service-boundary tests exercise signed core snapshot delivery, Studio override delivery,
  journal replay, hand-back, and authenticated frontend completion streaming.

These are local gates only. The tiny-model and physical cluster tests remain required before
release.

## Resolved implementation review

- Core publishes short-lived snapshots from the authoritative model registry. Studios verify
  the pinned core key, retry subscription after startup, reject rollback, and atomically install
  the cache. A membership change fences the old poller before the new one starts.
- Core and both Studios load election secrets from Keychain-backed paths. Direct, core-signed
  overrides remain usable during a core outage and are journaled for later audit.
- The node agent starts only the precreated, digest-pinned failover container while elected or
  draining. It fences the proof file and stops the container on shutdown or demotion.
- Core sends signed hand-back notices. The Studio drains outstanding completions up to its
  deadline, while bounded local events replay to core's idempotent audit reconciliation route.
- The degraded frontend serves the built web assets, displays its capability tier, and streams
  authenticated completions against resident models through scoped node relays.
- On 2026-09-25 the user chose to keep each Studio's 256 MiB standing ledger reservation
  after demotion so future failovers remain admission-safe. FR-018 and its acceptance scenario
  were aligned with FR-015 and this decision.

Each armed host runs one poll task on `failover_election_interval_s`. It exchanges a signed
heartbeat on the existing control listener and solicits a vote only when it is the eligible
leader. Three missed beats mark a peer unreachable; a slow beat is degraded; recovery takes the
same number of beats. The physical quickstart is still required before release.

## Planning reconciliation — 2026-10-14

The user requested specify, clarify, plan, tasks and read-only analysis for 020 after feature 019
merged at `cd355554`. The current plan/spec/contracts/tasks supersede the September design where
they differ. No implementation tests, deployment changes or real-cluster acceptance were performed
in this documentation pass. Historical results and checkmarks above remain historical evidence.

Read-only source inspection found work beyond the old T036 gate: restart/clock-safe grants, actual
origin/relay admission, route scopes, host-bound non-replayable break-glass, non-lossy security audit,
bounded active-request authority/drain, truthful capabilities and sharded residency proof, 019
maintenance integration, 022 control addressing, 023 telemetry export and real ingress qualification.
See [research.md](research.md) for specific source evidence and [tasks.md](tasks.md) for the executable
follow-up. Start at T041; close T036 only after the corrective and measured qualification tasks pass.

Current live prerequisite status is **not inspected**. No claim is made that account entitlements,
monitor credentials, independent recovery/fencing or approved sharded assets are ready. The next
model must record concrete preflight results and proceed with all independent work.

## Corrective execution baseline — 2026-10-14 (T041)

Source baseline: `cd35555493d8e5479d34cfc6cbc162213c6cd94a`, branch
`feat/020-control-plane-failover`, with preexisting uncommitted planning/skill changes preserved.
The requested global skill file was absent; execution uses the repository's implement skill.
Prerequisite discovery returned this feature and all required design artifacts. Requirements
checklist: 16 checked, zero unchecked. `.specify/extensions.yml` is absent (no pre/post hooks).

### Current route and credential inventory

| Surface | Current enforcement | Corrective task / risk |
|---|---|---|
| Frontend `/`, `/assets/*` | No origin identity check | T046/T047: require Access, including direct-origin requests. |
| Frontend `/ready` | Fresh snapshot and proof; no monitor credential | T046/T047: scoped authenticated, non-cacheable monitor. |
| Frontend `/failover/tier` | No credential; exposes in-flight count | T047: separate browser and local lifecycle scopes. |
| Frontend `/v1/models`, `/v1/chat/completions` | Access verifier, fresh snapshot, initial proof check | T049–T051/T062: holder, eligibility, request bounds and ongoing expiry checks. |
| Core `/failover/ready` | Election check when configured; otherwise 200, no monitor credential | T047/T049: monitor scope and actual gateway admission fencing. |
| Core peer heartbeat/votes | Signed declared member/candidate; no challenge-bound freshness | T043/T045: strict fresh exchange and durable grant safety. |
| Core snapshot | Signed Studio heartbeat with 30 s timestamp window | T045: challenge/replay binding and declared control addressing. |
| Core event replay | Signed host batch, authoritative transaction; count-only response | T064: acknowledged event IDs, audit capacity and concurrency safety. |
| Core admin override | `CurrentAdmin` and core signature; broadcasts to both Studios, audits after delivery | T055: human actor, single host/epoch, replay guard, intent before delivery. |
| Node heartbeat/votes/handback | Declared-peer signatures; handback returns 204 | T045/T062: fresh challenges, restart safety and fence acknowledgement. |
| Node override | Core signature; legacy override has no target/epoch/ID | T043/T055: reject unbound/replayed authority. |
| Node residency/completion relay | Separate `X-Coire-Failover-Relay` static secret; ready base engine checks | T049/T053/T066: request-bound authority, exact group/rank evidence and maintenance. |

Inspected implementations: `coire_failover/app.py`, core/node `routes/failover.py`,
`routes/admin_failover.py` and shared `models/failover.py`. These findings are release gaps,
not authorization to expose or activate the existing service.

### Deployment and acceptance prerequisites

- `deploy/compose/compose.yaml` puts the frontend behind the `studio-failover` profile, reads
  snapshot/proof files through a read-only mount, and mounts the independent relay secret.
  `coire-studio-failover-create` requires a digest-pinned image and both Studio relay URLs,
  materializes Keychain-backed relay material, and precreates without starting the service.
- `deploy/cloudflared/README.md` still describes an empty fallback pool; the reconciled design
  requires provider-supported ordered pools and an explicitly non-serving fallback (T057).
  Templates are not evidence of deployed Cloudflare entitlement, authenticated monitor policy,
  propagation time, or all-unhealthy behavior.
- Local session inspection found `COIRE_TEST_MODEL`, `COIRE_FAILOVER_IMAGE` and
  `COIRE_SECRETS_DIR` unset; `/opt/coire/secrets` and `/opt/coire/failover` are absent on this
  host. This establishes only missing **local-session** prerequisites, not missing Keychain or
  remote assets. No secret values were read or printed. No live configuration was changed.
- Still to verify through documented authenticated workflows: Cloudflare LB entitlement/token
  and three tunnels; separate monitor/lifecycle credentials; current Studio snapshots,
  reservations, maintenance owners and approved resident single/sharded assets; independent
  Core power recovery and public-origin fencing. No external blocker is yet established;
  independent corrective implementation must continue before live fault qualification.
- Ignore setup verified: Git/Python/Node/secrets/model artifacts in `.gitignore`, build-context
  exclusions in `.dockerignore`, and web flat ESLint ignores (`dist`, `node_modules`, `coverage`).
  No publishing, Terraform or Helm ignore requirement was identified for this feature.

### Test inventory and first red/green increment (partial T042/T043)

Historical tests remain in shared model/poller tests, API snapshot/publication/election/override/
handback/reconcile tests, node auth/subscription/election/route tests, frontend contract/proxy/
resolution tests, and composed `test_control_plane_failover.py`/topology tests. September results
above are not qualification of the revised specification. CI has the failover image build matrix;
required new isolated fault/tiny-engine cases remain T071 work.

Added three lease contract regressions: simultaneous proof plus override is rejected; inhibit
cannot be used as serving authority; each single authority is accepted while absent authority and
wrong proof holder are rejected. Initial model run: **3 failed, 6 passed**, exposing the two
permissive validation defects. Implemented the narrow validator correction in coire-core.

Validation after correction:

- `uv run pytest -q packages/coire-core/tests/test_failover_models.py packages/coire-core/tests/test_failover_poller.py apps/coire-failover/tests apps/coire-node/tests/unit/test_studio_failover_election.py apps/coire-api/tests/unit/test_failover_election.py`: **44 passed**. Pytest also emitted 14,370 cleanup warnings from previously retained temporary directories; these are not failed assertions or acceptance evidence.
- `uv run ruff check packages/coire-core/src/coire_core/models/failover.py packages/coire-core/tests/test_failover_models.py`: passed. Initial format check found one wrapping issue, corrected with `uv run ruff format`.
- `uv run mypy packages/coire-core/src/coire_core/models/failover.py`: passed.
- `uv run python -m coire_api.openapi --check` and `uv run python -m coire_failover.openapi --check`: passed. This validator-only change does not alter generated structural schemas.

T041 is complete. T042/T043 remain **partial and unchecked**: version/epoch/lifetime, host binding,
relay context, acknowledgements, limits and fresh heartbeat contracts are not delivered by this
increment. No release, live acceptance, image build/scan, complete type gate or full-suite claim is
made. Continue at T042, then the stated dependency order; T036 remains last.

### Continued corrective implementation — 2026-10-14 (worktree, unqualified)

Preserved the pre-existing skill/workflow/spec changes. No commit, deployment, PR or merge has
been performed. Implemented additional portions of T042–T051 and T061–T064: protocol-v2 strict
contracts, 300-second maximum authority lifetimes, host/epoch-bound override and acknowledgements,
120-second snapshot horizon, explicit eligible Access subjects and conservative local text limits;
atomic/fsynced restart/grant/high-water/replay guards; challenge-bound authenticated heartbeat
responses over configured control DNS; scoped frontend monitor/lifecycle/browser credentials;
request-bound signed relay contexts, independent node validation and lifetime cancellation;
bounded ordinary/terminal journals, committed-ID acknowledgement compaction and savepoint-based
receipt reconciliation. Single-target admin override delivery records intent before delivery and
validates human actor, signature and target acknowledgement. These are partial corrective tasks,
not a claim that all required negative, fault, sharded or maintenance cases are implemented.

Safe assumptions: fail closed on unconfigured identity eligibility, uncertain safety state and
unavailable audit capacity; preserve the original snapshot expiry during recovery/offline
promotion. Use conservative local counters, not a new billing or conversation store. Core is
still the sole writer. The normal node engine proxy now independently checks armed Core authority,
including current epoch/snapshot and lifetime; only the separately validated 019 smoke admission
has an exemption. Serving checks inspect the monotonic clock guard at admission, not just at poll
intervals. Polling refreshes production wall time after awaited I/O, preserving injected logical
time only in isolated deterministic tests. The Studio poller shares its supplied HTTP client so
ASGI/mock-based tests cannot accidentally use a separate transport against the real Studios.

Actual validation (separate invocations, totals must not be added as distinct coverage):

- Prior focused corrective run: **101 passed** (`/tmp/coire-020-third.log`).
- Boundary fixtures initially reproduced **3 failures, 16 passes**; corrected publication's
  active-user query fake, reconciliation savepoint fake and strict relay authority fixture.
- `uv run pytest -q packages/coire-core/tests/test_failover_poller.py`: **10 passed**, including
  captured signed heartbeat refusal, declared DNS endpoints and six independently scheduled
  restart/partition combinations. Every scheduled step inspects signed proof validity and
  admission gates, not roles alone. These are in-process simulations, not T058 process-boundary
  or live acceptance evidence. `/tmp/coire-020-poller.log`.
- Publication/handback/exact-adapter boundary tests: **13 passed**.
- Node Core-authority/proxy responsiveness/exact-adapter/poller tests: **24 passed** after correcting
  the new helper's default unarmed sentinel and membership setting name. Includes missing or
  malformed proof, uncertain node clock, signed epoch rotation and snapshot corruption.
- Composed `tests/integration/test_control_plane_failover.py`: **8 passed**. Updated obsolete
  unbound override fixtures, snapshot horizon and Core-reclaim fixture TTL to the production
  15-second lease. Signed override acknowledgement and committed journal compaction are asserted.
  Running with `-m integration` selected **zero** of these historical cases; marker/CI registration
  remains T071 work, not a passing integration gate. `/tmp/coire-020-composed2.log`.
- Web red tests: **5 failed, 4 passed**, reproducing false completed streams, false minimal service,
  expiry submission and lost drafts. After correction, focused web tests: **9 passed**.
  `pnpm -C apps/coire-web exec tsc -b` and `pnpm -C apps/coire-web lint`: passed after updating
  fixtures for generated required response fields. SSE EOF without terminal completion now
  reports interruption, readers are cancelled/released, and refused drafts are restored.
- Scoped strict mypy: **35 source files passed**, then the new Core-authority/engine/node-route
  increment: **3 source files passed**. Scoped Ruff formatting/import corrections performed;
  full integrated format/lint/type gates remain open.
- Both application OpenAPI documents and both TypeScript schemas regenerated. Final freshness
  checks remain open while contracts change.
- Full `uv run pytest -q` was time-limited at 180 seconds with no completed summary; it is **not**
  a passing result. A new full run is executing with logs/status under `/tmp/coire-020-full.*`.

Confirmed this development host is `coire-core.lab`. Do not start a native tiny-model workload
here: Core hosts no models (Principle II). Engine integration/acceptance must use the authorized
isolated Studio path. Live credentials/assets/account entitlements are still unverified, not an
established blocker. Required deployment durability/secret wiring, exact sharded rank/link proof,
process-boundary faults, 019 recovery/races, observability, runbooks, image gates and live cases
remain open. Task checkmarks remain conservative; T036 executes only after T073.

### Subsequent corrective increment — 2026-10-15 (worktree, not release-qualified)

- The full regression finished: **6,223 passed, 26 failed, 444 skipped, 1 error**
  (`/tmp/coire-020-full.log`). This is **not green**. The obsolete five-minute signed snapshot
  fixture is corrected and its targeted tests now pass. Remaining failures are upgrade Docker,
  hosted/native workload and observability/stream-isolation qualification failures; stale
  executor images and unavailable fixture prerequisites require fresh qualified reruns, not dismissal.
- Frontend transport credentials no longer sign authority. Added strict node-owned relay issuance;
  only the elected node's Ed25519 key can sign the request-bound context. Both issuance and relay
  verify current authority. Identity I/O is followed by renewed clock/epoch/serving checks.
  Real-crypto tests reject transport-token HMAC forgery, wrong node signatures, altered bodies
  and expired grants. Node contract tests exercise signed issuance, broad-credential refusal,
  revocation during identity I/O and a 64 KiB pre-validation body bound. The identity provider is
  explicitly mocked in these node contract cases; they are not live Access-provider evidence.
- Draining retains bounded authority for existing work only. A new busy-accounting case proves
  the drain file disappears at the underlying 15-second grant expiry even with in-flight work;
  the durable election reservation remains held to prevent overlapping successors.
- Text estimates include NFKD normalization expansion without tokenizers or weights; invalid
  surrogates fail with a stable refusal. Resident-only payloads clear wait-for-model instructions.
  Red tests reproduced both Unicode defects before correction.
- Browser tier responses now use a separate shared `FailoverPublicStatus` contract and omit
  lifecycle counts entirely. Scoped lifecycle callers retain the extended typed projection.
  OpenAPI and both generated TypeScript schemas were regenerated after this change.
- Latest broader corrective run: **171 passed** (`/tmp/coire-020-current.log`); subsequent
  crypto/public-projection/Unicode/drain increment: **145 passed** (`/tmp/coire-020-public.log`).
  These overlapping totals must not be added. Web tests: **9 passed**; web build and lint pass.
  Scoped strict mypy: **28 source files passed**. No native model workloads ran on Core.
- T042 is now checked against actual rejection and replay tests. T043 and subsequent corrective
  tasks remain open, including exact signed instance/group-generation/rank/link residency,
  independent process-boundary faults, 019 races, observability and live qualification. No
  deployment, commit, PR or merge has been performed in this increment.
- Public Cloudflare pool/load-balancer creation documentation was inspected read-only to begin
  provider-capability verification. This is not proof that this account accepts the disabled
  non-serving fallback, has required entitlements, or meets the live failover target.

### Latest foundation/access checkpoint — 2026-10-15

- Hand-back notices now have a signed request UUID, aware issue/expiry times and a maximum
  30-second lifetime. Receivers reject older terms and uncertain clocks. Node replies are typed,
  signed acknowledgements; Core checks request/host/epoch/term/successor, deadline and signature,
  and does not count merely draining work as fenced. Ten reply-tamper cases and the composed
  hand-back route pass (**20 passing tests**, `/tmp/coire-020-handback2.log`).
- Added exact single/sharded placement, immutable artifact target, per-model limits and opaque
  rank-generation contracts, plus short-lived challenge-correlated signed rank observations.
  Pure shared helpers detect PID reuse through creation time, instance/group/manifest replacement,
  and mismatched/expired/unhealthy rank/link evidence. These are **contracts and helper tests**,
  not live or fully wired sharded support. Snapshot publication and native/front-end admission
  still need exact residency integration; do not treat nullable placement or empty capabilities
  as eligible. T043/T051/T053 remain open until producers and consumers are wired fail-closed.
- Latest combined corrective suite: **208 passed**, `/tmp/coire-020-checkpoint.log`. Scoped mypy:
  **30 source files passed**. Web: **9 passed**, build/lint passed. Both module-based OpenAPI
  freshness checks and `git diff --check` pass. The documented `coire-api` executable is absent
  in this workspace, so freshness was checked via `uv run python -m coire_api.openapi --check`.
  Earlier full-regression failures/skips remain unresolved; this is not release completion.
- Read-only SSH succeeds to **both** exact Studio hostnames. Both have `/opt/coire/envs` and
  `/opt/coire/models`. No processes were started, services installed, secrets changed, model
  workloads run, live configurations changed, commits made or PRs opened.
- Cloudflare account/zone and the approved API-credential reference remain unidentified.
  Repository search found no API credential setting/reference; this process has no `CF_*` or
  Cloudflare environment names; readable login/System Keychain metadata has no Cloudflare-named
  service. This does **not** prove that an externally managed/custom-named credential is absent.
  An owner must identify its approved reference and qualification account/zone (not paste the
  secret) for authenticated provider configuration/entitlement and live fallback verification.
  Independent coding remains possible while that qualification prerequisite is resolved.

### Origin/residency implementation increment — 2026-10-15

Owner scope amendment: Cloudflare deployment and provider/public-edge acceptance happen last.
Those cases are deferred, not passing; authentication and all origin safety rules remain intact.

- Wired signed single-instance/artifact/process generation through snapshot publication,
  frontend discovery/selection, node-owned authority and native admission. Legacy nullable
  placement or empty capabilities refuse service. Core only reads registry/native metadata.
- Sharded launch commands now bind an immutable verified base target and command-specific
  worker environment markers. Each native manager observes its own worker PID/create-time;
  launcher PIDs, replaced workers, ambiguous workers and unverified adoption cannot qualify.
  Added signed challenge-bound all-rank checks, data-link checks and stream lifetime fencing.
- An asynchronous admission probe now completes before scheduling bare engine work. Native
  positive route tests exposed/fixed an existing bug forwarding Coire-only request fields to
  the strict bare-engine request schema. No wrapper, fallback or acquisition path was added.
- The frontend now initializes bounded local metric export and optional traces, using the
  already approved Studio OTLP control endpoint. Direct dependencies added to this distribution:
  `opentelemetry-sdk==1.44.0` and `opentelemetry-exporter-otlp-proto-grpc==1.44.0`, both Apache-2.0,
  already present at those exact versions in the workspace lock. Actual collector tests caught
  dimensionless gauge renaming; readiness/freshness now match shipped Prometheus rules.
- Frontend startup holds 019's existing admission permit across Docker I/O; an active
  maintenance fence prevents a late start without altering health or ownership records.

Evidence so far (overlapping totals; do not add):

- Exact residency initial suite: **93 passed, 3 obsolete proxy fixtures failed**; fixtures were
  corrected to carry the same exact engine ID. Native relay contract checkpoint: **11 passed**.
- Sharded identity/lifetime unit suite: **12 passed**, including five rank/link/proof loss cases
  interrupting a stream within three seconds. `/tmp/coire-020-shards.log`.
- Independent process suite: **3 passed** (all isolated-member majority/minority cuts, actual
  process kill/restart and durable grants, authenticated production frontend admission).
  Uses accelerated isolated timers; not live 60/90-second acceptance. `/tmp/coire-020-processes2.log`.
- **Real tiny-model smoke on edge-a: 1 passed in 17.46s.** Current source runs from the isolated
  `/tmp/coire-020-qualification` checkout with frozen workspace dependencies. It validates the
  existing admin-acquired Qwen2.5 0.5B acquisition manifest, uses a private hardlinked model store,
  starts/stops its own bare engine through coire-node, and completes authenticated buffered and
  streaming failover requests, then refuses stale process identity. The test identity provider
  is an isolated RSA JWKS fixture; Cloudflare is not exercised. No installed node service was
  replaced, production engine stopped, model downloaded, or weight loaded on Core.
  `/tmp/coire-020-native-smoke.log`. CI now registers this gate on the isolated hosted Mac.
- Actual OTLP collector export: **2 passed**, lean and diagnostics profiles.
  `/tmp/coire-020-metrics2.log`. New absence/audit/resource/deadline Promtool scenarios: **SUCCESS**.
- Broad non-integration suite: **6,063 passed, 1 failed, 75 skipped, 635 deselected** in 685.54s.
  The failure was the secret-generation fixture missing the new monitor/lifecycle files;
  its expected complete file set is corrected and targeted verification is running. Skips
  are not required-native qualification. `/tmp/coire-020-regression-current.log`.
- Native arm64 failover image built and passed all seven image-policy checks. Current final
  source images for API/scheduler/web/failover are building; scans/SBOM and final type/schema/
  web gates remain open. No commit, PR, merge or production deployment in this increment.
- Read-only Studio inspection found `rdma_ctl status` reports `enabled` while the old
  `system_profiler` heuristic reports degraded. Corrected the native read-only link observation
  to prefer the bounded authoritative OS status command, paired with the existing data-fabric
  connectivity check. This still does not substitute for real sharded-group qualification.

Remaining: finish sharded live acceptance, operational docs/scope reconciliation, final integrated
checks, image scans/SBOM, and precise disposition of deployment-only power/resource/public-edge
cases. Task markers remain conservative pending that evidence.


### Corrective implementation qualification — 2026-10-15

This section supersedes the earlier implementation-gap inventory; earlier attempts remain above
for provenance. Cloudflare remains owner-deferred. **This is not aggregate release completion.**

Additional closure:

- Core hand-back now requires a distinct signed `recovery_ready` observation. Bounded probes check
  PostgreSQL, enabled Core services, no Core maintenance fence and 019's existing retained-writer
  qualification. The observation expires after two poll intervals plus one second; a live unready
  Core remains live and cannot trigger drain. Recovery damping starts with continuous readiness.
  This closes the previously missing authoritative-cohort gate without changing ownership.
- Model rate/concurrency policy now intersects subject limits at both frontend and native relay.
  All-rank proof caches include the trusted membership/epoch as well as exact placement, so key or
  epoch changes require new signed observations immediately.
- Native sharded launches now drain a bounded diagnostic tail, detect early launcher exit, set
  offline model flags and include MLX's required `--pipeline` switch for PP. Existing acquired
  replicas remain unchanged; no engine wrapper or acquisition path was added.
- Updated operations runbook covers scoped credentials, 120-second horizon, one-host override,
  audit repair, conservative quarantine, versioned disarm/update/re-arm and 019-compatible rollback.
  Cloudflare's old null-fallback/priority manifest is explicitly non-deployable design, pending
  provider qualification. No claim is made that those placeholder fields are provider-supported.

Passing evidence (overlapping suites, do not sum):

| Gate | Measured result | Private artifact |
| --- | --- | --- |
| Broad non-integration regression | 6,063 passed; the sole outdated secret-file fixture failure fixed and covered by passing closure suites; 75 unrelated optional skips, 635 deselected | `/tmp/coire-020-regression-current.log` |
| Integrated feature, native contracts, process faults, real collector and SQL ownership | **300 passed**, no skips, 50.22 s | `/tmp/coire-020-final-gates.log` |
| Final process epoch-rotation and recovery expiry | **14 passed**; unrelated preexisting pytest temp cleanup warnings | `/tmp/coire-020-epoch-closure.log` |
| Final all-rank residency/cache/fault gate | **15 passed**, 5.58 s | `/tmp/coire-020-residency-final.log` |
| Real native tiny-model buffered/streaming and stale-process refusal | **1 passed**, 3.01 s, edge-a; updated explicit engine cleanup | `/tmp/coire-020-native-final.log` |
| Full web suite | **245 passed**, 61 files | `/tmp/coire-020-web.log` |
| Strict mypy | **1,301 source files passed** | `/tmp/coire-020-mypy-closure.log` |
| Ruff check/format, web ESLint, TypeScript build | Pass | `/tmp/coire-020-{ruff,format}-closure.log`, `/tmp/coire-020-web-lint-final.log`, `/tmp/coire-020-tsc-final.log` |
| API + failover OpenAPI freshness; regenerated TS; generated node rank/authority schema assertions | Pass | `/tmp/coire-020-{api,failover}-freshness.log` and native contract suite |
| Reversible migration coverage | **11 passed**; existing 0013 preserved, no additional migration needed | `/tmp/coire-020-migration-final.log` |
| Promtool absence/audit/resource/deadline rules | **SUCCESS** with actual `coire-prometheus:ci` | `tests/observability/failover.alert.test.yaml` |
| Lean/diagnostic collector-loss admission and bounded flush | **2 passed**; also included in integrated suite | `/tmp/coire-020-collector-loss.log` |
| Hardened production frontend startup, network-none collector loss | Anonymous requests refused before/after 31 seconds; running, no OOM; **79.07 MiB / 256 MiB**, CPU sample **0.19%** | `/tmp/coire-020-container-smoke.json` |

The short container resource sample is **not** the mandatory fifteen-minute baseline/armed delta
or a Studio swap/coexistence acceptance. No required feature test was reported as passed by skipping.
Native engines ran only on Studios. Both exact Studio hosts were checked after cleanup: no
qualification `mlx_lm.server` or `native_cluster.py` process remained. Installed node services,
production model files and production configuration were not replaced. Qualification images and
private model hardlinks are outside Git; no credentials or model bytes were staged.

Final arm64 production images all built, passed seven image-policy rules, scanned with **zero
CRITICAL findings** (`--ignore-unfixed=false`), and have SPDX JSON SBOMs. Other severities were not
part of the repository's blocking scan policy. A concurrent Trivy cache-lock failure was retried
sequentially and passed; it was not suppressed.

| Image | Local immutable image ID |
| --- | --- |
| `coire-failover:020-qualification` | `sha256:86208931daa2627e5d473625fb5e2b67d910653c300ac996c3b63f9174dd1075` |
| `coire-api:020-qualification` | `sha256:969afaa39a793d614850418642bea9d32783af69857a2c44c9ac6f6e9a239b59` |
| `coire-scheduler:020-qualification` | `sha256:52cda8746ebd559fc1284991860093cbf0c817466c7df1ab7ffaccc63f5ece5d` |
| `coire-web:020-qualification` | `sha256:4b18da83e3ca2a7949cb6bef815eea3d7565175f695ce0fd1145304fe8bc76e3` |

Scan/SBOM/policy files: `/tmp/coire-020-<image>-trivy-qualified.json`,
`/tmp/coire-020-<image>-qualified.spdx.json`, `/tmp/coire-020-<image>-policy-qualified.log`.
The image-ID/SBOM-digest manifest is `/tmp/coire-020-images-qualification.json`.
These are local content IDs, not published repository digests. No images were pushed.

Remaining qualification and exact prerequisites:

1. **Live TP sharded smoke failed before serving**, with MLX reporting `No IPv4-mapped GID` for
   `rdma_en5`. Both Studios' `en5` interfaces are active but have no IPv4 address; the approved
   fabric addresses are on `bridge0`. No network change was made. Owner approval was requested
   to temporarily add `169.254.200.11/16` and `.12/16` to the respective `en5` interfaces and
   remove them after testing, as required by AGENTS.md. Approval is pending. Raw attempt:
   `/tmp/coire-020-native-sharded2.err` and `/tmp/coire-020-coire-edge-a-sharded.log` (later PP
   attempt overwrote the latter). Source-verified TP capability on Qwen2.5 0.5B is true.
2. **PP attempt failed before serving** because the acquired tiny Qwen model has no pipeline
   implementation in the pinned MLX-LM. The available SmolLM tiny model also reports unsupported
   PP. The operator acceptance runner now checks native capability before launching; no model
   substitution or download occurred. `/tmp/coire-020-native-sharded-pp.err`. A verified
   PP-capable asset is needed for that independent mode's live acceptance.
3. **Full armed-020 upgrade/rollback matrix (T065)** remains open. Full API middleware preserves
   signed safety exchanges and fences DB/user work under 019; frontend start holds admission;
   real SQL handover deduplication/actor/ownership checks pass. These are not evidence of an entire
   running 019 success/failed-candidate rollback with 020 armed.
4. **Physical Core power loss** requires an independently demonstrated recovery path. This agent
   is running on Core; that recovery prerequisite was not established. Process kill/restart tests
   are not a substitute. Live 60/90-second public timings, fifteen-minute participant overhead,
   actual Studio swap/coexistence and deployed rollback remain unmeasured.
5. **Cloudflare** account/provider configuration, monitor/pool/fallback behavior and public-edge
   acceptance are explicitly deferred to its later installation. Do not apply the design manifest.

T036, T056/T057, T065 and T073 remain open. No production activation or merge is claimed.

Additional closure: native locked wheel build succeeded (`scripts/build-node-wheel.sh --local-only`),
selecting 88 locked macOS arm64 wheels; no Studio installation. Final strict type check including
`scripts/coire-deploy.py`: **1,302 source files passed**. Image pin check passed. Actual node relay
streaming across a newly acquired 019 maintenance fence remains inventoried, completes its prior
authorised request, and refuses new admission: **13 native contract tests passed**. Existing 019
Core target/writer, coordination, admission and failover-inventory regression: **59 passed**.
Logs: `/tmp/coire-020-node-wheel.log`, `/tmp/coire-020-type-final.log`,
`/tmp/coire-020-maint-stream2.log`, `/tmp/coire-020-019-closure.log`.

Final integrated rerun after the recovery-expiry, membership-cache and active-stream additions:
**302 passed, no skipped tests, 45.56 seconds** (`/tmp/coire-020-final-verified.log`). Ruff check,
format freshness and `git diff --check` remain clean. `.specify/extensions.yml` is absent, so no
post-implementation extension hooks apply. The five explicitly open tasks remain release gates.

Runtime/native-wheel source commit: `817ff11b2beb74c0c3f2d6a227d24fb5b2761302`. Production image IDs above were built from this
runtime source; subsequent record-only commits do not change their source.
