"""Gateway instruments promised to observability consumers."""

from opentelemetry import metrics, trace

tracer = trace.get_tracer("coire.gateway")
meter = metrics.get_meter("coire.gateway")
request_duration_ms = meter.create_histogram("coire_gateway_request_duration_ms", unit="ms")
queue_duration_ms = meter.create_histogram("coire_gateway_queue_duration_ms", unit="ms")
first_token_duration_ms = meter.create_histogram("coire_gateway_first_token_duration_ms", unit="ms")
overhead_duration_ms = meter.create_histogram("coire_gateway_overhead_duration_ms", unit="ms")
request_counter = meter.create_counter("coire_gateway_requests_total")
token_counter = meter.create_counter(
    "coire_gateway_tokens_total",
    description="Prompt and completion tokens with bounded identity-class labels.",
)
failure_counter = meter.create_counter("coire_gateway_failures_total")
inflight_counter = meter.create_up_down_counter("coire_gateway_inflight")
