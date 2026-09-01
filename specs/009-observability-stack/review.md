# Feature 009 Review Evidence

**Reviewed**: 2026-09-01
**Branch**: `feat/009-observability-stack`

## Acceptance evidence

- Full integration suite after convergence: `84 passed, 1 skipped`, including an explicit collector-stop test
  that kept five consecutive control-plane requests successful. The cold 512-token streaming
  case produced one Tempo trace containing
  gateway, admission, load-wait, prefill, decode, and network spans and attributed the induced
  delay to decode.
- Alert rules: Prometheus 3.14 `promtool test rules` passed induced cases for all eleven alert
  families, including subject labels and dashboard/runbook annotations.
- Live stack: all twelve core services were healthy. Prometheus, Loki, and Tempo datasource
  health checks returned success through Grafana.
- Browser: WebKit rendered Cluster (5 panels), Traffic (3), and Jobs (3) at 1440x900 and
  390x844. A Jobs navigation immediately after first startup hit a transient frontend asset
  load page; three immediate repeat trials rendered all panels successfully.
- Code gates: Ruff formatting/lint, strict mypy (122 files), non-integration pytest
  (`409 passed, 8 skipped`), web tests/lint/build, OpenAPI freshness, compose rendering, and
  image pin checks passed.
- Supply chain: all eleven compose production images were built for arm64, passed the image
  hardening policy, produced SPDX SBOMs, and scanned with zero CRITICAL Trivy findings. The
  five telemetry images use patched source builds and scratch runtimes.

## Studio collection budget

A bounded 512-token synthetic request was sent through the authenticated Coire gateway to the
already-running tiny model on edge-a; no engine or container was started or changed. During
that request, the existing node-agent processes were sampled 40 times at 250 ms intervals:

| Node | Average CPU | Peak CPU | Average RSS | Peak RSS |
|---|---:|---:|---:|---:|
| coire-edge-a | 1.155% of one core | 14.9% transient | 97.22 MiB | 97.25 MiB |
| coire-edge-b | 2.105% of one core | 21.3% transient | 94.70 MiB | 94.70 MiB |

Both averages meet the plan's `<3% of one CPU core` and `<128 MiB RSS` collection budget.
The optional sampler's unit tests also verify exponential backoff when a collection exceeds
its configured duration budget.

## Operational notes

- Telemetry remains local and fail-open; no serving service depends on the collector.
- Convergence added live token/queue/throughput/unload metrics, explicit aggregate link health,
  a configurable local tunnel readiness producer, controlled stage-delay traces, composed
  health hysteresis, and hard default ingestion ceilings totalling 92.48 GB over seven days.
- The live node-offline test found and fixed a stale-truth defect: failed probes no longer
  refresh the receipt timestamp of the previous successful observation.
- Prometheus runtime configuration names only `coire-platform.yml`, keeping the adjacent
  `promtool` test fixture out of the live rule loader.
- Rollback, induced-alert procedures, retention controls, content policy, and dashboard/rule
  extension conventions are documented in `docs/runbooks/observability.md`.
