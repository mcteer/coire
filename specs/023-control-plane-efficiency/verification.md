# Verification record — feature 023

## Before live rollout

- The user reported lag while moving Mac desktop windows. The pre-change Mini was mostly idle
  with no swap pressure in the sampled window. OrbStack's helper RSS and the containers' memory
  accounting were different measures; neither alone established a cause for frame lag. The
  observed control plane did have repeated PostgreSQL authentication errors and missing
  historical telemetry exporters. See [research.md](research.md) for the initial measurements.
- A local application-network `asyncpg` attempt with the mounted credential failed while local
  `psql` trust authentication succeeded. The mounted API, scheduler and PostgreSQL secret files
  matched. This points to drift between the persisted `coire` role and Keychain credential;
  it does not justify deleting or reinitializing the data volume.
- Unit/contract suite before final changes: 741 passed, 8 skipped, 3 image-lock failures.
  The three failures were one missing `images.lock` entry for the imported pinned Grafana
  build base; after adding it, `tests/unit/test_pin_images.py` passed 4/4.
- Isolated release-manifest smoke test: candidate installed outside the source checkout,
  replaced first-party tags with local image IDs, copied cluster config, and passed
  `docker compose config --quiet` with no live container replacement.
- Isolated Docker bring-up/topology/restart checks: 49 passed, 1 skipped in 202.86 seconds.
  The first fixture attempt exposed its old direct Compose recreation path; the fixture now
  installs a new credential generation through `coire-up`.
- Isolated PostgreSQL drift/recovery test: 1 passed in 122.13 seconds. It seeded a row, proved
  network preflight refused a mismatched credential without selecting a new release, backed up
  and recovered the role, checked the row survived, and restored the original credential. Its
  first attempt exposed reuse of the old one-shot migration container; the release path now
  reuses identical generations and force-recreates services when credentials change.
- Web: 19 tests passed; ESLint, TypeScript and Vite production build passed. The hook test
  confirmed hidden-page abort and cursor resume. `uv run mypy packages/coire-core/src
  apps/coire-api/src scripts/coire-deploy.py` passed 134 source files. OpenAPI freshness
  check passed using the repository's actual `uv run python -m coire_api.openapi --check` entry.
- Final local unit/contract suite after the migration bridge and optional ops credential:
  759 passed, 8 skipped, 108 integration tests deselected. CI-equivalent
  `uv run mypy apps/ packages/` passed 369 source files; Ruff check/format,
  OpenAPI freshness and image pin checks passed. The eight skips remain explicit rather
  than being counted as passes.
- Production image build passed for the lean and diagnostics profiles. Image policy passed
  for API, scheduler, MCP, ops, web, collector, Prometheus, Alertmanager, Loki, Tempo and
  Grafana. Static Compose rendering passed for lean, ops/MCP, diagnostics and combined
  profiles, with disabled capabilities absent in each rendering.
- The final-image integration check loaded the baseline alert rules and returned
  authenticated dependency health.
- A repeated composed sharding failure exposed a genuine startup race: node reconciliation
  could mark a freshly persisted STARTING engine as dead before the node accepted its create
  request, although status polling already allowed a 30-second startup grace. The node
  reconciliation path now uses the same bounded grace. A focused unit test passed for a
  transient miss and an expired startup.
- The composed acquisition, placement and sharding path passed 12 consecutive cases after
  the startup-race fix. The next run case exposed a configuration omission from moving
  `RunCommandExecutor` into scheduler: the scheduler did not receive the integration
  `RUN_GATEWAY_URL` and sent its relay to the production DNS name. Compose now passes the
  selected gateway URL to scheduler in both production and integration, with a profile
  regression test. A proposed relay-timeout increase was reverted after finding the cause.
- Critical-severity Trivy scans and SPDX JSON SBOM generation passed for all eleven changed
  production images plus the temporary relay build. The relay source is back at its original
  timeout; the temporary registry was removed.
- The final monitoring images were rebuilt with upstream license text inside each image;
  Prometheus, Alertmanager, Loki, Tempo and Grafana all passed image policy, critical-severity
  scans and SPDX SBOM generation again after that change.
- After propagating `RUN_GATEWAY_URL` to scheduler, the isolated restart/run/kill and admin
  console integration modules passed 3/3 in 136.54 seconds. The two normal runs succeeded,
  and the deliberately slow third run reached `killed` within the existing five-second bound.
- The optional diagnostics profile started in an isolated `coirediag` project: PostgreSQL,
  API, scheduler, web, collector, Prometheus, Alertmanager, Loki, Tempo and Grafana all
  reported healthy. The socket proxy was running without a Docker healthcheck. The project,
  volumes and temporary credential state were removed after the check.
- A fresh PostgreSQL installation with the linked historical revision passed the isolated
  bring-up and baseline alert/health tests, 7/7 in 77.00 seconds. The changed migration image
  passed image policy, a critical-severity vulnerability scan and SPDX SBOM generation.
- Before live replacement, PostgreSQL held 2 models, 2 nodes, 15 audit rows and 2 download
  jobs at `0005_observability_health`; the named `coire_coire-pgdata` volume was present.
  A private, mode-0600 custom-format dump passed `pg_restore -l`. A disposable PostgreSQL
  restored the dump and upgraded through the linked historical migration to
  `0013_failover_event_receipts` with the 2 model, 2 node and 15 audit rows preserved.
  No live database row or role had been changed at this point.
- The pre-rollout 60-second log sample contained 17 PostgreSQL authentication failures in
  API logs and 82 collector exporter failure/retry lines. A single `docker stats` sample
  showed API 79.53 MiB/8.39% CPU, scheduler 47.79 MiB/8.23%, MCP 54.36 MiB/8.61%, web
  14.71 MiB/0.92%, Grafana 292.4 MiB/0.20%, collector 24.1 MiB/0.08%, and PostgreSQL
  34.21 MiB/0%. These are container samples, not desktop frame measurements.
- The first live `coire-up --recover-db-role` refused to proceed before any service or data
  change because Keychain lacked `bootstrap_admin_email` and `ops_service_token`.
  The ops token was found unnecessary for lean mode and made conditional on the `ops`
  profile, with a focused unit test. The bootstrap email remains a required operator input.

## Final review and pending live gate

- The final Spec Kit consistency analysis found coverage for all 13 functional requirements
  and all eight success criteria, with no unresolved constitution conflict. Four wording and
  topology mismatches in the design artifacts were corrected. `git diff --check` passed.
- The broader isolated workflows, image policy, critical-severity vulnerability scans and
  SPDX SBOM generation passed as recorded above. No tiny real-model integration was run;
  there is no tiny test model available on this Mini, and no Studio engine was started.
- Before the operator supplied the bootstrap admin email, the live database and containers
  remained unchanged and the recoverable dump and named volumes were retained.
- The operator provided the bootstrap email, which was added to Keychain along with the
  previously absent ops credential while all existing credentials were retained. CI's first
  integration run exposed a clean-host omission: the installed release used `--pull never`
  before the pinned third-party socket proxy image was available. Startup now pulls only
  the digest-pinned PostgreSQL and socket proxy images when missing, while first-party
  release image IDs remain immutable. The integration rerun is pending.

## Live Mini rollout

- With the operator-supplied email in Keychain, `coire-up --recover-db-role --no-build`
  installed the lean release and completed successfully. The pinned external images were
  already cached locally, so the new missing-only pull skipped both. PostgreSQL, API,
  scheduler, web, socket proxy, collector, Prometheus and Alertmanager started; seven of
  these report Docker healthy, and the socket proxy runs without a Compose healthcheck.
- The live database is at `0013_failover_event_receipts`; the existing 2 models, 2 nodes
  and 2 download jobs remain. The audit log rose from 15 to 16 rows during bootstrap, and
  there is one user. The `coire_coire-pgdata` volume is still mounted, and the private
  pre-rollout dump remains under the project backups directory. The role-recovery path
  was available but the installed credential passed the preflight, so no role change was
  required during this invocation.
- Authenticated `/health` from inside the API returned HTTP 200. PostgreSQL, scheduler,
  collector, Prometheus and Alertmanager were healthy. The overall status was `degraded`
  because both Studio nodes were already recorded unreachable; this rollout did not start
  or change Studio engines. The host could not reach the existing VLAN-bound
  `192.168.4.10:8180` address through its current network route, so external access was
  not asserted from this Mac session.
- A post-rollout 60-second log sample found zero authentication or exporter error matches
  across API, scheduler and collector, compared with 17 API authentication failures and
  82 collector retry/failure lines in the pre-rollout sample. One `docker stats` sample
  showed API 103.4 MiB/0.47% CPU, scheduler 101.6 MiB/3.94%, web 11.62 MiB/0%,
  PostgreSQL 78.96 MiB/0.52%, Prometheus 100 MiB/0.21%, Alertmanager 29.29 MiB/0.10%,
  collector 25.09 MiB/0.04% and socket proxy 28.45 MiB/0%. Samples are not desktop
  frame measurements and were taken at different workload moments.
- After replacement health, removed only six Coire project containers: disabled MCP,
  Grafana, Loki and Tempo, the old created API orphan, and the exited one-shot migration
  container. The eight lean containers remain running. All Coire named volumes, including
  historical monitoring volumes, were preserved; no unrelated container was removed.
