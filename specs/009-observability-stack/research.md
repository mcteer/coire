# Research: Observability Stack

## R1 — Local signal pipeline

**Decision**: All producers send OTLP/gRPC to the existing collector. It exposes metrics for Prometheus and exports traces/logs to local Tempo/Loki. No exporter has an Internet destination.

**Rationale**: One fail-open ingress centralizes batching, redaction, memory limiting, and retries.

**Alternatives considered**: Direct SDK export duplicates failure behavior; cloud telemetry violates FR-002.

## R2 — Retention and storage

**Decision**: Prometheus retains seven days; Tempo local blocks use 168-hour compaction; Loki uses TSDB/filesystem storage with compactor retention of 168 hours. Each signal has a configurable setting and separate volume.

**Rationale**: Official Loki guidance requires compactor retention for TSDB and a 24-hour index period. Separate volumes isolate growth and rollback.

**Alternatives considered**: Infinite retention and shared volumes.

## R3 — Cardinality

**Decision**: Labels are limited to service, node, operation, model slug, outcome, placement, and bounded identity class. Raw user/key/run/job/instance IDs are trace/log fields.

**Rationale**: Aggregate questions remain answerable without unbounded Prometheus series.

**Alternatives considered**: Labels per identity can exhaust core storage.

## R4 — Logs and content policy

**Decision**: Record operational metadata only. Never record bodies, authorization headers, cookies, node tokens, API keys, or secret values. The collector deletes sensitive keys defensively.

**Rationale**: Metadata diagnoses operations without creating a sensitive content store.

**Alternatives considered**: Opt-in body capture adds consent and access-control scope.

## R5 — Health semantics

**Decision**: Store latest observation plus counters. Three failures/degraded evaluations enter failure; five successes recover; expired observations become unknown; responding over CPU/memory/thermal/latency limits is degraded.

**Rationale**: Asymmetric hysteresis distinguishes dead, busy, and stale and prevents thrash.

**Alternatives considered**: Instant transitions and last-known-good.

## R6 — Clock correctness

**Decision**: The API uses its receipt time for freshness and records node time separately; their delta is a clock-skew metric.

**Rationale**: A fast node clock cannot make stale data current.

**Alternatives considered**: Trusting node timestamps.

## R7 — Studio budget

**Decision**: Keep collection in `coire-node`; use psutil and unprivileged IOKit; measure CPU/RSS/duration; exponentially back off optional GPU/process sampling when over budget while essential heartbeat/memory continues.

**Rationale**: Inference wins contention without a privileged resident helper.

**Alternatives considered**: Continuous `powermetrics` and a second Studio service.

## R8 — Alerts and dashboards

**Decision**: Prometheus rules use persistence windows and Alertmanager grouping by alert name, cluster, and subject. Provision immutable Cluster, Traffic, Jobs dashboards and local data sources with trace/log links.

**Rationale**: Native rules are promtool-testable; version-controlled dashboards reproduce cleanly.

**Alternatives considered**: Grafana-managed alerts and hand-built dashboards.

## R9 — Dependencies and licences

**Decision**: Add no Python package. Use existing Apache-2.0 OTel libraries and pinned upstream releases. Prometheus/Alertmanager are Apache-2.0; Grafana/Loki/Tempo are AGPL-3.0 and remain isolated services used over their public interfaces. Record source tags, base digests, SBOMs, and scans in the PR.

**Rationale**: These are isolated infrastructure services, not linked application libraries.

**Alternatives considered**: Reimplementing storage/UI.

The node adds `opentelemetry-instrumentation-fastapi` (Apache-2.0), already used by coire-api and
already present in the workspace lock, because provider setup alone does not extract W3C context or
create inbound HTTP spans. No new licence family or external runtime service is introduced.

The published Prometheus 3.14.0, Alertmanager 0.33.0, Loki 3.7.4, Tempo 2.10.7, and Grafana 13.2.0 binaries still contained CVE-2026-56854 at implementation time. Coire therefore builds those exact upstream sources with only `golang.org/x/crypto` replaced by fixed v0.55.0. Prometheus, Alertmanager, Loki, and Tempo become non-root scratch images; Grafana retains its pinned upstream runtime and frontend but replaces the main binary. Unused vulnerable Grafana datasource executables are removed. The required Loki datasource is Grafana's signed 13.1.1 release, which already contains the fix, so plugin signature enforcement remains enabled. These narrow builds are required by the zero-CRITICAL gate and are rechecked by Trivy and SPDX SBOM generation.

Pinned build/runtime inputs are Go 1.26 Bookworm
`sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514`
and Grafana 13.2.0
`sha256:3fd54ae1214669f8355f065ec9f6445d5279a3d77095ab048ca045685272429b`.
Runtime ceilings are Prometheus 768 MiB, Alertmanager 128 MiB, Loki 768 MiB, Tempo 768 MiB,
Grafana 512 MiB, and the collector 512 MiB.
