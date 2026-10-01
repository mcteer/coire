# Implementation Plan: Content-free image telemetry

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | No engine, model load or container added. |
| III | Telemetry enums are internal, not wire shapes. |
| IV | Metrics omit user content, secrets and high-cardinality IDs. |
| V | No acquisition behavior. |
| VI | API/node spans, metrics, dashboard and alert are included. |
| VII | Tests first; later services must use these helpers. |

## Approach

Create enum-only API and node recording functions. Keep IDs in structured logs and spans only where needed, never in metric attributes. Add Prometheus rules and a provisioned Grafana dashboard. Tests capture label maps and reject arbitrary strings or sensitive keys.
