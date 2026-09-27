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
