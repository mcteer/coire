# Review: Container Run Orchestration on the Studios

**Reviewed**: 2026-09-01 09:15 MDT
**Branch**: `feat/011-container-run-orchestration`
**Status**: automated implementation and convergence gates pass; real-cluster acceptance remains operator-only

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
| `uv run mypy apps/ packages/` | pass; 293 source files |
| `uv run pytest -q -m 'not integration'` | 601 passed, 8 skipped, 102 deselected |
| `COIRE_INTEGRATION=1 uv run pytest -q -m integration` | 101 passed, 1 third-party/environment skip, 609 deselected; 556.35 s |
| Local real-Docker run lifecycle | pass; direct scenarios cover create/replay/start/wait/log/collect, fixed internal relay, hardened inspect, immediate token denial, sub-5-second kill, owner-scoped orphan reap, and cleanup. The composed scenario proves scheduler restart with exact in-flight container identity, one-slot FIFO capacity, token-safe ambiguous-create recovery, terminal results, and runner/relay cleanup. |
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

The implementation covers every contract and automated implementation task. The composed recovery
scenario restarts the actual scheduler process mid-run, preserves the exact container identity,
keeps the successor queued under a one-slot cap, completes both runs in FIFO order, and leaves no
runner or relay artifact. Regression coverage also proves two-phase authoritative reconciliation,
relay-only orphan discovery, node-owner scoping, bounded wait transport timeouts, and token-safe
recovery after an ambiguous create response. T041, T052, and T053 are complete.

## Real-cluster evidence

T054 remains open by design. Repository policy prohibits a development agent or CI from running
Docker or engine commands against the real Studios. An operator must execute `quickstart.md` after
the feature images are published and record only non-secret run IDs, timestamps, selected node,
kill latency, recovery identity, and core image/container absence here.
