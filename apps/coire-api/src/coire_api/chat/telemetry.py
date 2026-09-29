"""Content-free native Chat request telemetry."""

from __future__ import annotations

from opentelemetry import metrics, trace

tracer = trace.get_tracer("coire.api.chat")
requests_total = metrics.get_meter("coire.api.chat").create_counter(
    "coire_chat_requests_total",
    unit="1",
    description="Native Chat API requests by operation and outcome",
)
