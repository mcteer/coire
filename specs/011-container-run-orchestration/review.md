# Review: Container Run Orchestration on the Studios

**Reviewed**: 2026-09-01 07:30 MDT
**Branch**: `feat/011-container-run-orchestration`
**Status**: automated implementation gates pass; real-cluster acceptance remains operator-only

## Constitution check

- **I**: runners reach only the authenticated Coire gateway; neither runner nor relay controls an engine.
- **II**: placement admits `NodeRole.STUDIO` only; topology tests reject a core Docker socket or user harness service/image dependency.
- **II-a**: the agent and relay are separate digest-pinned, non-root, distroless images. The runner is read-only, capability-free, no-new-privileges, resource-limited, and attached only to its per-run internal network.
- **III**: run, token, command, result, reconciliation, and problem shapes originate in `coire-core`; OpenAPI and generated TypeScript are fresh.
- **IV**: run tokens are Argon2id-hashed at rest, scoped, server-time-expiring, atomically charged, and revoked before kill contact; mutations and terminal outcomes are audited.
- **V**: admission resolves registry UUIDs and requires a published, validated, harness-verified variant. No caller model path or acquisition trigger reaches an engine.
- **VI**: API, scheduler, and node spans, bounded metrics, structured identifiers, dashboard panels, and alerts cover the new paths without logging token or content values.
- **VII**: unit, contract, migration, image, and local Docker integration gates pass. The mandated real-Studio exercise is not executable from the development environment and remains T054.

No exception was introduced.

## Automated evidence

| Gate | Result |
|---|---|
| `uv run ruff format` / `uv run ruff check` | pass; 461 files formatted/checked |
| `uv run mypy apps/ packages/` | pass; 292 source files |
| `uv run pytest -q -m 'not integration'` | 598 passed, 8 skipped, 100 deselected |
| `COIRE_INTEGRATION=1 uv run pytest -q -m integration` | 99 passed, 1 third-party/environment skip, 606 deselected; 488.86 s |
| Local real-Docker run lifecycle | pass; 2 scenarios cover create/replay/start/wait/log/collect, fixed internal relay, hardened inspect, stable in-flight container identity, immediate token denial, sub-5-second kill, orphan reap, and cleanup |
| Web `test`, `lint`, `build` | 9 files / 11 tests passed; ESLint and production build passed |
| `python -m coire_api.openapi --check` | pass |
| PostgreSQL migration `upgrade head → downgrade -1 → upgrade head` | pass on disposable PostgreSQL 17; final head `0011_container_runs` |
| `scripts/pin-images.sh --check` | pass |
| `scripts/image-policy.sh coire-agent:ci` | pass |
| `scripts/image-policy.sh coire-run-relay:ci` | pass |
| Trivy critical scan, both images | pass; zero critical findings |
| Syft SPDX 2.3, `coire-agent:ci` | pass; 1,210,567-byte valid SBOM generated outside repository |
| Syft SPDX 2.3, `coire-run-relay:ci` | pass; 1,279,552-byte valid SBOM generated outside repository |

The SBOM files are transient release evidence under `/tmp`; CI regenerates and publishes image
artifacts, so generated SBOMs are not committed.

## Convergence findings

The implementation now covers every contract and automated implementation task except T041/T052's
full control-plane recovery scenario. The local Docker tests prove node-level exact container
identity during an in-flight replay, immediate token denial before a sub-five-second live kill,
and actual label-scoped orphan reaping. Unit and contract tests prove strict FIFO queueing, DBOS
command identity, revocation-before-contact, and ownership independently. A single integrated
scenario must still restart the actual scheduler process mid-run and observe the capacity-queued
successor without a duplicate container. This remains T041 and prevents T052/T053 from being
marked complete.

## Real-cluster evidence

T054 remains open by design. Repository policy prohibits a development agent or CI from running
Docker or engine commands against the real Studios. An operator must execute `quickstart.md` after
the feature images are published and record only non-secret run IDs, timestamps, selected node,
kill latency, recovery identity, and core image/container absence here.
