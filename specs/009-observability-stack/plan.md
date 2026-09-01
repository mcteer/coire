# Implementation Plan: Observability Stack

**Branch**: `feat/009-observability-stack` | **Date**: 2026-09-01 | **Spec**: `specs/009-observability-stack/spec.md`

## Summary

Add a fully local OTel pipeline and resource-bounded Prometheus, Loki, Tempo, Grafana, and Alertmanager services to the core compose project. Extend shared contracts and API/node instrumentation with truthful sampled host telemetry, damped heartbeat health, freshness enforcement, bounded labels, trace propagation, and stage spans. Provision three linked dashboards and the complete alert set, then validate failure isolation and induced slow/failure paths.

## Technical Context

**Language/Version**: Python 3.13; YAML; provisioned Grafana JSON
**Primary Dependencies**: Existing OpenTelemetry SDK/exporters, FastAPI, psutil; pinned Prometheus, Alertmanager, Loki, Tempo, Grafana, OTel Collector images
**Storage**: Existing Postgres node rows plus separate local volumes for metrics, logs, traces, and Grafana; seven-day retention
**Testing**: pytest unit/contract/integration, compose validation, promtool, browser validation, image policy/SBOM/CVE gates
**Target Platform**: core on OrbStack; native launchd agents on two Apple Silicon Studios
**Project Type**: distributed control plane and native node agent
**Performance Goals**: 15 s node refresh; collection below 3% of one CPU core and 128 MiB RSS; alerts within their evaluation windows
**Constraints**: local fail-open telemetry; no resident privileged helper; bounded cardinality; no credentials or content; constrained core services
**Scale/Scope**: three hosts, two agents, six observability containers, three dashboards, eleven alert conditions

## Constitution Check

*GATE: passed before research and re-checked after design.*

- **I — Bare Engines**: instrumentation wraps Coire boundaries; no inference wrapper or exposed engine port.
- **II — Core/Studio roles**: backends are core containers; Studios receive only bounded native agent collection.
- **II-a — One service, one container**: every backend is separate, pinned, least-privileged, read-only where supported, healthchecked, and telemetry-network-only.
- **III — Contracts first**: health, sampling, and process wire shapes begin in `coire-core`; OpenAPI and TS types are refreshed.
- **IV — Zero implicit trust**: health remains authenticated, Grafana is loopback/operator-only, fields are allowlisted, and secrets/content excluded.
- **V — Models are data**: only registry IDs appear; telemetry never causes acquisition.
- **VI — Observable or it does not ship**: local signals, stage spans, dashboards, and alerts are the deliverable.
- **VII — Spec-driven/test-gated**: contract, unit, integration, browser, image, scan, and live budget checks are tasks.

No exception is required. Core reachability and service/container metrics come from API and compose targets. This feature does not pretend container metrics are macOS host telemetry; Studio macOS resources come from native agents.

## Project Structure

```text
specs/009-observability-stack/{plan.md,research.md,data-model.md,quickstart.md,contracts/,tasks.md}
packages/coire-core/src/coire_core/models/
apps/coire-api/src/coire_api/
apps/coire-node/src/coire_node/
deploy/compose/{compose.yaml,otel-collector.yaml,prometheus/,alertmanager/,loki/,tempo/,grafana/}
deploy/observability/
docs/runbooks/
tests/
```

## Design Phases

1. Define health/sampling/process contracts and freshness/state invariants.
2. Build fail-open signal routing, redaction, propagation, and stage instrumentation.
3. Add damped health evaluation and stale-health exclusion.
4. Add six resource-constrained services with local storage and retention.
5. Provision linked dashboards, recording rules, grouped alerts, and runbooks.
6. Run code, compose, browser, image/SBOM/scan, integration, and Studio budget gates.

## Post-Design Constitution Re-check

Passed. Contracts precede routes, roles remain isolated, Studio work stays in the existing agent, telemetry cannot gate serving, and every service has explicit hardening and verification work.
