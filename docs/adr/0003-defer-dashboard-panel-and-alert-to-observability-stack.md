# ADR-0003: Defer feature 000's dashboard panel and alert rule to the observability stack

- **Status**: Accepted
- **Date**: 2026-08-29
- **Deciders**: Dan McTeer
- **Constitution**: exception to **Principle VI** (Observable or it doesn't ship — "a feature is not complete until its dashboard panel and at least one alert rule exist")
- **Time-box**: closes with feature 009 (observability stack), whose acceptance MUST include a bootstrap-health panel and the node-unreachable alert

## Context

Principle VI requires every feature to ship with a dashboard panel and an alert rule. Feature
000 is the skeleton that the metrics, logs and traces backends will later run in; those
backends — Prometheus, Loki, Tempo, Grafana, Alertmanager — are feature 009. There is nowhere
for a panel or a rule to exist yet.

## Decision

1. Feature 000 wires **export**: `coire-api`, the two stubs and `coire-node` emit OTLP traces
   and metrics to the collector container (FR-014), and the collector's health extension is
   itself probed by `/health`. Nothing observable is lost; it is buffered at the collector's
   debug exporter until 009 attaches backends.
2. The panel and rule are **owed, not waived**. Feature 009's spec already carries them:
   its cluster dashboard must show per-node liveness and the `/health` aggregate, and its
   alert set includes node unreachable and — for this feature's FR-013c — the
   `coire_fallback_requests_total` counter crossing zero.
3. Feature 009's Constitution Check MUST record this ADR as **closed** and cite the panel and
   rule that close it.

## Consequences

- Until 009, the only way to see bootstrap health is `/health`, `docker compose ps/logs`, and
  the collector's debug output — documented in `docs/runbooks/bootstrap.md`.
- The counter and WARNING for egress fallback (FR-013c) exist from 000; the *alert* on them is
  009's.

## Alternatives rejected

- Pulling Grafana and Prometheus into 000 — turns "skeleton" into half of 009 and duplicates it.
- Skipping OTLP export until 009 — would force 009 to retrofit instrumentation into every service.
