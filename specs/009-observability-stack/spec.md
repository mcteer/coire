# Feature Specification: Observability Stack

**Feature Branch**: `009-observability-stack`

**Roadmap ID**: 007 (Phase 2 — Identity, users, admin)

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "OTel/Prometheus/Loki/Tempo/Grafana compose, instrumentation across api/node, three seed dashboards, alert rules from the architecture doc."

## Overview

Principle VI says a feature is not complete until its dashboard panel and at least one alert rule exist. This feature builds the place those panels and rules live: the metrics, logs, and traces stack on core, instrumentation across the control plane and node agents, three seed dashboards covering the cluster, traffic, and jobs, and the alert rules the architecture enumerates. Its acceptance bar is diagnostic rather than cosmetic — a deliberately slow request must be attributable to a specific span.

## Clarifications

### Session 2026-08-29

- Q: Does telemetry leave the network? → A: No. Everything exports to the local collector on core and stays there. The instrumentation layer used for the API and agent harness is treated purely as an OpenTelemetry instrumentation library exporting locally; no cloud account is involved and no telemetry egresses.
- Q: What retention is expected? → A: Seven days by default for metrics, logs, and traces, chosen because core has 24 GB of memory and 460 GB of disk and the observability stack shares it with Postgres and the whole control plane. Retention is configurable per signal so it can be tuned or moved to Studio SSDs later.
- Q: Which spans must exist for the acceptance bar to be met? → A: A request must be decomposable into gateway handling, queueing or admission wait, model load wait, prefill, decode, and node-to-node network time where sharded. Those are the six places latency actually goes, and a trace that cannot separate them cannot answer the question the bar asks.
- Q: What is the minimum alert set? → A: Node unreachable, interconnect peer down, memory ledger drift beyond threshold, model load exceeding a duration, agent run exceeding its timeout, and tunnel down. These come directly from the architecture and each corresponds to a failure that is silent without alerting.
- Q: How do later features satisfy Principle VI? → A: This feature establishes the dashboards, the alert-rule mechanism, and a documented convention for adding a panel and a rule. Later features add their own panel and rule to that structure rather than building anything new.

### Session 2026-08-29 (follow-up: end-to-end cluster health)

- Q: Is link health sufficient to describe cluster health? → A: No. Cluster health is the conjunction of node liveness, live per-node CPU and memory utilisation, per-process resource use, thermal state, interconnect health, and control-plane reachability. A cluster whose link is healthy but whose node is saturated or thermally throttled is not healthy, and a monitor that only watches the link cannot say so.
- Q: How is a slow node distinguished from a dead one? → A: By separating liveness from responsiveness. A heartbeat on a short interval establishes liveness; observed latency and utilisation establish responsiveness. A node that heartbeats but misses its latency budget is `degraded`, not `unreachable`, and only sustained heartbeat loss beyond a threshold marks it `unreachable`. This distinction is a known failure mode in comparable local clusters, where coordinators that cannot separate the two either strand work on a dying node or evict a merely-busy one.
- Q: What prevents a flapping link or node from thrashing placement? → A: Hysteresis. State transitions require the condition to persist for a configured number of consecutive evaluations, and recovery requires a longer confirmation than failure. Health states are treated as a continuum with damping, not as instantaneous booleans, because a transient degradation promoted immediately to a categorical failure causes more disruption than the degradation itself.
- Q: How is stale topology knowledge avoided? → A: Every health record carries the observation time, and any consumer treats a record older than its freshness window as unknown rather than as last-known-good. Placement decisions made on stale health are the failure this guards against — a node that looks present because nothing has updated its row.
- Q: What monitoring overhead is acceptable on a Studio? → A: A hard budget, because the Studios exist to run inference and nothing else. Collection on a node must stay within a small single-digit percentage of one CPU core and a bounded memory footprint, sampling at an interval tuned to that budget rather than to the finest resolution obtainable. Any metric that cannot be gathered within the budget is gathered less often or not at all.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A slow request is attributable to a span (Priority: P1)

An operator investigating a slow response opens its trace and sees exactly where the time went, rather than guessing between the gateway, the queue, the load, and the model.

**Why this priority**: This is the roadmap's named acceptance bar and the entire reason to instrument rather than merely log.

**Independent Test**: Deliberately induce a slow request, open its trace, and identify the dominant span.

**Acceptance Scenarios**:

1. **Given** a completed request, **When** its trace is opened, **Then** it decomposes into gateway, admission wait, load wait, prefill, decode, and network spans as applicable.
2. **Given** a request deliberately delayed at a known stage, **When** its trace is examined, **Then** the dominant span is the delayed stage.
3. **Given** a request against a sharded instance, **When** its trace is examined, **Then** node-to-node time is attributable separately from decode.
4. **Given** a request that waited for a cold model, **When** its trace is examined, **Then** load wait is distinguishable from queueing.

---

### User Story 2 - Alerts fire on the failures that are otherwise silent (Priority: P1)

An operator is told when a node goes away, the interconnect drops, the ledger drifts, a load hangs, a run overruns, or the tunnel dies — without watching a screen.

**Why this priority**: Each of these fails silently. A cluster with an unreachable node still answers some requests, which is exactly why it needs to page rather than wait to be noticed.

**Independent Test**: Take a node offline and confirm the node-unreachable alert fires; drop the tunnel and confirm the tunnel alert fires.

**Acceptance Scenarios**:

1. **Given** a healthy cluster, **When** a node becomes unreachable, **Then** the node-down alert fires within its evaluation window.
2. **Given** a healthy mesh, **When** the interconnect peer drops, **Then** the peer-down alert fires.
3. **Given** ledger drift beyond 10%, **When** it persists, **Then** the drift alert fires.
4. **Given** a model load exceeding its configured duration, **When** the threshold passes, **Then** the slow-load alert fires.
5. **Given** the tunnel dropping, **When** it does, **Then** the tunnel-down alert fires.
6. **Given** any alert, **When** it fires, **Then** it names the affected node or subject rather than firing generically.

---

### User Story 3 - Three dashboards answer the three standing questions (Priority: P2)

An operator has one place for cluster capacity, one for traffic, and one for jobs, each built to answer the question that screen exists for.

**Why this priority**: Dashboards are how the platform is understood day to day, but the alerts above are what prevent outages.

**Independent Test**: Load each dashboard against a running cluster and confirm it answers its question without further navigation.

**Acceptance Scenarios**:

1. **Given** a running cluster, **When** the cluster dashboard is opened, **Then** per-node live CPU and GPU utilisation, memory used against budget, reservations, thermal state, loaded models, disk, and interconnect health are shown.
2. **Given** traffic flowing, **When** the traffic dashboard is opened, **Then** request rate, latency distribution, and tokens broken down by user, credential, and model are shown.
3. **Given** running work, **When** the jobs dashboard is opened, **Then** agent runs and jobs are shown with durations, outcomes, and links to their traces.
4. **Given** any dashboard panel, **When** an anomaly is visible, **Then** the operator can reach the underlying trace or log from that panel.

---

### User Story 4 - Node-level resource behaviour is visible (Priority: P2)

An operator can see live per-node CPU and memory utilisation, per-process CPU and resident memory, GPU utilisation, thermal state, throughput, queue depth, and load and unload durations.

**Why this priority**: These are the signals that explain why the cluster behaves as it does, and the ledger's accuracy depends on them being observable.

**Independent Test**: Load a model and confirm its CPU and resident memory, the resulting node-level change, and the load duration all appear as metrics.

**Acceptance Scenarios**:

1. **Given** a node agent running, **When** metrics are scraped, **Then** node CPU utilisation, memory used, budget, and reserved, plus per-process CPU and resident memory, are exposed.
2. **Given** a model generating, **When** metrics are scraped, **Then** throughput and queue depth are exposed.
3. **Given** a load or unload, **When** it completes, **Then** its duration is recorded as a metric.
4. **Given** a node under thermal pressure, **When** metrics are scraped, **Then** GPU utilisation and thermal state are exposed.
5. **Given** the node agent collecting metrics, **When** its own resource use is measured, **Then** it stays within its configured collection budget.

---

### User Story 5 - Cluster health is answered end to end (Priority: P1)

An operator asks one question — is the cluster healthy — and gets an answer covering every node's liveness and live utilisation, the interconnect, and the control plane, with a degraded node distinguished from a dead one.

**Why this priority**: This is the question every incident starts with. Answering it from link state alone, as this feature's first draft did, describes a fraction of the ways the cluster can be unwell.

**Independent Test**: Saturate one node's CPU without killing it, and confirm the cluster reports degraded rather than healthy or unreachable.

**Acceptance Scenarios**:

1. **Given** a running cluster, **When** cluster health is read, **Then** it reports per-node liveness, live CPU and memory utilisation, thermal state, interconnect health, and control-plane reachability, plus an aggregate verdict.
2. **Given** a node that heartbeats but exceeds its latency or utilisation budget, **When** health is evaluated, **Then** it is reported `degraded`, not `unreachable`.
3. **Given** a node whose heartbeat stops beyond the failure threshold, **When** health is evaluated, **Then** it is reported `unreachable`, and the elapsed time since its last successful heartbeat is shown.
4. **Given** a node recovering, **When** it resumes heartbeating, **Then** it returns to healthy only after the longer recovery confirmation, not on the first successful beat.
5. **Given** a health record older than its freshness window, **When** it is consumed, **Then** it is treated as unknown rather than as last-known-good.
6. **Given** a link or node flapping, **When** health is evaluated, **Then** transitions are damped and placement is not thrashed.

---

### Edge Cases

- The collector is down: services MUST continue serving requests, and telemetry loss MUST NOT become an outage.
- Telemetry volume spikes: retention and cardinality limits MUST protect core's disk rather than filling it.
- A trace spans the gateway and a node agent: context MUST propagate so the trace is not split into two unrelated traces.
- A metric label would carry a user identifier of unbounded cardinality: labels MUST be bounded, with high-cardinality detail carried in traces and logs instead.
- Logs contain a credential or prompt content: secrets MUST never be logged, and user content MUST be handled according to a stated policy rather than logged indiscriminately.
- An alert fires repeatedly for one ongoing condition: it MUST group rather than emit continuously.
- Core is under memory pressure: the observability stack MUST be constrained so it cannot starve the control plane.
- Clocks drift between nodes: traces MUST remain interpretable, and significant skew MUST itself be visible. Heartbeat freshness MUST be computed so that skew cannot make a stale record look current.
- A node is saturated but alive: it MUST report `degraded` with its utilisation, and MUST NOT be reported either healthy or unreachable.
- A node is thermally throttled while otherwise responsive: throttling MUST be visible as its own signal rather than presenting only as reduced throughput.
- The interconnect flaps repeatedly: damping MUST prevent repeated placement changes, and the flapping itself MUST be alertable as a distinct condition from a clean link failure.
- The node agent dies while its engines keep running: health MUST report the node as unreachable while recording that its processes are unverified, rather than implying the models are gone.
- Metric collection would exceed its budget on a busy node: sampling MUST degrade rather than compete with inference for CPU.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The observability stack MUST run as containers within the control plane's compose project on core, one process per container.
- **FR-002**: All telemetry MUST remain local; no signal may egress to an external service.
- **FR-003**: The control plane and node agents MUST emit traces, metrics, and structured logs to the local collector.
- **FR-004**: Trace context MUST propagate across the gateway and node agent so a request forms one trace.
- **FR-005**: A request's trace MUST decompose into gateway, admission wait, load wait, prefill, decode, and node-to-node network spans as applicable.
- **FR-006**: Node agents MUST expose live per-node CPU utilisation and memory used, budget, and reserved; per-process CPU utilisation and resident memory; GPU utilisation; thermal state; throughput; queue depth; and load and unload durations.
- **FR-006a**: Metric collection on a node MUST stay within a configured resource budget, and MUST reduce sampling frequency or omit a metric rather than exceed it. The node agent MUST expose its own collection cost.
- **FR-006b**: Collection MUST NOT require a privileged helper running continuously; any elevated sampling MUST be short-lived, bounded, and omittable without losing the rest of the signal.
- **FR-007**: The system MUST provide a cluster dashboard covering, per node, live CPU and GPU utilisation, memory used against budget, reservations, thermal state, loaded models, disk, and interconnect health, plus the aggregate health verdict.
- **FR-008**: The system MUST provide a traffic dashboard covering request rate, latency distribution, and tokens by user, credential, and model.
- **FR-009**: The system MUST provide a jobs dashboard covering agent runs and jobs with durations, outcomes, and trace links.
- **FR-010**: Dashboard panels MUST allow navigation to the underlying traces or logs.
- **FR-011**: The system MUST define alert rules for node unreachable, node degraded, sustained CPU or memory saturation, thermal throttling, interconnect peer down or flapping, ledger drift beyond threshold, model load exceeding duration, agent run exceeding timeout, and tunnel down.
- **FR-012**: Alerts MUST name the affected subject and MUST group rather than emit continuously for one ongoing condition.
- **FR-012a**: The system MUST expose an end-to-end cluster health view covering per-node liveness and live utilisation, thermal state, interconnect health, and control-plane reachability, with an aggregate verdict.
- **FR-012b**: Node liveness MUST be established by a heartbeat on a configured short interval, separately from responsiveness.
- **FR-012c**: A node that heartbeats but exceeds its latency or utilisation budget MUST be reported `degraded`; only sustained heartbeat loss beyond a configured threshold may report `unreachable`.
- **FR-012d**: Health state transitions MUST require the condition to persist across a configured number of consecutive evaluations, and recovery MUST require a longer confirmation than failure.
- **FR-012e**: Every health record MUST carry its observation time, and a consumer MUST treat a record older than its freshness window as unknown rather than as last-known-good.
- **FR-012f**: Placement and scheduling decisions MUST NOT be made on health records outside their freshness window.
- **FR-012g**: Time since last successful heartbeat MUST be exposed per node.
- **FR-013**: Telemetry retention MUST default to seven days per signal and MUST be configurable per signal.
- **FR-014**: Metric label cardinality MUST be bounded; high-cardinality detail MUST live in traces and logs.
- **FR-015**: Credentials MUST never appear in logs, metrics, or traces.
- **FR-016**: The system MUST apply a stated policy for whether and how prompt and response content is recorded.
- **FR-017**: Collector or backend unavailability MUST NOT cause request failures.
- **FR-018**: The observability stack MUST be resource-constrained so it cannot starve the control plane on core.
- **FR-019**: The system MUST document a convention by which a later feature adds its dashboard panel and alert rule.
- **FR-020**: Significant clock skew between nodes MUST be observable.

### Key Entities

- **Trace**: One request's end-to-end record. Trace identity, spans with names, durations, parent relationships, node and service attribution.
- **Metric Series**: A named measurement over time with bounded labels for node, service, model, and outcome.
- **Alert Rule**: A condition with a threshold, evaluation window, subject labels, and severity.
- **Dashboard**: A named collection of panels answering one operational question, with links from panels to traces and logs.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A deliberately slowed request is attributable to the correct span in its trace, in 100% of trials across the six span categories.
- **SC-002**: Taking a node offline fires the node-down alert within its evaluation window, naming the node.
- **SC-003**: Dropping the tunnel fires the tunnel-down alert.
- **SC-004**: All six required alert rules exist, are evaluated, and each has been observed to fire under an induced condition.
- **SC-005**: All three dashboards load against a live cluster and answer their question without further navigation.
- **SC-006**: A trace spanning gateway and node agent appears as one trace, not two.
- **SC-007**: No credential appears in any log, metric, or trace, verified by inspection.
- **SC-008**: With the collector stopped, request success rate is unchanged.
- **SC-009**: Seven-day retention across all three signals stays within core's allocated disk budget.
- **SC-010**: Cluster health reports live CPU and memory utilisation for every node, refreshed within its configured interval.
- **SC-011**: A saturated-but-alive node is reported `degraded`, never healthy and never unreachable, in 100% of trials.
- **SC-012**: A node whose heartbeat stops is reported `unreachable` within the configured threshold, with time since last beat shown.
- **SC-013**: A flapping link or node produces no placement change, and is alerted as flapping.
- **SC-014**: Node-side metric collection stays within its configured budget under full inference load, measured on a Studio.
- **SC-015**: No scheduling decision is made on a health record outside its freshness window.

## Assumptions

- Feature 000 shipped the collector container and OTLP export wiring from the API; this feature adds the metrics, logs, and traces backends, the dashboards, and the alert rules.
- Features 001–007 have shipped, so there are nodes, instances, a ledger, requests, and credentials to instrument.
- Core is an M4 Pro with 24 GB of memory and 460 GB of disk (verified 2026-08-29), shared with Postgres and the whole control plane, which is what motivates the default seven-day retention and the resource constraints.
- Alert delivery destinations are an operator configuration concern; this feature guarantees rules exist, evaluate, and fire.
- Interconnect health metrics come from the link record feature 006 produces; if feature 006 has not shipped, the peer-down rule covers reachability only.
- Agent-run metrics become meaningful when feature 011 ships runs; the jobs dashboard is built to accommodate them and shows model and acquisition jobs until then.
- Later features add their own panel and alert rule to this structure, which is how Principle VI is satisfied going forward.
- The Studios are kept deliberately bare so their compute is dedicated to inference. Monitoring is therefore held to an explicit resource budget on a node, and any agent-side collection that cannot meet it is reduced or dropped rather than allowed to compete with inference.
- The degraded-versus-unreachable distinction, transition damping, and health-record freshness are drawn from documented failure modes in comparable local AI clusters, where coordinators unable to separate a slow node from a dead one either strand work on a dying node or evict a merely-busy one, and where undamped link flapping thrashes placement.
