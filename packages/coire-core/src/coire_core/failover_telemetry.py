"""Election, readiness, snapshot, and ingress instruments for the failover tier."""

from opentelemetry import metrics, trace

tracer = trace.get_tracer("coire.failover")
meter = metrics.get_meter("coire.failover")

elections_total = meter.create_counter(
    "coire_failover_elections_total",
    unit="1",
    description="Promotion and break-glass elections",
)
handbacks_total = meter.create_counter(
    "coire_failover_handbacks_total",
    unit="1",
    description="Elected members that began draining for hand-back",
)
ingress_refusals_total = meter.create_counter(
    "coire_failover_ingress_refusals_total",
    unit="1",
    description="Requests refused because the tier could not safely serve them",
)
readiness = meter.create_gauge(
    "coire_failover_readiness",
    description="1 when this host is the elected serving tier",
)
snapshot_fresh = meter.create_gauge(
    "coire_failover_snapshot_fresh",
    description="1 when the local failover snapshot is signed and unexpired",
)
participant_last_seen = meter.create_gauge(
    "coire_failover_participant_last_seen_seconds",
    unit="s",
    description="Last completed local election observation in Unix seconds",
)
audit_available = meter.create_gauge(
    "coire_failover_audit_available",
    description="Required journal is readable with reserved fencing capacity",
)
audit_utilization = meter.create_gauge(
    "coire_failover_audit_utilization",
    description="Fraction of ordinary unacknowledged journal slots used",
)
drain_remaining = meter.create_gauge(
    "coire_failover_drain_remaining_seconds",
    unit="s",
    description="Remaining bounded drain authority",
)
