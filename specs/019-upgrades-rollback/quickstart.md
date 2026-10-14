# Validation Guide: 019 upgrades and rollback

This guide describes the implemented validation paths. Record exact source and candidate artifact identities, commands and results in `execution-record.md`. Validation of a working tree must name its built content identities; repeat affected checks after any implementation change and record the final reviewed commit before release. Earlier fixture results do not substitute for current acceptance.

## Prerequisites

- Read AGENTS.md, constitution, spec, plan and contracts. Use `feat/019-upgrades-rollback-plan` (or its explicitly recorded successor), never the dirty obsolete `coire-019` worktree.
- Complete the recorded executor-network permission gate before changing deployment networking.
- Use `uv sync --all-packages`; preserve pinned dependencies and licensing. A separate executor image is required, with no shell/Docker CLI.
- Prepare source and candidate releases from trusted builds, with full manifests, local images and declared compatibility. Install and verify node/Core recovery bootstrap before enabling managed upgrades.
- Keep Keychain-backed credentials and all fixtures/artifacts outside Git. Use isolated project names/volumes for CI. Never point CI at the real Studios.
- For native integration, set `COIRE_TEST_MODEL` to an admin-acquired, manifest-verified tiny model of at most 1 GB. Prepare its registry record through existing admin tooling. No test or upgrade may trigger an implicit download.
- Core migration acceptance needs a protected backup/validation receipt at most 24 hours old, matching project and source schema. After bootstrap establishes committed shared deployment ownership, run `uv run python scripts/coire-deploy.py prepare-backup /private/operator/backup-catalog --publisher-image sha256:<verified-executor-image-id>`. The catalog must be outside Git. This fixed mode dumps the project PostgreSQL 17 database, restores it into a disconnected disposable container using the existing immutable PostgreSQL image, verifies the same structural schema digest used by activation, and publishes protected bytes under the service UID. It never restores production or rotates credentials. The tested bound is a 4 GiB dump and a 4 GiB disposable restore data directory; larger databases require an explicitly tested extension before use.

## Local automated gates

Run focused checks as each story lands, then the full required gates once the implementation is stable:

```bash
uv run ruff format --check
uv run ruff check
uv run mypy apps/ packages/ scripts/coire-deploy.py
uv run pytest -q
uv run python -m coire_api.openapi --check
pnpm -C apps/coire-web test
pnpm -C apps/coire-web lint
pnpm -C apps/coire-web build
```

Use the existing repository OpenAPI/TypeScript generation scripts before freshness checks. Follow existing CI entrypoints for image build, SBOM and scan. Do not weaken topology tests: update their expected narrow executor role with authorization and test forbidden paths.

For isolated stateful/native checks, run the implemented suites with their documented fixture setup:

```bash
uv run pytest -q tests/integration/test_upgrade_persistence.py
docker build -f apps/coire-upgrade-executor/Dockerfile -t coire-019-upgrade-executor:dev .
uv run pytest -q tests/integration/test_upgrade_backup_preparation.py
uv run pytest -q tests/integration/test_upgrade_control_plane.py
uv run pytest -q tests/integration/test_upgrade_bootstrap.py
uv run pytest -q -m integration tests/integration/test_upgrade_node.py
```

Missing Docker, PostgreSQL, tiny assets or native hardware are concrete prerequisites to provision through authorized workflows. Record and resolve them; skipped required tests do not count as acceptance.

## Story scenarios

| Scenario | Procedure | Required evidence |
|---|---|---|
| US1 broken candidate | Submit a deliberately invalid prepared candidate; separately inject failure after pointer selection and after agent restart. | Source digest and files intact/restored; fresh ten-token request succeeds; bounded failure output; no leaked PID/port/holds. |
| US1 crash recovery | Interrupt agent, recovery helper and isolated host at every durable boundary; replay commands and reuse an old PID. | One effect per generation; bootstrap runs before new work; exact source restored or explicit recovery-required fencing. |
| US2 successful change | Upgrade one Studio to a valid prepared release, verify exact identity, request compatible rollback. | Ten-token smoke on both transitions, source retained, actor/outcome audited. |
| US2 qualification | Change content with the same package version; run real paired collective with both exact runtime identities. | Stale training/Apply/link qualifications cannot authorize work; historical results unchanged; pair remains blocked until fresh evidence. |
| US2 busy and racing work | Start each owner class from contracts/node.md, request upgrade, then race another admission. | Existing work completes normally; new work rejected; drain timeout aborts unchanged; unknown owners and unreleased holds block. |
| US3 migration | Use compatible additive migration, failed transaction and ambiguous partial-outcome fixtures. | Expected schema; zero replacements on failure; no production downgrade or destructive restore; uncertain state stays fenced. |
| US3 rollout failure | Fail each service in turn, especially API and scheduler; kill executor and reboot isolated host. | Every stopped source restored in reverse order; exact source images and mount/secret identities; durable replay after API recovery. |
| US3 deployment race | Race coire-up/down/cleanup against executor; remove source checkout; attempt another project/volume/exec path. | Typed ownership excludes races; source-independent recovery works; foreign effects and topology changes rejected. |
| US3 bootstrap handover | Install first bootstrap, then replace executor with healthy and unhealthy candidates; restart both/host in each phase. | Initial not-ready state is safe; incumbent survives until candidate owns recovery; one writer; automatic restoration on failed handover. |
| US3 availability | Stream 100 requests while restarting unrelated services; then exercise gateway maintenance and election hand-back. | Zero unrelated stream drops; positive drain; retry guidance while ingress lives; successful outage ≤300 s; valid lease before reopening. |
| US4 operators | Use console and CLI for versions/start/status/cancel/rollback; disconnect SSE and retry start. | Same job, bounded replay ≤10 s after readiness, truthful rolled-back state, keyboard flow and no duplicate mutation. |
| US4 security | Try user/run/ops credentials, revoked admin, stale generation, changed idempotency payload and tampered artifacts. | Rejected before unauthorized side effects; redacted security/audit evidence. |

## Authorized real-cluster acceptance

The implementation agent is authorized by AGENTS.md to run required workloads on the Studios through authenticated admin, gateway and node paths. Record prerequisites first; never interpret the CI isolation rule as a ban on this acceptance.

1. Record source/target/environment/image digests, source state and both Studios' workload/hold inventory. Confirm failover state, prepared tiny assets, retained source and backup evidence.
2. Demonstrate existing training/measurement and representative image/agent work drain without forced termination. Exercise timeout/refusal while work remains active. Verify normal work continues after an aborted upgrade.
3. Upgrade one Studio only. Prove single-node serving, paired collective and qualification invalidation for exact runtime identities. Confirm no concurrent activation is possible.
4. Exercise deliberately failed candidate and compatible manual rollback. Measure detection-to-restoration time, record the exact source identity and verify a fresh request. Do not damage a running user's release; use prepared test candidates and the explicit maintenance protocol.
5. Run the Core compatible rollout and controlled failure cases in an approved maintenance window; verify API/scheduler recovery, credentials, release pointer, all changed services, failover hand-back and preserved history.
6. Verify published dataset/adapter hashes, feedback capture/opt-out generations, pending unexported purge obligations and native training/evaluation records before and after. Do not copy private content into evidence.
7. Verify dashboard and alerts with diagnostics enabled and disabled; confirm audit remains complete. Restore the intended release/configuration, remove only owned test artifacts and confirm all test resource holds are terminal/released.

The execution record must report actual commands, durations, identities, counts and check outcomes, with secrets redacted. A plan or synthetic test is not evidence of real-cluster acceptance.
