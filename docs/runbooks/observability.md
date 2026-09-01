# Observability

Coire keeps traces, metrics, and operational logs on core. Prometheus, Alertmanager, Loki, Tempo, Grafana, and the collector are independent services on `coire-telemetry`; Grafana is available only through the normal `/grafana/` ingress. Defaults retain each signal for seven days and constrain the stack to protect the control plane. The default ingestion envelope is 92.48 GB for that period: 32 GB Prometheus, at most 0.05 MB/s Loki, and at most 50,000 B/s Tempo. Check free space before raising any corresponding compose override.

## Content and cardinality policy

Never emit prompts, responses, authorization/cookie headers, API keys, node tokens, passwords, or secret values. Metric labels are bounded to service, node, operation, model registry slug, outcome, placement, and identity class. Put exact user, credential, run, job, and instance correlation IDs on spans or structured logs.

## Diagnose

Start at Cluster for aggregate health and live node/link capacity, Traffic for request/latency/token behavior, or Jobs for workflow duration and outcome. Follow panel links into Tempo or Loki. Required request stages are gateway, admission wait, load wait, prefill, decode, and node network.

If the collector or a backend is down, serving continues. Inspect `docker compose -f deploy/compose/compose.yaml ps`, then the affected service logs. Restart only that service. Storage volumes are signal-specific; do not delete one during diagnosis.

## Alert actions

### Node unreachable

Check heartbeat age and control VLAN reachability. Engines are `unverified`, not presumed stopped. Do not force placement until a fresh observation exists.

### Node degraded / saturation / thermal

Check CPU, memory, process RSS, thermal state, and collection cost. Drain work through normal scheduler/admin verbs; do not kill unknown processes.

### Interconnect

Confirm the Studio-only Thunderbolt cable and data listener. Link flapping is damped; it must not trigger repeated placement changes. Single-node control remains on Wi-Fi.

### Ledger, load, or run

Follow the subject to Jobs and its trace. Use audited cancel/unload operations. Never edit ledger rows directly.

### Tunnel

Confirm cloudflared independently of internal service health. The tunnel is outbound-only; do not widen firewall ingress.

### Clock skew

Compare node sample time with control-plane receipt time and correct macOS time synchronization. Freshness is always based on receipt time.

## Induced validation and rollback

Run deterministic fixtures first. Schedule real node/tunnel interruption only when no active inference depends on it. Restore the component, wait for five successful health evaluations, and confirm the alert resolves. Roll back by restoring the prior compose/config commit and recreating only observability containers; persistent signal volumes are compatible and must not be removed.

## Adding a feature panel and rule

Add a bounded `coire_` metric and `coire.<service>.<operation>` span, a version-controlled panel in one seed dashboard, and a Prometheus rule carrying `severity`, `subject`, `runbook_url`, and `dashboard`. Add a deterministic rule/config test and a runbook action. Validate with promtool and a clean compose run.
