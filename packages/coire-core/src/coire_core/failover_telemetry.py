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
    unit="1",
    description="1 when this host is the elected serving tier",
)
snapshot_fresh = meter.create_gauge(
    "coire_failover_snapshot_fresh",
    unit="1",
    description="1 when the local failover snapshot is signed and unexpired",
)
