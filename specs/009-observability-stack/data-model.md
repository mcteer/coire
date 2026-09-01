# Data Model: Observability Stack

## NodeHealthObservation

Fields: declared `node_name`; timezone-aware `sampled_at`, `received_at`, `fresh_until`; heartbeat latency; optional bounded CPU/GPU percentages; memory used/budget/reserved; thermal state; bounded processes; collection CPU/RSS/duration/budget verdict; and `process_state_verified`.

Validation: percentages are 0–100, byte counts non-negative, timestamps aware, and stale observations never placement-eligible.

## NodeHealthState

Fields: `verdict` (`healthy | degraded | unreachable | unknown`), bounded reason, last success/observation, seconds since heartbeat, success/failure/degraded counters, and derived freshness.

Transitions:

```text
unknown --3 successful fresh probes--> healthy
healthy --3 threshold breaches-------> degraded
healthy/degraded --3 failed probes---> unreachable
degraded/unreachable --5 successes---> healthy
any --freshness expiry---------------> unknown
```

## ProcessObservation

Fields: positive PID, bounded engine/job kind, optional registry model slug, CPU percentage, RSS bytes, and verification time.

## ClusterHealth

The aggregate verdict combines critical control-plane state, node health plus observations, and fresh interconnect state. Critical database failure remains `unhealthy`; lesser failures preserve degraded/unreachable/unknown distinctions.

## Trace

W3C trace/parent IDs with required spans `coire.api.gateway`, `coire.scheduler.admission_wait`, `coire.node.load_wait`, `coire.node.prefill`, `coire.node.decode`, and `coire.node.network`. High-cardinality IDs are span fields, not metric labels.

## AlertRule and Dashboard

An alert has a stable name, PromQL condition, persistence window, severity, subject label, and dashboard/runbook annotations. A dashboard has a stable UID/title/version, provisioned panels/variables, and Tempo/Loki data links.
