# ADR-0007: Lean control plane with optional historical diagnostics

- **Status**: Accepted for feature 023
- **Date**: 2026-09-26
- **Deciders**: Coire maintainer (feature 023 request)
- **Constitution**: Principles II, II-a, IV, VI and VII; version 2.0.0

## Context

The M4 Pro Mini has 24 GiB unified memory and is also the interactive macOS desktop. The full
design keeps Prometheus, Alertmanager, Loki, Tempo and Grafana running alongside the collector,
database, gateway, scheduler, web, ops and MCP services. The live installation showed no swap or
sustained host CPU saturation during inspection, so its desktop lag has no proven cause. The
installation nevertheless had four stopped observability backends and a collector retrying them,
while configuration files were mounted from a changing development checkout.

This three-host lab needs continuous actionable health, a complete security/mutation audit, and
the ability to inspect detailed traces when diagnosing incidents. It does not need every history
backend to consume resources continuously while unused.

## Decision

- The default Compose profile runs API, scheduler, web, PostgreSQL, collector, Prometheus and
  Alertmanager. Remote ingress is configured separately where used.
- MCP, ops and container management can be enabled explicitly. They retain separate images,
  non-root execution, network boundaries, health checks and privilege scopes.
- Loki, Tempo and Grafana form an explicit local diagnostics profile. Exporters are enabled only
  with their destinations. Logs, traces and metrics never leave the lab.
- Every service remains instrumented. The baseline retains metrics, actionable alerts, complete
  audit records and bounded local structured logs. With diagnostics disabled, historical trace
  and central log search are unavailable; operators see that limitation in the UI/runbook.
- Each feature still defines its dashboard panel and alert rule. The panel is visible when the
  diagnostics profile runs; Alertmanager continues evaluating baseline rules.
- Container/image resource bounds and log retention are tested against actual workload startup.
  Host-wide OrbStack ceilings are operator trials, never silently changed by deployment.

## Consequences and validation

The baseline has fewer always-running services and no exporter retrying a missing backend.
Incident investigation may require enabling diagnostics before reproduction; earlier detailed
traces cannot be recovered. Audit history and metric alerts are retained. Feature 023 tests both
profile combinations, exporter destinations, backend health, alert loading, and the UI/runbook
notice. A service remains independently restartable in either profile.

The old feature 009 documents its original always-running retention target. Feature 023
supersedes that default after its implementation; historical specifications remain evidence of
their design context. No Studio model/engine, database, or user harness moves to core.

## Alternatives considered

- Keep every backend running: preserves continuous full history but retains idle runtime and
  deployment coupling that this lab does not currently need.
- Eliminate the collector and monitoring: would remove actionable health, metrics and alerting.
- Move telemetry to the Studios: changes their workload isolation and failover assumptions.
- Run all core services natively: eliminates OrbStack only after a much larger security,
  packaging and recovery migration; no measured evidence currently justifies it.
